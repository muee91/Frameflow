"""Small transactional store. Shared facts never contain device paths."""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from copy import deepcopy
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path

from .errors import Conflict, TaskCancelled, TaskError
from .file_state import path_signature

ACTIVE = {"queued", "running", "cancelling"}
RETRYABLE = {"failed", "cancelled", "interrupted"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in {0, 1}:
                raise RuntimeError("本机数据库来自更新版本，不能降级打开。")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL,
                    roots_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets (
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    media_id TEXT NOT NULL, metadata_json TEXT NOT NULL,
                    PRIMARY KEY(project_id, media_id)
                );
                CREATE TABLE IF NOT EXISTS bindings (
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    media_id TEXT NOT NULL, path TEXT NOT NULL, signature_json TEXT NOT NULL,
                    PRIMARY KEY(project_id, media_id)
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
                    attempt INTEGER NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_project
                  ON jobs(project_id) WHERE state IN ('queued','running','cancelling');
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id), created_at REAL NOT NULL,
                    level TEXT NOT NULL, stage TEXT NOT NULL, message TEXT NOT NULL,
                    current_item TEXT
                );
                CREATE INDEX IF NOT EXISTS events_job_seq ON events(job_id, seq);
                CREATE TABLE IF NOT EXISTS results (
                    project_id TEXT PRIMARY KEY REFERENCES projects(id),
                    job_id TEXT NOT NULL REFERENCES jobs(id), revision INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL, job_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    clip_id TEXT NOT NULL, created_at REAL NOT NULL,
                    previous_json TEXT NOT NULL, replacement_json TEXT NOT NULL, reason TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield db

    @staticmethod
    def _job(db, job_id):
        row = db.execute("SELECT payload_json FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return json.loads(row[0])

    @staticmethod
    def _save_job(db, job):
        db.execute("UPDATE jobs SET state=?,payload_json=? WHERE id=?",
                   (job["state"], encode(job), job["id"]))

    @staticmethod
    def _event(db, job, message, level="info", current_item=None):
        db.execute("INSERT INTO events(job_id,created_at,level,stage,message,current_item) VALUES(?,?,?,?,?,?)",
                   (job["id"], time.time(), level, job["stage"], str(message)[:2000], current_item))

    def create_project(self, name, roots):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("项目名称须为 1–100 个字符。")
        if not roots or len(roots) > 32:
            raise ValueError("请添加 1–32 个机位或录音目录。")
        normalized, seen = [], set()
        for root in roots:
            path = Path(root["path"]).expanduser()
            if not path.is_absolute():
                raise ValueError("请选择本机绝对目录。")
            if path.is_symlink():
                raise ValueError("素材目录不能是符号链接。")
            if not path.is_dir():
                raise ValueError("所选素材目录不存在或不能读取。")
            path = path.resolve()
            if any(path == other or path in other.parents or other in path.parents for other in seen):
                raise ValueError("素材目录不能重复或互相包含。")
            seen.add(path)
            kind = root.get("kind", "camera")
            if kind not in {"camera", "recorder"}:
                raise ValueError("素材来源须为机位或独立录音。")
            label = root.get("label", "").strip()
            if not label or len(label) > 80:
                raise ValueError("请为每个目录填写 1–80 字的机位/录音名称。")
            normalized.append({"path": str(path), "kind": kind, "label": label})
        project = {"id": uuid.uuid4().hex, "name": name, "created_at": time.time(), "roots": normalized}
        with self.transaction() as db:
            db.execute("INSERT INTO projects VALUES(?,?,?,?)",
                       (project["id"], name, project["created_at"], encode(normalized)))
        return project

    def projects(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT id,name,created_at FROM projects ORDER BY created_at DESC")]

    def project(self, project_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            if row is None:
                raise KeyError(project_id)
            return {"id": row["id"], "name": row["name"], "created_at": row["created_at"],
                    "roots": json.loads(row["roots_json"])}

    def save_asset(self, project_id, asset):
        public = {k: v for k, v in asset.items() if k not in {"local_path", "_source_signature"}}
        try:
            signature = path_signature(Path(asset["local_path"]))
        except OSError as exc:
            raise TaskError("MEDIA_UNREADABLE", "无法验证本机素材文件状态，请检查素材后重试。") from exc
        # Probe supplies the metadata state enclosing its full-content hash.
        # Refuse a known change rather than refresh the old identity's state.
        # Equal metadata is not content proof: the runner performs full hash
        # verification before publishing synchronization or delivery results.
        # Internal test/import callers without a probe snapshot still bind the
        # current state, but the production scanner always supplies this field.
        if asset.get("_source_signature") is not None and signature != asset["_source_signature"]:
            raise TaskError("SOURCE_CHANGED", "素材在身份校验后发生变化，请重新扫描核验身份。")
        with self.transaction() as db:
            db.execute("INSERT INTO assets VALUES(?,?,?) ON CONFLICT(project_id,media_id) DO UPDATE SET metadata_json=excluded.metadata_json",
                       (project_id, asset["media_id"], encode(public)))
            db.execute("INSERT INTO bindings VALUES(?,?,?,?) ON CONFLICT(project_id,media_id) DO UPDATE SET path=excluded.path,signature_json=excluded.signature_json",
                       (project_id, asset["media_id"], asset["local_path"], encode(signature)))

    def binding(self, project_id, media_id):
        with self.connect() as db:
            row = db.execute("SELECT path FROM bindings WHERE project_id=? AND media_id=?", (project_id, media_id)).fetchone()
            if row is None:
                raise KeyError(media_id)
            return Path(row[0])

    def checked_binding(self, project_id, media_id):
        """Fast local metadata guard; full identity checks belong to the worker."""
        with self.connect() as db:
            row = db.execute("SELECT path,signature_json FROM bindings WHERE project_id=? AND media_id=?", (project_id, media_id)).fetchone()
        if row is None:
            raise KeyError(media_id)
        path = Path(row["path"])
        if not path.is_file() or path.is_symlink() or path.resolve() != path:
            raise TaskError("MEDIA_OFFLINE", "素材离线或位置已变化，请恢复素材后重试。")
        try:
            signature = path_signature(path)
        except OSError as exc:
            raise TaskError("MEDIA_UNREADABLE", "无法验证本机素材文件状态，请检查素材后重试。") from exc
        # Older unversioned snapshots are deliberately not trusted/migrated in
        # place; re-scanning is required to establish a full current identity.
        if signature != json.loads(row["signature_json"]):
            raise TaskError("SOURCE_CHANGED", "素材内容或文件信息已变化，请重新扫描核验身份。")
        return path

    def create_job(self, project_id, retry_of=None, *, kind="sync", revision=None, format=None):
        project = self.project(project_id)
        now = time.time()
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running','cancelling')", (project_id,)).fetchone():
                raise Conflict("当前项目仍有任务运行，请等待或取消后再试。")
            if retry_of:
                old = self._job(db, retry_of)
                if old["project_id"] != project_id or old["state"] not in RETRYABLE:
                    raise Conflict("只有失败、取消或中断的任务可以重试。")
                kind, format, revision = old.get("kind", "sync"), old.get("format"), old.get("source_revision")
            snapshot = None
            if kind == "export":
                row = db.execute("SELECT payload_json FROM results WHERE project_id=?", (project_id,)).fetchone()
                if row is None:
                    raise Conflict("尚无同步结果。")
                snapshot = json.loads(row[0])
                if snapshot["revision"] != revision:
                    raise Conflict("结果已更新，请重新核对当前版本后导出。")
                if any(c["status"] not in {"approved", "excluded"} for c in snapshot["clips"]):
                    raise Conflict("请先核对每个片段。")
                if not any(c["status"] == "approved" for c in snapshot["clips"]):
                    raise Conflict("没有已确认的片段可导出。")
                if format != "fcpxml":
                    raise ValueError("当前仅提供实验性 FCPXML 导出。")
            elif kind != "sync":
                raise ValueError("Unknown job kind")
            previous = db.execute("SELECT MAX(attempt) FROM jobs WHERE project_id=?", (project_id,)).fetchone()[0] or 0
            job = {"id": uuid.uuid4().hex, "project_id": project_id, "attempt": previous + 1,
                   "retry_of": retry_of, "kind": kind, "format": format, "source_revision": revision,
                   "state": "queued", "stage": "export" if kind == "export" else "scan", "completed": 0, "total": None,
                   "unit": "files", "current_item": None, "message": "已排队，等待本机工作进程。",
                   "error_code": None, "error_message": None, "retryable": True,
                   "created_at": now, "started_at": None, "finished_at": None,
                   "heartbeat_at": None, "progress_at": None, "cancel_requested": False,
                   "input_roots": project["roots"], "input_result": snapshot, "artifact": None}
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?)", (job["id"], project_id, job["attempt"], job["state"], encode(job)))
            self._event(db, job, job["message"])
        return self.public_job(job)

    @staticmethod
    def public_job(job):
        return {k: v for k, v in job.items() if k not in {"input_roots", "input_result"}}

    def job(self, job_id, private=False):
        with self.connect() as db:
            job = self._job(db, job_id)
        return job if private else self.public_job(job)

    def jobs(self, project_id):
        self.project(project_id)
        with self.connect() as db:
            return [self.public_job(json.loads(row[0])) for row in db.execute(
                "SELECT payload_json FROM jobs WHERE project_id=? ORDER BY attempt DESC", (project_id,))]

    def start_job(self, job_id):
        with self.transaction() as db:
            job = self._job(db, job_id)
            if job["state"] != "queued" or job["cancel_requested"]:
                return False
            job.update(state="running", started_at=time.time(), heartbeat_at=time.time(), progress_at=time.time())
            self._save_job(db, job)
            self._event(db, job, "工作进程已启动。")
            return True

    def heartbeat(self, job_id):
        with self.transaction() as db:
            job = self._job(db, job_id)
            if job["state"] in ACTIVE:
                job["heartbeat_at"] = time.time()
                self._save_job(db, job)

    def progress(self, job_id, stage, completed, total=None, unit="files", current_item=None, message=None):
        if completed < 0 or (total is not None and (total < 0 or completed > total)):
            raise ValueError("Invalid progress counters")
        with self.transaction() as db:
            job = self._job(db, job_id)
            if job["state"] != "running" or job["cancel_requested"]:
                raise TaskCancelled()
            changed = any(job.get(key) != value for key, value in {
                "stage": stage, "completed": completed, "total": total, "unit": unit,
                "current_item": current_item, "message": message or job["message"]}.items())
            previous_item, previous_stage, previous_message = job["current_item"], job["stage"], job["message"]
            job.update(stage=stage, completed=completed, total=total, unit=unit,
                       current_item=current_item, heartbeat_at=time.time())
            if message:
                job["message"] = message
            if changed:
                job["progress_at"] = time.time()
            self._save_job(db, job)
            if (previous_item != current_item or previous_stage != stage or
                    (message and message != previous_message)):
                self._event(db, job, job["message"], current_item=current_item)

    def activity(self, job_id, message, level="info", current_item=None):
        with self.transaction() as db:
            self._event(db, self._job(db, job_id), message, level, current_item)

    def events(self, job_id, after=0, limit=200):
        self.job(job_id)
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT seq,created_at,level,stage,message,current_item FROM events WHERE job_id=? AND seq>? ORDER BY seq LIMIT ?",
                (job_id, after, min(limit, 500)))]

    def cancel(self, job_id):
        with self.transaction() as db:
            job = self._job(db, job_id)
            if job["state"] not in ACTIVE:
                return self.public_job(job)
            job["cancel_requested"] = True
            if job["state"] == "queued":
                job.update(state="cancelled", finished_at=time.time(), message="任务在启动前已取消。")
            else:
                job.update(state="cancelling", message="正在停止子进程，等待安全退出。")
            self._save_job(db, job)
            self._event(db, job, job["message"])
        return self.public_job(job)

    def finish(self, job_id, state, *, result=None, error=None, artifact=None):
        """Result publication and terminal state share a transaction/cancellation fence."""
        with self.transaction() as db:
            job = self._job(db, job_id)
            if job["state"] not in ACTIVE:
                return
            if job["cancel_requested"]:
                state, result, error, artifact = "cancelled", None, None, None
            if result is not None:
                if state != "needs_review" or job["state"] != "running":
                    raise Conflict("任务当前状态不能发布同步结果。")
                row = db.execute("SELECT revision FROM results WHERE project_id=?", (job["project_id"],)).fetchone()
                revision = (row[0] if row else 0) + 1
                result.update(job_id=job_id, revision=revision)
                db.execute("INSERT INTO results VALUES(?,?,?,?) ON CONFLICT(project_id) DO UPDATE SET job_id=excluded.job_id,revision=excluded.revision,payload_json=excluded.payload_json",
                           (job["project_id"], job_id, revision, encode(result)))
            job.update(state=state, finished_at=time.time(), heartbeat_at=time.time(), current_item=None)
            if artifact is not None:
                if state != "succeeded":
                    raise Conflict("未成功的导出不能发布文件。")
                job["artifact"] = artifact
            messages = {"needs_review": "同步计算已完成，请播放核对候选结果。", "cancelled": "已停止；未发布本次结果。",
                        "failed": "任务失败，可查看原因并重试。", "interrupted": "任务已中断。",
                        "succeeded": "工程文件已生成，目标剪辑软件导入仍待验收。"}
            job["message"] = messages.get(state, state)
            if state == "needs_review":
                job.update(stage="review", completed=0, total=len(result["clips"]), unit="clips")
            if error:
                job.update(error_code=error.code, error_message=error.message, retryable=error.retryable)
            self._save_job(db, job)
            self._event(db, job, error.message if error else job["message"], "error" if error else "info")

    def recover(self):
        with self.transaction() as db:
            for row in db.execute("SELECT payload_json FROM jobs WHERE state IN ('queued','running','cancelling')").fetchall():
                job = json.loads(row[0])
                job.update(state="interrupted", finished_at=time.time(), error_code="WORKER_INTERRUPTED",
                           error_message="上次运行未正常结束，旧结果未发布。请重试。", message="运行中断，等待重试。", retryable=True)
                self._save_job(db, job)
                self._event(db, job, job["error_message"], "warning")

    def result(self, project_id):
        with self.connect() as db:
            row = db.execute("SELECT payload_json FROM results WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                raise KeyError(project_id)
            return json.loads(row[0])

    def review(self, project_id, *, revision, clip_id, status, offset_seconds=None, reason="", group_id=None):
        if type(revision) is not int or revision < 1:
            raise ValueError("结果版本必须是正整数。")
        if status not in {"proposed", "approved", "excluded"}:
            raise ValueError("请选择应用修正、确认或排除。")
        reason = reason.strip()
        if not reason or len(reason) > 1000:
            raise ValueError("请填写 1–1000 字的核对依据或排除原因。")
        if offset_seconds is not None and (type(offset_seconds) not in {int, float} or
                                          not math.isfinite(offset_seconds) or abs(offset_seconds) > 7 * 86400):
            raise ValueError("偏移须为 7 天以内的有限秒数。")
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running','cancelling')", (project_id,)).fetchone():
                raise Conflict("正在重新同步，请等待任务结束再修正。")
            row = db.execute("SELECT * FROM results WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                raise KeyError(project_id)
            if row["revision"] != revision:
                raise Conflict("核对结果已更新，请刷新后再保存。")
            result = json.loads(row["payload_json"])
            clip = next((c for c in result["clips"] if c["id"] == clip_id), None)
            if clip is None:
                raise KeyError(clip_id)
            previous = encode(clip)
            target_group = group_id or clip.get("group_id")
            if status in {"proposed", "approved"}:
                if clip.get("blocked_reason"):
                    raise ValueError("此片段含尚未支持的时间映射，请保留待处理或排除。")
                if target_group not in {g["id"] for g in result["groups"]}:
                    raise ValueError("请指定一个已有同步组后再应用或确认。")
                target_offset = offset_seconds if offset_seconds is not None else clip.get("offset_seconds")
                if target_offset is None:
                    raise ValueError("未匹配片段需要明确的人工偏移。")
                if clip.get("status") == "unmatched" or target_offset != clip.get("offset_seconds") or target_group != clip.get("group_id"):
                    clip["evidence"] = list(dict.fromkeys(clip.get("evidence", []) + ["manual"]))
                    clip.setdefault("suggested_offset_seconds", clip.get("offset_seconds"))
                    clip.setdefault("suggested_group_id", clip.get("group_id"))
                    clip.setdefault("original_mapping", deepcopy(clip.get("mapping", [])))
                    # The shared mapping must reflect the same manual correction
                    # as the displayed offset; keep the original explicitly named.
                    points = clip.get("mapping") or []
                    if points:
                        first, last = points[0], points[-1]
                        source0 = Fraction(first["source"]["value"], first["source"]["timescale"])
                        source1 = Fraction(last["source"]["value"], last["source"]["timescale"])
                        island0 = Fraction(first["island"]["value"], first["island"]["timescale"])
                        island1 = Fraction(last["island"]["value"], last["island"]["timescale"])
                        rate = (island1 - island0) / (source1 - source0)
                        delta = Fraction(str(target_offset)) - (island0 - rate * source0)
                        for point in points:
                            value = (Fraction(point["island"]["value"], point["island"]["timescale"]) + delta).limit_denominator(1_000_000_000)
                            point["island"] = {"value": value.numerator, "timescale": value.denominator}
                    else:
                        def media_time(value):
                            value = Fraction(str(value)).limit_denominator(1_000_000_000)
                            return {"value": value.numerator, "timescale": value.denominator}
                        points = [{"source": media_time(0), "island": media_time(target_offset)},
                                  {"source": media_time(clip["duration"]),
                                   "island": media_time(target_offset + clip.get("rate", 1) * clip["duration"])}]
                    clip["mapping"] = points
                clip.update(offset_seconds=target_offset, group_id=target_group)
            clip.update(status=status, review_reason=reason)
            result["revision"] = revision + 1
            db.execute("UPDATE results SET revision=?,payload_json=? WHERE project_id=?", (revision + 1, encode(result), project_id))
            db.execute("INSERT INTO reviews(project_id,job_id,revision,clip_id,created_at,previous_json,replacement_json,reason) VALUES(?,?,?,?,?,?,?,?)",
                       (project_id, result["job_id"], revision + 1, clip_id, time.time(), previous, encode(clip), reason))
            job = self._job(db, result["job_id"])
            done = sum(c["status"] in {"approved", "excluded"} for c in result["clips"])
            job.update(completed=done, total=len(result["clips"]), progress_at=time.time())
            # "succeeded" describes the reviewed sync task, never an NLE import.
            job["state"] = "succeeded" if done == len(result["clips"]) else "needs_review"
            job["message"] = "全部片段已核对，交付仍需目标软件验收。" if job["state"] == "succeeded" else "继续核对剩余片段。"
            self._save_job(db, job)
            action = "已应用修正，仍待播放核对" if status == "proposed" else "已保存人工核对"
            self._event(db, job, f"{action}：{clip['name']}", current_item=clip["name"])
        return result

    def last_undoable_clip(self, project_id, revision, job_id):
        """Undo is available only for the latest revision of this result."""
        with self.connect() as db:
            row = db.execute(
                "SELECT clip_id,revision,reason FROM reviews WHERE project_id=? AND job_id=? ORDER BY seq DESC LIMIT 1",
                (project_id, job_id),
            ).fetchone()
        if row is None or row["revision"] != revision or row["reason"].startswith("撤销最近一次修订"):
            return None
        return row["clip_id"]

    def undo_last_review(self, project_id, *, revision, clip_id):
        """Revision-checked undo. Restore proposal, never silently reinstate approval."""
        if type(revision) is not int or revision < 1 or not isinstance(clip_id, str) or not clip_id:
            raise ValueError("撤销参数无效。")
        with self.transaction() as db:
            if db.execute(
                "SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running','cancelling')",
                (project_id,),
            ).fetchone():
                raise Conflict("有正在进行的任务，不能撤销核对。")
            row = db.execute("SELECT * FROM results WHERE project_id=?", (project_id,)).fetchone()
            if row is None:
                raise KeyError(project_id)
            if row["revision"] != revision:
                raise Conflict("核对结果已经改变，请重新读取后再撤销。")
            last = db.execute(
                "SELECT * FROM reviews WHERE project_id=? AND job_id=? ORDER BY seq DESC LIMIT 1",
                (project_id, row["job_id"]),
            ).fetchone()
            if (last is None or last["revision"] != revision or
                    last["clip_id"] != clip_id or last["reason"].startswith("撤销最近一次修订")):
                raise Conflict("只能撤销本次同步的最近一条人工核对操作。")
            result = json.loads(row["payload_json"])
            current = next((c for c in result["clips"] if c["id"] == clip_id), None)
            if current is None or encode(current) != last["replacement_json"]:
                raise Conflict("素材状态发生变化，不能安全撤销。")
            restored = json.loads(last["previous_json"])
            # Previously approved or excluded data must not regain export authority
            # through an undo; it must be replayed and explicitly approved.
            if restored["status"] in {"approved", "excluded"}:
                restored["status"] = "proposed"
            restored["review_reason"] = "已撤销上次修改，仍待重新播放核对。"
            result["clips"] = [restored if c["id"] == clip_id else c for c in result["clips"]]
            result["revision"] = revision + 1
            reason = f"撤销最近一次修订 #{last['seq']}，重新播放核对"
            db.execute("UPDATE results SET revision=?,payload_json=? WHERE project_id=?",
                       (revision + 1, encode(result), project_id))
            db.execute(
                "INSERT INTO reviews(project_id,job_id,revision,clip_id,created_at,previous_json,replacement_json,reason) VALUES(?,?,?,?,?,?,?,?)",
                (project_id, result["job_id"], revision + 1, clip_id, time.time(),
                 encode(current), encode(restored), reason),
            )
            job = self._job(db, result["job_id"])
            done = sum(c["status"] in {"approved", "excluded"} for c in result["clips"])
            job.update(completed=done, total=len(result["clips"]), state="needs_review",
                       progress_at=time.time(), message="修改已撤销，等待重新核对。")
            self._save_job(db, job)
            self._event(db, job, f"撤销最近修订：{restored['name']}", current_item=restored["name"])
        return result

    def review_history(self, project_id):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT seq,job_id,revision,clip_id,created_at,reason FROM reviews WHERE project_id=? ORDER BY seq", (project_id,))]

    def require_exportable(self, project_id):
        with self.connect() as db:
            if db.execute("SELECT 1 FROM jobs WHERE project_id=? AND state IN ('queued','running','cancelling')", (project_id,)).fetchone():
                raise Conflict("重新同步尚未完成，暂不能导出旧结果。")
        result = self.result(project_id)
        if any(c["status"] not in {"approved", "excluded"} for c in result["clips"]):
            raise Conflict("请先核对每个片段；无法匹配的片段需要补正或明确排除。")
        if not any(c["status"] == "approved" for c in result["clips"]):
            raise Conflict("没有已确认的同步片段可供导出。")
        return result
