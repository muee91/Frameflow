from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import ModuleType
import unittest
from unittest.mock import patch

import frameflow
from frameflow.errors import TaskError
from frameflow.jobs import JobManager, WorkspaceLock


def fake_result(assets):
    return {
        "groups": [{"id": "g1", "name": "合成同步组"}],
        "clips": [
            {"id": a["id"], "media_id": a["media_id"], "name": a["name"], "camera": a["camera"], "kind": a["kind"],
             "duration": 2.0, "group_id": "g1", "offset_seconds": 0.0, "status": "candidate", "evidence": ["audio"]}
            for a in assets
        ],
    }


class JobTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.media.mkdir()
        (self.media / "A.mov").write_bytes(b"synthetic first source")
        (self.media / "B.wav").write_bytes(b"synthetic second source")
        self.seen_by_engine = []
        self.engine = ModuleType("frameflow.engine")
        self.engine.inspect_engine = lambda _: {"available": True, "executable": "test-engine", "version": "test"}

        def synchronize(_executable, assets, _run_dir, _ctx):
            self.seen_by_engine.append(assets)
            return fake_result(assets)

        self.engine.synchronize = synchronize
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.dict(sys.modules, {"frameflow.engine": self.engine}))
        self.patches.enter_context(patch.object(frameflow, "engine", self.engine, create=True))
        self.patches.enter_context(patch("frameflow.jobs.shutil.which", return_value="test-tool"))
        self.patches.enter_context(patch("frameflow.media.probe_and_hash", side_effect=self.probe))
        self.manager = JobManager(self.root / "state")
        self.addCleanup(self.manager.close)
        self.project = self.manager.store.create_project("任务测试", [{"path": str(self.media), "label": "A", "kind": "camera"}])

    @staticmethod
    def probe(item, ctx):
        ctx.check_cancel()
        path = item["path"]
        identity = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        return {"id": identity, "media_id": identity, "name": path.name, "local_path": str(path),
                "duration": 2.0, "camera": item["camera"], "kind": item["kind"], "has_audio": True, "has_video": path.suffix == ".mov"}

    def wait_for(self, job_id, states=("needs_review", "failed", "cancelled"), timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            row = self.manager.store.job(job_id)
            if row["state"] in states:
                return row
            time.sleep(0.01)
        self.fail(f"job did not reach {states}: {self.manager.store.job(job_id)['state']}")

    def test_cancelled_queued_task_never_calls_engine(self):
        queued = self.manager.store.create_job(self.project["id"])
        self.manager.store.cancel(queued["id"])
        self.manager.pending.put(queued["id"])
        deadline = time.monotonic() + 2
        while self.manager.pending.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.manager.pending.unfinished_tasks, 0)
        self.assertEqual(self.seen_by_engine, [])
        self.assertEqual(self.manager.store.job(queued["id"])["state"], "cancelled")

    def test_running_cancel_wins_when_engine_returns_late(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def late_success(_executable, assets, _run_dir, _ctx):
            entered.set()
            release.wait(timeout=2)
            return fake_result(assets)

        self.engine.synchronize = late_success
        job = self.manager.submit(self.project["id"])
        self.assertTrue(entered.wait(timeout=2))
        self.manager.store.cancel(job["id"])
        release.set()
        self.assertEqual(self.wait_for(job["id"])["state"], "cancelled")
        with self.assertRaises(KeyError):
            self.manager.store.result(self.project["id"])
        deadline = time.monotonic() + 2
        while self.manager.pending.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.manager.pending.unfinished_tasks, 0)
        self.assertFalse((self.manager.workspace / "runs" / job["id"]).exists())

    def test_content_duplicates_are_analyzed_once(self):
        (self.media / "A-copy.mov").write_bytes((self.media / "A.mov").read_bytes())
        job = self.manager.submit(self.project["id"])
        self.assertEqual(self.wait_for(job["id"])["state"], "needs_review")
        self.assertEqual(len(self.seen_by_engine), 1)
        self.assertEqual(len(self.seen_by_engine[0]), 2)
        self.assertTrue(any(event["level"] == "warning" for event in self.manager.store.events(job["id"])))

    def test_timeout_and_unexpected_engine_exception_do_not_kill_queue(self):
        outcomes = [TaskError("PROCESS_TIMEOUT", "超时。"), RuntimeError("private-local-path-in-exception"), None]

        def fail_then_pass(_executable, assets, _run_dir, _ctx):
            outcome = outcomes.pop(0)
            if outcome:
                raise outcome
            return fake_result(assets)

        self.engine.synchronize = fail_then_pass
        first = self.manager.submit(self.project["id"])
        self.assertEqual(self.wait_for(first["id"])["error_code"], "PROCESS_TIMEOUT")
        second = self.manager.submit(self.project["id"], retry_of=first["id"])
        failure = self.wait_for(second["id"])
        self.assertEqual(failure["error_code"], "WORKER_ERROR")
        self.assertNotIn("private-local-path-in-exception", failure["error_message"])
        third = self.manager.submit(self.project["id"], retry_of=second["id"])
        self.assertEqual(self.wait_for(third["id"])["state"], "needs_review")
        self.assertTrue(self.manager.thread.is_alive())

    def test_run_directory_failure_does_not_kill_queue(self):
        first = self.manager.store.create_job(self.project["id"])
        existing = self.manager.workspace / "runs" / first["id"]
        existing.mkdir(parents=True)
        (existing / "keep.txt").write_text("preexisting data")
        self.manager.pending.put(first["id"])
        failure = self.wait_for(first["id"], timeout=1)
        self.assertEqual(failure["state"], "failed")
        self.assertEqual((existing / "keep.txt").read_text(), "preexisting data")
        second = self.manager.submit(self.project["id"], retry_of=first["id"])
        self.assertEqual(self.wait_for(second["id"])["state"], "needs_review")
        self.assertTrue(self.manager.thread.is_alive())

    def test_export_rechecks_approved_sources_before_calling_exporter(self):
        sync = self.manager.submit(self.project["id"])
        self.assertEqual(self.wait_for(sync["id"])["state"], "needs_review")
        result = self.manager.store.result(self.project["id"])
        for clip in result["clips"]:
            result = self.manager.store.review(self.project["id"], revision=result["revision"],
                                               clip_id=clip["id"], status="approved", reason="合成数据人工确认。")
        called = []
        self.engine.export_fcpxml = lambda *args: called.append(args)
        (self.media / "A.mov").write_bytes(b"source changed after review")
        export = self.manager.submit(self.project["id"], kind="export", revision=result["revision"], format="fcpxml")
        failed = self.wait_for(export["id"])
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(failed["error_code"], "SOURCE_CHANGED")
        self.assertIsNone(failed["artifact"])
        self.assertEqual(called, [])
        self.assertEqual(self.manager.store.result(self.project["id"]), result)

    def test_export_after_failed_rescan_uses_current_verified_binding(self):
        sync = self.manager.submit(self.project["id"])
        self.assertEqual(self.wait_for(sync["id"])["state"], "needs_review")
        result = self.manager.store.result(self.project["id"])
        for clip in result["clips"]:
            result = self.manager.store.review(self.project["id"], revision=result["revision"],
                                               clip_id=clip["id"], status="approved", reason="已核对合成原始结果。")
        original = self.media / "A.mov"
        moved = self.media / "0-A-copy.mov"
        identity = "sha256:" + hashlib.sha256(original.read_bytes()).hexdigest()
        raw_path = self.manager.workspace / "runs" / sync["id"] / "align-result.json"
        raw_path.write_text(json.dumps({"project": {"clips": [{"id": identity, "url": str(original)}]}}))
        original_raw = raw_path.read_bytes()
        moved.write_bytes(original.read_bytes())

        def fail_after_new_binding(_executable, _assets, _run_dir, _ctx):
            raise TaskError("PROCESS_TIMEOUT", "重扫同步失败，保留原核对结果。")

        self.engine.synchronize = fail_after_new_binding
        rescan = self.manager.submit(self.project["id"])
        self.assertEqual(self.wait_for(rescan["id"])["error_code"], "PROCESS_TIMEOUT")
        self.assertEqual(self.manager.store.checked_binding(self.project["id"], identity), moved)
        self.assertEqual(self.manager.store.result(self.project["id"]), result)
        received_paths = {}

        def export_current(_executable, raw, _result, output_dir, _ctx, *, verified_paths):
            self.assertEqual(Path(raw).read_bytes(), original_raw)
            received_paths.update({key: Path(value) for key, value in verified_paths.items()})
            output_dir.mkdir()
            candidate = output_dir / "candidate.fcpxml"
            candidate.write_text('<fcpxml version="1.11"/>')
            return candidate

        self.engine.export_fcpxml = export_current
        export = self.manager.submit(self.project["id"], kind="export", revision=result["revision"], format="fcpxml")
        finished = self.wait_for(export["id"], states=("succeeded", "failed", "cancelled"))
        self.assertEqual(finished["state"], "succeeded", finished)
        self.assertEqual(received_paths[identity], moved)
        self.assertNotEqual(received_paths[identity], original)
        self.assertEqual(set(received_paths), {clip["media_id"] for clip in result["clips"]})
        self.assertTrue((self.manager.workspace / "runs" / export["id"] / finished["artifact"]).is_file())
        self.assertEqual(raw_path.read_bytes(), original_raw)

    def test_source_mutated_during_export_cannot_publish_artifact(self):
        sync = self.manager.submit(self.project["id"])
        self.assertEqual(self.wait_for(sync["id"])["state"], "needs_review")
        result = self.manager.store.result(self.project["id"])
        for clip in result["clips"]:
            result = self.manager.store.review(self.project["id"], revision=result["revision"],
                                               clip_id=clip["id"], status="approved", reason="导出前合成数据核对。")
        produced = []

        def mutate_while_exporting(_executable, _raw, _result, output_dir, _ctx, *, verified_paths):
            output_dir.mkdir()
            candidate = output_dir / "must-not-be-delivered.fcpxml"
            candidate.write_text('<fcpxml version="1.11"/>')
            produced.append(candidate)
            source = Path(next(iter(verified_paths.values())))
            source.write_bytes(b"source changed while exporter was running")
            return candidate

        self.engine.export_fcpxml = mutate_while_exporting
        export = self.manager.submit(self.project["id"], kind="export", revision=result["revision"], format="fcpxml")
        finished = self.wait_for(export["id"], states=("succeeded", "failed", "cancelled"))
        self.assertEqual(finished["state"], "failed", finished)
        self.assertEqual(finished["error_code"], "SOURCE_CHANGED")
        self.assertIsNone(finished["artifact"])
        self.assertEqual(len(produced), 1)
        deadline = time.monotonic() + 2
        while self.manager.pending.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.manager.pending.unfinished_tasks, 0)
        self.assertFalse(produced[0].exists())
        self.assertFalse((self.manager.workspace / "runs" / export["id"]).exists())
        self.assertEqual(self.manager.store.result(self.project["id"]), result)

    def test_second_manager_cannot_recover_live_jobs(self):
        queued = self.manager.store.create_job(self.project["id"])
        with self.assertRaises(RuntimeError):
            JobManager(self.manager.workspace)
        self.assertEqual(self.manager.store.job(queued["id"])["state"], "queued")

    def test_workspace_lock_excludes_another_process(self):
        source = Path(__file__).resolve().parents[1] / "src"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(source) + os.pathsep + environment.get("PYTHONPATH", "")
        script = (
            "import sys; from frameflow.jobs import WorkspaceLock\n"
            "try:\n lock=WorkspaceLock(sys.argv[1])\n"
            "except RuntimeError:\n raise SystemExit(23)\n"
            "else:\n lock.close(); raise SystemExit(0)\n"
        )
        result = subprocess.run([sys.executable, "-c", script, str(self.manager.workspace)], env=environment,
                                capture_output=True, timeout=5, check=False)
        self.assertEqual(result.returncode, 23, result.stderr.decode())
        other = self.root / "another-state"
        other.mkdir()
        lock = WorkspaceLock(other)
        lock.close()
        reopened = WorkspaceLock(other)
        reopened.close()


if __name__ == "__main__":
    unittest.main()
