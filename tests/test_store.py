from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from fractions import Fraction
import json
from pathlib import Path
import tempfile
import threading
import unittest

from frameflow.errors import Conflict, TaskError
from frameflow.store import Store


def candidate_result():
    return {
        "groups": [{"id": "group-1", "name": "同一拍摄"}],
        "clips": [
            {"id": "clip-A", "media_id": "sha256:a", "name": "A.mov", "status": "candidate", "offset_seconds": 0.0,
             "group_id": "group-1", "evidence": ["audio"], "duration": 2.0},
            {"id": "clip-B", "media_id": "sha256:b", "name": "B.wav", "status": "unmatched", "offset_seconds": None,
             "group_id": None, "evidence": [], "duration": 2.0},
        ],
    }


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.media = self.root / "media"
        self.media.mkdir()
        self.store = Store(self.root / "state.sqlite3")
        self.project = self.store.create_project("测试项目", [{"path": str(self.media), "label": "A", "kind": "camera"}])
        self.project_id = self.project["id"]

    def publish(self):
        job = self.store.create_job(self.project_id)
        self.assertTrue(self.store.start_job(job["id"]))
        self.store.finish(job["id"], "needs_review", result=candidate_result())
        return job, self.store.result(self.project_id)

    def test_undo_last_review_restores_offset_but_never_auto_approves(self):
        _, baseline = self.publish()
        approved = self.store.review(
            self.project_id, revision=baseline["revision"], clip_id="clip-A",
            status="approved", offset_seconds=0.0, reason="合成基线已听看。")
        changed = self.store.review(
            self.project_id, revision=approved["revision"], clip_id="clip-A",
            status="proposed", offset_seconds=0.37, reason="开始人工偏移修正。")
        self.assertEqual(self.store.last_undoable_clip(self.project_id, changed["revision"], changed["job_id"]), "clip-A")
        with self.assertRaises(Conflict):
            self.store.undo_last_review(self.project_id, revision=approved["revision"], clip_id="clip-A")
        with self.assertRaises(Conflict):
            self.store.undo_last_review(self.project_id, revision=changed["revision"], clip_id="clip-B")
        undone = self.store.undo_last_review(
            self.project_id, revision=changed["revision"], clip_id="clip-A")
        first = next(c for c in undone["clips"] if c["id"] == "clip-A")
        self.assertEqual(first["offset_seconds"], 0.0)
        self.assertEqual(first["status"], "proposed")
        self.assertEqual(undone["revision"], changed["revision"] + 1)
        self.assertIsNone(self.store.last_undoable_clip(self.project_id, undone["revision"], undone["job_id"]))
        self.assertEqual(self.store.job(undone["job_id"])["state"], "needs_review")
        self.assertIn("撤销最近一次修订", self.store.review_history(self.project_id)[-1]["reason"])
        with self.assertRaises(Conflict):
            self.store.undo_last_review(self.project_id, revision=undone["revision"], clip_id="clip-A")

    def test_markers_are_project_scoped_and_never_change_review_revision(self):
        _, result = self.publish()
        marker = self.store.add_marker(
            self.project_id, group_id="group-1", time_seconds=1.125, label="拍板")
        self.assertEqual(marker["time_seconds"], 1.125)
        self.assertEqual(len(self.store.markers(self.project_id)), 1)
        self.assertEqual(self.store.result(self.project_id)["revision"], result["revision"])
        with self.assertRaises(ValueError):
            self.store.add_marker(self.project_id, group_id="unknown", time_seconds=0)
        with self.assertRaises(ValueError):
            self.store.add_marker(self.project_id, group_id="group-1", time_seconds=float("nan"))
        other = self.store.create_project("隔离项目", [{
            "path": str(self.media), "kind": "camera", "label": "B"}])
        self.assertEqual(self.store.markers(other["id"]), [])
        with self.assertRaises(KeyError):
            self.store.delete_marker(other["id"], marker["id"])
        self.store.delete_marker(self.project_id, marker["id"])
        self.assertEqual(self.store.markers(self.project_id), [])

    def test_queued_cancellation_cannot_start_or_publish(self):
        job = self.store.create_job(self.project_id)
        self.store.cancel(job["id"])
        self.assertFalse(self.store.start_job(job["id"]))
        self.store.finish(job["id"], "succeeded", artifact="should-not-exist.fcpxml")
        latest = self.store.job(job["id"])
        self.assertEqual(latest["state"], "cancelled")
        self.assertIsNone(latest["artifact"])
        with self.assertRaises(KeyError):
            self.store.result(self.project_id)

    def test_running_cancel_wins_over_late_result_and_preserves_previous(self):
        _, previous = self.publish()
        job = self.store.create_job(self.project_id)
        self.store.start_job(job["id"])
        self.assertEqual(self.store.cancel(job["id"])["state"], "cancelling")
        self.store.finish(job["id"], "needs_review", result=candidate_result())
        self.assertEqual(self.store.job(job["id"])["state"], "cancelled")
        self.assertEqual(self.store.result(self.project_id), previous)

    def test_failure_does_not_replace_previous_reviewable_result(self):
        _, previous = self.publish()
        job = self.store.create_job(self.project_id)
        self.store.start_job(job["id"])
        self.store.finish(job["id"], "failed", error=TaskError("PROCESS_TIMEOUT", "处理超时。"))
        self.assertEqual(self.store.result(self.project_id), previous)
        self.assertEqual(self.store.job(job["id"])["error_code"], "PROCESS_TIMEOUT")

    def test_retry_keeps_original_attempt_and_its_events(self):
        old = self.store.create_job(self.project_id)
        self.store.start_job(old["id"])
        self.store.activity(old["id"], "正在核对音频。")
        self.store.finish(old["id"], "failed", error=TaskError("PROCESS_TIMEOUT", "超时。"))
        events = self.store.events(old["id"])
        retry = self.store.create_job(self.project_id, retry_of=old["id"])
        self.assertNotEqual(retry["id"], old["id"])
        self.assertEqual(retry["attempt"], old["attempt"] + 1)
        self.assertEqual(retry["retry_of"], old["id"])
        self.assertEqual(self.store.job(old["id"])["state"], "failed")
        self.assertEqual(self.store.events(old["id"]), events)
        self.assertGreater(self.store.events(retry["id"])[0]["seq"], events[-1]["seq"])

    def test_restart_recovery_marks_active_attempts_interrupted_once(self):
        job = self.store.create_job(self.project_id)
        self.store.start_job(job["id"])
        reopened = Store(self.store.path)
        reopened.recover()
        self.assertEqual(reopened.job(job["id"])["state"], "interrupted")
        self.assertEqual(reopened.job(job["id"])["error_code"], "WORKER_INTERRUPTED")
        events = reopened.events(job["id"])
        reopened.recover()
        self.assertEqual(reopened.events(job["id"]), events)
        retry = reopened.create_job(self.project_id, retry_of=job["id"])
        self.assertEqual(retry["attempt"], 2)

    def test_two_concurrent_submissions_admit_only_one_active_attempt(self):
        barrier = threading.Barrier(2)

        def submit():
            store = Store(self.store.path)
            barrier.wait(timeout=3)
            try:
                return store.create_job(self.project_id)["id"]
            except Conflict:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sum(value is not None for value in outcomes), 1)
        self.assertEqual(len(self.store.jobs(self.project_id)), 1)

    def test_review_compare_and_swap_preserves_human_reason_and_manual_evidence(self):
        _, result = self.publish()
        updated = self.store.review(self.project_id, revision=result["revision"], clip_id="clip-B", status="approved",
                                    group_id="group-1", offset_seconds=1.25, reason="已听拍板并检查口型。")
        clip = next(value for value in updated["clips"] if value["id"] == "clip-B")
        self.assertEqual(updated["revision"], result["revision"] + 1)
        self.assertEqual(clip["offset_seconds"], 1.25)
        self.assertIn("manual", clip["evidence"])
        with self.assertRaises(Conflict):
            self.store.review(self.project_id, revision=result["revision"], clip_id="clip-B", status="excluded", reason="过期页面。")
        history = self.store.review_history(self.project_id)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["reason"], "已听拍板并检查口型。")
        self.assertEqual(self.store.result(self.project_id), updated)

    def test_manual_offsets_update_all_rational_points_without_erasing_original_evidence(self):
        original_mapping = [
            {"source": {"value": 0, "timescale": 1}, "island": {"value": 0, "timescale": 1}},
            {"source": {"value": 1, "timescale": 3}, "island": {"value": 1, "timescale": 3}},
            {"source": {"value": 2, "timescale": 1}, "island": {"value": 2, "timescale": 1}},
        ]
        proposed = candidate_result()
        proposed["clips"][0]["mapping"] = deepcopy(original_mapping)
        proposed["groups"].append({"id": "group-2", "name": "人工复核组"})
        job = self.store.create_job(self.project_id)
        self.store.start_job(job["id"])
        self.store.finish(job["id"], "needs_review", result=proposed)
        initial = self.store.result(self.project_id)

        def fraction(value):
            return Fraction(value["value"], value["timescale"])

        first = self.store.review(self.project_id, revision=initial["revision"], clip_id="clip-A",
                                  status="approved", offset_seconds=2.25, reason="第一次口型修正。")
        first_clip = first["clips"][0]
        self.assertEqual(first_clip["original_mapping"], original_mapping)
        for original, corrected in zip(original_mapping, first_clip["mapping"]):
            self.assertEqual(corrected["source"], original["source"])
            self.assertEqual(fraction(corrected["island"]), fraction(original["island"]) + Fraction(9, 4))
        second = self.store.review(self.project_id, revision=first["revision"], clip_id="clip-A", status="approved",
                                   offset_seconds=3.5, group_id="group-2", reason="第二次核对拍板。")
        second_clip = second["clips"][0]
        self.assertEqual(second_clip["original_mapping"], original_mapping)
        self.assertEqual(second_clip["suggested_offset_seconds"], 0.0)
        self.assertEqual(second_clip["suggested_group_id"], "group-1")
        for original, corrected in zip(original_mapping, second_clip["mapping"]):
            self.assertEqual(fraction(corrected["island"]), fraction(original["island"]) + Fraction(7, 2))
        self.assertEqual(initial["clips"][0]["mapping"], original_mapping)
        third = self.store.review(self.project_id, revision=second["revision"], clip_id="clip-B", status="approved",
                                  offset_seconds=1.5, group_id="group-2", reason="为未匹配录音补定位。")
        recovered = third["clips"][1]
        self.assertEqual([fraction(point["source"]) for point in recovered["mapping"]], [Fraction(0), Fraction(2)])
        self.assertEqual([fraction(point["island"]) for point in recovered["mapping"]], [Fraction(3, 2), Fraction(7, 2)])
        self.assertEqual(recovered["original_mapping"], [])
        self.assertIsNone(recovered["suggested_offset_seconds"])
        self.assertIsNone(recovered["suggested_group_id"])
        history = self.store.review_history(self.project_id)
        self.assertEqual([entry["revision"] for entry in history], [initial["revision"] + n for n in (1, 2, 3)])
        self.assertEqual([entry["reason"] for entry in history], ["第一次口型修正。", "第二次核对拍板。", "为未匹配录音补定位。"])
        with self.store.connect() as db:
            audit = db.execute("SELECT previous_json,replacement_json FROM reviews WHERE project_id=? ORDER BY seq", (self.project_id,)).fetchall()
        self.assertEqual(json.loads(audit[0]["previous_json"])["mapping"], original_mapping)
        self.assertEqual(json.loads(audit[0]["replacement_json"])["mapping"], first_clip["mapping"])
        self.assertEqual(json.loads(audit[1]["previous_json"])["mapping"], first_clip["mapping"])
        self.assertEqual(json.loads(audit[1]["replacement_json"])["mapping"], second_clip["mapping"])

    def test_pending_correction_is_audited_and_reopens_export_gate_until_reapproved(self):
        job, result = self.publish()
        result = self.store.review(self.project_id, revision=result["revision"], clip_id="clip-A",
                                   status="approved", reason="已核对合成位置。")
        result = self.store.review(self.project_id, revision=result["revision"], clip_id="clip-B",
                                   status="excluded", reason="本轮无共同内容。")
        self.store.require_exportable(self.project_id)
        previous = deepcopy(result)
        request = dict(revision=result["revision"], clip_id="clip-A", status="proposed",
                       offset_seconds=0.04, reason="先应用一帧修正，等待试听。")
        pending = self.store.review(self.project_id, **request)
        clip = pending["clips"][0]
        self.assertEqual(clip["status"], "proposed")
        self.assertEqual(clip["offset_seconds"], 0.04)
        self.assertEqual(clip["suggested_offset_seconds"], 0.0)
        self.assertIn("manual", clip["evidence"])
        self.assertEqual(pending["revision"], previous["revision"] + 1)
        self.assertEqual(self.store.job(job["id"])["state"], "needs_review")
        self.assertEqual(self.store.job(job["id"])["completed"], 1)
        with self.assertRaises(Conflict):
            self.store.require_exportable(self.project_id)
        with self.assertRaises(Conflict):
            self.store.review(self.project_id, **request)
        with self.store.connect() as db:
            audit = db.execute("SELECT previous_json,replacement_json FROM reviews ORDER BY seq DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(audit["previous_json"]), previous["clips"][0])
        self.assertEqual(json.loads(audit["replacement_json"]), clip)
        reapproved = self.store.review(self.project_id, revision=pending["revision"], clip_id="clip-A",
                                      status="approved", reason="试听后确认。")
        self.assertEqual(reapproved["clips"][0]["mapping"], clip["mapping"])
        self.assertEqual(self.store.require_exportable(self.project_id), reapproved)
        self.assertEqual(self.store.job(job["id"])["state"], "succeeded")

    def test_review_rejects_invalid_values_without_mutation(self):
        _, original = self.publish()
        defaults = {"revision": original["revision"], "clip_id": "clip-A", "status": "approved", "reason": "已核对。"}
        cases = [
            {"reason": " "}, {"reason": "x" * 1001}, {"status": "automatic"},
            {"offset_seconds": float("nan")}, {"offset_seconds": float("inf")},
            {"offset_seconds": 7 * 86400 + 1}, {"group_id": "missing"},
            {"offset_seconds": True}, {"offset_seconds": "1.25"},
            {"revision": True}, {"revision": 1.0},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(ValueError):
                    self.store.review(self.project_id, **(defaults | overrides))
                self.assertEqual(self.store.result(self.project_id), original)
                self.assertEqual(self.store.review_history(self.project_id), [])

    def test_unmatched_and_blocked_clips_cannot_be_approved_without_evidence(self):
        _, result = self.publish()
        with self.assertRaises(ValueError):
            self.store.review(self.project_id, revision=result["revision"], clip_id="clip-B", status="approved",
                              group_id="group-1", reason="尚未找到位置。")
        self.assertEqual(self.store.result(self.project_id), result)
        job = self.store.create_job(self.project_id)
        self.store.start_job(job["id"])
        blocked = candidate_result()
        blocked["clips"][0]["blocked_reason"] = "unsupported_time_mapping"
        self.store.finish(job["id"], "needs_review", result=blocked)
        result = self.store.result(self.project_id)
        with self.assertRaises(ValueError):
            self.store.review(self.project_id, revision=result["revision"], clip_id="clip-A", status="approved", reason="不能忽略漂移。")

    def test_export_gate_requires_current_revision_and_complete_review(self):
        _, result = self.publish()
        with self.assertRaises(Conflict):
            self.store.create_job(self.project_id, kind="export", revision=result["revision"], format="fcpxml")
        result = self.store.review(self.project_id, revision=result["revision"], clip_id="clip-A", status="approved", reason="已核对。")
        result = self.store.review(self.project_id, revision=result["revision"], clip_id="clip-B", status="excluded", reason="没有匹配证据。")
        with self.assertRaises(Conflict):
            self.store.create_job(self.project_id, kind="export", revision=result["revision"] - 1, format="fcpxml")
        export = self.store.create_job(self.project_id, kind="export", revision=result["revision"], format="fcpxml")
        self.assertEqual(export["source_revision"], result["revision"])

    def test_public_jobs_assets_and_results_have_no_local_path(self):
        private_path = str(self.media / "A.mov")
        Path(private_path).write_bytes(b"synthetic media identity")
        asset = {"id": "sha256:a", "media_id": "sha256:a", "name": "A.mov", "camera": "A", "kind": "camera", "local_path": private_path}
        self.store.save_asset(self.project_id, asset)
        job, result = self.publish()
        with self.store.connect() as db:
            metadata = json.loads(db.execute("SELECT metadata_json FROM assets").fetchone()[0])
        public = json.dumps([self.store.job(job["id"]), metadata, result])
        self.assertNotIn(str(self.root), public)
        self.assertNotIn("local_path", public)
        self.assertNotIn("input_roots", public)
        self.assertEqual(self.store.binding(self.project_id, "sha256:a"), Path(private_path))
        moved = self.media / "relocated.mov"
        Path(private_path).rename(moved)
        self.store.save_asset(self.project_id, asset | {"local_path": str(moved)})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM assets").fetchone()[0], 1)
        self.assertEqual(self.store.binding(self.project_id, "sha256:a"), moved)

    def test_heartbeat_does_not_claim_forward_progress(self):
        job = self.store.create_job(self.project_id)
        self.store.start_job(job["id"])
        self.store.progress(job["id"], "sync", 0, None, "tasks", "A.mov", "正在计算。")
        before = self.store.job(job["id"])
        self.store.heartbeat(job["id"])
        after = self.store.job(job["id"])
        self.assertEqual(after["progress_at"], before["progress_at"])
        self.assertEqual(after["completed"], 0)
        self.assertIsNone(after["total"])
        self.assertGreaterEqual(after["heartbeat_at"], before["heartbeat_at"])

    def test_checked_binding_blocks_changed_and_offline_media(self):
        path = self.media / "protected.mov"
        path.write_bytes(b"synthetic original")
        asset = {"id": "sha256:protected", "media_id": "sha256:protected", "name": path.name,
                 "camera": "A", "kind": "camera", "local_path": str(path)}
        self.store.save_asset(self.project_id, asset)
        self.assertEqual(self.store.checked_binding(self.project_id, asset["media_id"]), path)
        path.write_bytes(b"different source content")
        with self.assertRaises(TaskError) as error:
            self.store.checked_binding(self.project_id, asset["media_id"])
        self.assertEqual(error.exception.code, "SOURCE_CHANGED")
        path.unlink()
        with self.assertRaises(TaskError) as error:
            self.store.checked_binding(self.project_id, asset["media_id"])
        self.assertEqual(error.exception.code, "MEDIA_OFFLINE")


if __name__ == "__main__":
    unittest.main()
