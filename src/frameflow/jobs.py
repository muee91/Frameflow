"""A single local queue; durable state and child-process work are separate."""

from __future__ import annotations

import os
import queue
import shutil
import threading
import time
from pathlib import Path

from .errors import TaskCancelled, TaskError
from .store import Store


class WorkspaceLock:
    """Do not let a second server 'recover' a live server's jobs."""

    def __init__(self, root):
        self.file = open(Path(root) / "worker.lock", "a+b")
        self.file.seek(0)
        if not self.file.read(1):
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise RuntimeError("该工作目录已有 Frameflow 运行，请使用原窗口或关闭旧进程。") from exc

    def close(self):
        self.file.close()


class JobContext:
    def __init__(self, store, job_id, shutdown=None):
        self.store, self.job_id = store, job_id
        self.shutdown = shutdown or threading.Event()
        self.last_heartbeat = 0.0
        self.last_progress_tick = 0.0
        self.last_progress_key = None

    def check_cancel(self):
        if self.shutdown.is_set():
            raise TaskCancelled()
        job = self.store.job(self.job_id)
        if job["cancel_requested"] or job["state"] not in {"running", "queued"}:
            raise TaskCancelled()

    def heartbeat(self):
        now = time.monotonic()
        if now - self.last_heartbeat >= 1:
            self.store.heartbeat(self.job_id)
            self.last_heartbeat = now

    def progress(self, stage, completed, total=None, unit="files", current_item=None, message=None):
        self.check_cancel()
        now = time.monotonic()
        key = (stage, unit, current_item)
        # Byte hashing can produce thousands of updates/second. Keep cancellation
        # responsive without turning the media read into a SQLite write benchmark.
        if (unit == "bytes" and key == self.last_progress_key and total is not None and
                0 < completed < total and now - self.last_progress_tick < 0.2):
            return
        self.store.progress(self.job_id, stage, completed, total, unit, current_item, message)
        self.last_progress_tick, self.last_progress_key = now, key

    def activity(self, message, level="info", current_item=None):
        self.store.activity(self.job_id, message, level, current_item)


class JobManager:
    def __init__(self, workspace: Path, engine_path=None):
        self.workspace = Path(workspace).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.lock = WorkspaceLock(self.workspace)
        self.store = Store(self.workspace / "frameflow.sqlite3")
        self.engine_path = engine_path
        self.store.recover()
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True, name="frameflow-worker")
        self.thread.start()

    def submit(self, project_id, retry_of=None, **kwargs):
        job = self.store.create_job(project_id, retry_of, **kwargs)
        self.pending.put(job["id"])
        return job

    def _loop(self):
        while not self.stopping.is_set():
            try:
                job_id = self.pending.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self._run(job_id)
            except Exception as exc:
                # One broken task must not silently kill the queue consumer.
                try:
                    self.store.finish(job_id, "failed", error=TaskError(
                        "WORKER_ERROR", f"工作进程发生异常（{type(exc).__name__}），任务未发布。"))
                except Exception:
                    import logging
                    logging.getLogger(__name__).error("Cannot persist worker failure; check the local workspace")
            finally:
                self.pending.task_done()

    def _run(self, job_id):
        run_dir = self.workspace / "runs" / job_id
        owns_run_dir = False
        try:
            from . import engine, media
            if not self.store.start_job(job_id):
                return
            job = self.store.job(job_id, private=True)
            ctx = JobContext(self.store, job_id, self.stopping)
            run_dir.mkdir(parents=True, exist_ok=False)
            owns_run_dir = True
            ctx.check_cancel()
            info = engine.inspect_engine(self.engine_path)
            if not info["available"]:
                raise TaskError("ENGINE_UNAVAILABLE", info["message"])
            ctx.activity(f"同步引擎：Align {info['version']}；运行结果需要人工核对。")
            if job.get("kind") == "export":
                source = job["input_result"]
                verified_paths = {}
                for clip in source["clips"]:
                    if clip["status"] == "approved":
                        verified_paths[clip["media_id"]] = self.store.checked_binding(job["project_id"], clip["media_id"])
                raw_path = self.workspace / "runs" / source["job_id"] / "align-result.json"
                output = engine.export_fcpxml(info["executable"], raw_path, source, run_dir / "export", ctx,
                                              verified_paths=verified_paths)
                ctx.check_cancel()
                for media_id, exact_path in verified_paths.items():
                    if self.store.checked_binding(job["project_id"], media_id) != exact_path:
                        raise TaskError("SOURCE_CHANGED", "交付期间素材绑定已变化，未发布本次工程。")
                # Only the filename is public; the local path stays in the workspace.
                if not output.resolve().is_relative_to(run_dir.resolve()) or not output.is_file():
                    raise TaskError("INVALID_EXPORT_PATH", "导出器返回了无效文件位置。", False)
                self.store.finish(job_id, "succeeded", artifact=str(output.relative_to(run_dir)))
                return
            if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
                raise TaskError("MEDIA_TOOLS_MISSING", "请安装 FFmpeg 和 ffprobe 并加入 PATH，然后重试。")
            items = media.scan_roots(job["input_roots"], ctx)
            assets, seen, signatures = [], set(), {}
            for index, item in enumerate(items):
                ctx.progress("probe", index, len(items), current_item=item["path"].name,
                             message="读取真实媒体元数据并校验完整内容身份。")
                asset = media.probe_and_hash(item, ctx)
                if asset["media_id"] in seen:
                    ctx.activity("发现完全相同的素材副本，仅分析一份。", "warning", asset["name"])
                    continue
                seen.add(asset["media_id"])
                stat = Path(asset["local_path"]).stat()
                signatures[asset["media_id"]] = (stat.st_size, stat.st_mtime_ns)
                assets.append(asset)
                self.store.save_asset(job["project_id"], asset)
            if len(assets) < 2:
                raise TaskError("INSUFFICIENT_MEDIA", "至少需要两个不同内容的媒体文件才能同步。")
            ctx.progress("sync", 0, None, unit="tasks", message="正在结合音频、时间码和连续录制证据计算候选位置。")
            result = engine.synchronize(info["executable"], assets, run_dir, ctx)
            for asset in assets:
                ctx.check_cancel()
                stat = Path(asset["local_path"]).stat()
                if signatures[asset["media_id"]] != (stat.st_size, stat.st_mtime_ns):
                    raise TaskError("SOURCE_CHANGED", f"分析期间素材发生变化：{asset['name']}。请重试。")
            self.store.finish(job_id, "needs_review", result=result)
        except TaskCancelled:
            self.store.finish(job_id, "cancelled")
        except TaskError as exc:
            self.store.finish(job_id, "failed", error=exc)
        except Exception as exc:
            # Do not leak local paths, command arguments, or arbitrary stderr to events.
            self.store.finish(job_id, "failed", error=TaskError(
                "WORKER_ERROR", f"任务未完成（{type(exc).__name__}），没有发布结果。请检查媒体后重试。"))
        finally:
            state = self.store.job(job_id)["state"]
            if owns_run_dir and state not in {"needs_review", "succeeded"}:
                # A cancelled/failed attempt can never become a downloadable artifact.
                shutil.rmtree(run_dir, ignore_errors=True)

    def close(self):
        self.stopping.set()
        for project in self.store.projects():
            for job in self.store.jobs(project["id"]):
                if job["state"] in {"queued", "running", "cancelling"}:
                    self.store.cancel(job["id"])
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            # Retain ownership until process exit rather than permit another runner.
            raise RuntimeError("工作进程尚未退出；工作目录锁已保留。")
        self.lock.close()
