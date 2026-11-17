"""Contract and fail-closed tests; all media and paths are generated locally."""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

from frameflow import engine
from frameflow.errors import TaskCancelled, TaskError


class Context:
    def __init__(self):
        self.events = []
        self.cancelled = False

    def check_cancel(self):
        if self.cancelled:
            raise TaskCancelled()

    def heartbeat(self):
        pass

    def progress(self, stage, done, total=None, **fields):
        self.events.append({"stage": stage, "done": done, "total": total, **fields})

    def activity(self, message, **fields):
        self.events.append({"message": message, **fields})


def time_value(seconds):
    return {"value": round(seconds * 1_000_000), "timescale": 1_000_000}


def fixtures(root):
    assets = []
    for letter in ("A", "B"):
        assets.append({"id": "sha256:" + letter * 64, "media_id": "sha256:" + letter * 64, "name": f"{letter} take.wav", "local_path": str(root / f"{letter} take.wav"), "kind": "recorder", "camera": letter, "duration": 30.0, "fps": None, "has_video": False, "has_audio": True, "width": 0, "height": 0, "audio_channels": 1})
    raw = {
        "project": {"clips": [{"id": f"engine-{i}", "url": asset["local_path"], "kind": "audio", "duration": time_value(30), "audio": [{"sampleRate": 16000, "channels": 1, "bitDepth": 16, "isFloat": False, "sourceTimecode": None}], "video": None, "recordedAt": None, "recordedAtSource": None, "sourceIdentifier": None, "mediaSpan": None} for i, asset in enumerate(assets)], "warnings": [], "importedTimeline": None},
        "islands": [{"id": 0, "placements": [{"clipID": f"engine-{i}", "confidence": 0.8, "mapping": {"points": [{"source": time_value(0), "island": time_value(i * 1.25)}, {"source": time_value(30), "island": time_value(30 + i * 1.25)}]}} for i in range(2)]}],
        "unmatched": [],
        "matches": [{"left": "engine-0", "right": "engine-1", "driftPPM": 0.0, "offset": time_value(1.25), "confidence": 0.8, "anchors": 8, "covered": time_value(25), "residual": time_value(0.0001), "evidence": "waveform"}],
        "temporalPolicy": {"default": "auto", "modes": {}},
    }
    return assets, raw


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets, self.raw = fixtures(self.root)
        self.verified_paths = {asset["media_id"]: Path(asset["local_path"]) for asset in self.assets}
        for path in self.verified_paths.values():
            path.write_bytes(b"synthetic media placeholder: " + path.name.encode())
        self.ctx = Context()
        self.info = {"available": True, "version": "0.3.3", "message": "ready", "executable": "align-cli"}

    def normalised(self):
        return engine._normalise(self.raw, self.assets)

    def approved(self):
        result = self.normalised()
        for clip in result["clips"]:
            clip["status"] = "approved"
        return result

    def raw_file(self):
        path = self.root / "raw.json"
        path.write_text(json.dumps(self.raw), encoding="utf-8")
        return path

    def test_version_is_exact_and_missing_is_readable(self):
        with patch.object(engine.shutil, "which", return_value=None):
            self.assertFalse(engine.inspect_engine("missing")["available"])
        with patch.object(engine.shutil, "which", return_value="align-cli"):
            for version, supported in [("align-cli 0.3.3\n", True), ("align-cli 0.3.4\n", False), ("untrusted banner 0.3.3\n", False)]:
                with self.subTest(version=version), patch.object(engine.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, version, "")):
                    self.assertEqual(engine.inspect_engine("align-cli")["available"], supported)

    def test_sync_uses_real_progress_and_commits_only_complete_json(self):
        def run(argv, context, **kwargs):
            self.assertIsInstance(argv, list)
            self.assertEqual(argv[-2:], [a["local_path"] for a in self.assets])
            kwargs["on_line"]("fingerprint 1/2 A take.wav")
            kwargs["on_line"]("error: private diagnostics must not become public activity")
            kwargs["stdout_path"].write_text(json.dumps(self.raw), encoding="utf-8")
            return ""
        with patch.object(engine, "_require_engine", return_value=self.info), patch.object(engine, "run_command", side_effect=run):
            result = engine.synchronize("align-cli", self.assets, self.root / "run", self.ctx)
        self.assertTrue((self.root / "run/align-result.json").is_file())
        self.assertFalse((self.root / "run/align-result.json.part").exists())
        self.assertEqual([c["id"] for c in result["clips"]], [a["media_id"] for a in self.assets])
        self.assertEqual({c["status"] for c in result["clips"]}, {"proposed"})
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertTrue(any(e.get("current_item") == "A take.wav" and e.get("done") == 1 for e in self.ctx.events))
        self.assertNotIn("private diagnostics", json.dumps(self.ctx.events))

    def test_bad_json_and_cancellation_cannot_publish_result(self):
        def run(argv, context, **kwargs):
            kwargs["stdout_path"].write_text('{"project":', encoding="utf-8")
            return ""
        with patch.object(engine, "_require_engine", return_value=self.info), patch.object(engine, "run_command", side_effect=run):
            with self.assertRaises(TaskError):
                engine.synchronize("align-cli", self.assets, self.root / "invalid", self.ctx)
        self.assertEqual(list((self.root / "invalid").iterdir()), [])

        def cancel(argv, context, **kwargs):
            kwargs["stdout_path"].write_text(json.dumps(self.raw), encoding="utf-8")
            context.cancelled = True
            return ""
        with patch.object(engine, "_require_engine", return_value=self.info), patch.object(engine, "run_command", side_effect=cancel):
            with self.assertRaises(TaskCancelled):
                engine.synchronize("align-cli", self.assets, self.root / "cancelled", self.ctx)
        self.assertEqual(list((self.root / "cancelled").iterdir()), [])

    def test_engine_omission_and_singletons_stay_visible_unmatched(self):
        self.raw["project"]["clips"] = self.raw["project"]["clips"][:1]
        self.raw["islands"][0]["placements"] = self.raw["islands"][0]["placements"][:1]
        self.raw["matches"] = []
        result = self.normalised()
        self.assertEqual(len(result["clips"]), 2)
        self.assertEqual(result["groups"], [])
        self.assertEqual({c["status"] for c in result["clips"]}, {"unmatched"})
        self.assertIsNone(result["clips"][1]["engine_clip_id"])
        self.assertTrue(result["warnings"])

    def test_decode_failure_has_actionable_reason_without_private_path(self):
        def fail(argv, context, **kwargs):
            kwargs["on_line"]("error: Cannot decode audio for drift correction: " + str(self.root / "private.wav"))
            raise TaskError("PROCESS_FAILED", "exit 1")
        with patch.object(engine, "_require_engine", return_value=self.info), patch.object(engine, "run_command", side_effect=fail), self.assertRaises(TaskError) as caught:
            engine.synchronize("align-cli", self.assets, self.root / "run", self.ctx)
        self.assertEqual(caught.exception.code, "ENGINE_AUDIO_DECODE_FAILED")
        self.assertNotIn(str(self.root), caught.exception.message)

    def test_unknown_evidence_and_weak_waveform_do_not_create_a_group(self):
        for changes in [{"evidence": None}, {"anchors": 1}, {"residual": time_value(0.2)}]:
            with self.subTest(changes=changes):
                raw = deepcopy(self.raw)
                raw["matches"][0].update(changes)
                result = engine._normalise(raw, self.assets)
                self.assertEqual(result["groups"], [])
                self.assertTrue(all(c["status"] == "unmatched" for c in result["clips"]))

    def test_timecode_only_is_a_reviewable_proposal_not_approval(self):
        self.raw["matches"][0].update(evidence="timecode", anchors=1)
        result = self.normalised()
        self.assertEqual(result["clips"][0]["status"], "proposed")
        self.assertEqual(result["clips"][0]["evidence"], ["timecode"])
        self.assertIn("必须人工核对", result["clips"][0]["reason"])

    def test_nonlinear_map_is_preserved_and_never_approximated(self):
        points = self.raw["islands"][0]["placements"][1]["mapping"]["points"]
        points.insert(1, {"source": time_value(15), "island": time_value(16.4)})
        clip = self.normalised()["clips"][1]
        self.assertEqual(clip["blocked_reason"], "NONLINEAR_MAPPING")
        self.assertIsNone(clip["offset_seconds"])
        self.assertEqual(clip["mapping"], points)

    def test_unenrolled_paths_and_contradictory_states_are_rejected(self):
        raw = deepcopy(self.raw)
        raw["project"]["clips"][0]["url"] = str(self.root / "not-selected.wav")
        with self.assertRaises(TaskError):
            engine._normalise(raw, self.assets)
        self.raw["unmatched"] = ["engine-0"]
        with self.assertRaises(TaskError):
            self.normalised()

    def test_warnings_do_not_publish_private_paths(self):
        self.raw["project"]["warnings"] = [{"url": self.assets[0]["local_path"], "message": "Cannot decode audio: " + str(self.root / "private" / "actor.wav")}]
        result = self.normalised()
        self.assertIn("音频解码异常", result["warnings"][0])
        self.assertNotIn(str(self.root), json.dumps(result))

    def test_export_updates_manual_offset_and_excludes_explicitly(self):
        result = self.approved()
        result["clips"][0]["status"] = "excluded"
        result["clips"][1]["offset_seconds"] = 2.5
        source = self.raw_file()
        original = source.read_bytes()
        target = self.root / "delivery"

        def run(argv, context, **kwargs):
            self.assertIn("--no-drift", argv)
            self.assertEqual(argv[argv.index("--unmatched") + 1], "remove")
            raw = json.loads(Path(argv[-2]).read_text(encoding="utf-8"))
            self.assertEqual([c["id"] for c in raw["project"]["clips"]], ["engine-1"])
            point = raw["islands"][0]["placements"][0]["mapping"]["points"][0]["island"]
            self.assertEqual(point["value"] / point["timescale"], 2.5)
            output = Path(argv[-1]) / "arbitrary export name.fcpxml"
            output.write_text("<fcpxml/>", encoding="utf-8")
            return json.dumps([{"format": "finalCutProXML", "url": str(output)}])

        with patch.object(engine, "_require_engine", return_value=self.info), patch.object(engine, "run_command", side_effect=run):
            exported = engine.export_fcpxml("align-cli", source, result, target, self.ctx,
                                            verified_paths=self.verified_paths)
        self.assertTrue(exported.is_file())
        self.assertEqual(source.read_bytes(), original)

    def test_pending_review_multiple_groups_and_drift_block_export(self):
        source = self.raw_file()
        with patch.object(engine, "_require_engine", return_value=self.info):
            with self.assertRaises(TaskError) as caught:
                engine.export_fcpxml("align-cli", source, self.normalised(), self.root / "out", self.ctx)
            self.assertEqual(caught.exception.code, "REVIEW_REQUIRED")
            result = self.approved()
            result["groups"].append({"id": "another", "name": "another"})
            result["clips"][1]["group_id"] = "another"
            with self.assertRaises(TaskError) as caught:
                engine.export_fcpxml("align-cli", source, result, self.root / "out", self.ctx)
            self.assertEqual(caught.exception.code, "MULTIPLE_GROUPS_EXPORT_PENDING")
            result = self.approved()
            result["clips"][1]["rate"] = 1.00001
            with self.assertRaises(TaskError) as caught:
                engine.export_fcpxml("align-cli", source, result, self.root / "out", self.ctx)
            self.assertEqual(caught.exception.code, "DRIFT_EXPORT_UNVERIFIED")

    def test_display_rate_cannot_hide_immutable_drift(self):
        self.raw["islands"][0]["placements"][1]["mapping"]["points"][-1]["island"] = time_value(31.253)
        result = self.approved()
        self.assertIn("时钟漂移", result["clips"][1]["reason"])
        result["clips"][1]["rate"] = 1.0
        with patch.object(engine, "_require_engine", return_value=self.info), self.assertRaises(TaskError) as caught:
            engine.export_fcpxml("align-cli", self.raw_file(), result, self.root / "out", self.ctx)
        self.assertEqual(caught.exception.code, "DRIFT_EXPORT_UNVERIFIED")

    def test_export_outside_job_is_rejected_and_partials_removed(self):
        outside = self.root / "outside.fcpxml"
        outside.write_text("outside", encoding="utf-8")
        target = self.root / "delivery"

        def run(argv, context, **kwargs):
            (target / "partial.xml").write_text("partial", encoding="utf-8")
            return json.dumps([{"format": "finalCutProXML", "url": str(outside)}])

        with patch.object(engine, "_require_engine", return_value=self.info), patch.object(engine, "run_command", side_effect=run), self.assertRaises(TaskError) as caught:
            engine.export_fcpxml("align-cli", self.raw_file(), self.approved(), target, self.ctx,
                                  verified_paths=self.verified_paths)
        self.assertEqual(caught.exception.code, "EXPORT_PATH_INVALID")
        self.assertFalse(target.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_export_requires_all_current_regular_absolute_bindings(self):
        source, result = self.raw_file(), self.approved()
        for bindings, code in [
            (None, "EXPORT_BINDINGS_REQUIRED"),
            ({}, "EXPORT_BINDINGS_REQUIRED"),
            ({**self.verified_paths, self.assets[0]["media_id"]: Path("relative.wav")}, "EXPORT_BINDING_INVALID"),
            ({**self.verified_paths, self.assets[0]["media_id"]: self.root / "missing.wav"}, "EXPORT_BINDING_INVALID"),
            ({**self.verified_paths, self.assets[0]["media_id"]: self.root}, "EXPORT_BINDING_INVALID"),
        ]:
            with self.subTest(code=code, bindings=bindings), patch.object(engine, "_require_engine", return_value=self.info), patch.object(engine, "run_command") as run, self.assertRaises(TaskError) as caught:
                engine.export_fcpxml("align-cli", source, result, self.root / "out", self.ctx,
                                      verified_paths=bindings)
            self.assertEqual(caught.exception.code, code)
            run.assert_not_called()
        if os.name != "nt":
            link = self.root / "linked.wav"
            link.symlink_to(self.verified_paths[self.assets[0]["media_id"]])
            with patch.object(engine, "_require_engine", return_value=self.info), self.assertRaises(TaskError) as caught:
                engine.export_fcpxml("align-cli", source, result, self.root / "out", self.ctx,
                                      verified_paths={**self.verified_paths, self.assets[0]["media_id"]: link})
            self.assertEqual(caught.exception.code, "EXPORT_BINDING_INVALID")

    def test_export_uses_current_binding_without_mutating_historical_raw(self):
        source = self.raw_file()
        original_raw = source.read_bytes()
        first = self.assets[0]
        old_path = Path(first["local_path"])
        rebound = self.root / "preserved-source-copy.wav"
        rebound.write_bytes(old_path.read_bytes())
        old_path.write_bytes(b"different source now occupies the historical URL")
        current = {**self.verified_paths, first["media_id"]: rebound}
        target = self.root / "delivery"

        def run(argv, context, **kwargs):
            reviewed = json.loads(Path(argv[-2]).read_text(encoding="utf-8"))
            exported_clips = {c["id"]: c for c in reviewed["project"]["clips"]}
            self.assertEqual(exported_clips["engine-0"]["url"], str(rebound))
            self.assertEqual(exported_clips["engine-1"]["url"], self.assets[1]["local_path"])
            artifact = target / "result.fcpxml"
            artifact.write_text("<fcpxml/>", encoding="utf-8")
            return json.dumps([{"format": "finalCutProXML", "url": str(artifact)}])

        with patch.object(engine, "_require_engine", return_value=self.info), patch.object(engine, "run_command", side_effect=run):
            exported = engine.export_fcpxml("align-cli", source, self.approved(), target, self.ctx,
                                            verified_paths=current)
        self.assertTrue(exported.is_file())
        self.assertEqual(source.read_bytes(), original_raw)
        self.assertEqual(self.raw["project"]["clips"][0]["url"], str(old_path))

    @unittest.skipUnless(os.environ.get("FRAMEFLOW_TEST_ALIGN"), "Set FRAMEFLOW_TEST_ALIGN to a verified Align 0.3.3 CLI")
    def test_real_align_sync_and_export_synthetic_audio(self):
        executable = os.environ["FRAMEFLOW_TEST_ALIGN"]
        sample_rate = 16000
        rng = random.Random(37128)
        tones = [(rng.uniform(220, 1100), rng.uniform(1500, 2800), rng.uniform(0.3, 0.85)) for _ in range(240)]
        samples = []
        for index in range(sample_rate * 60):
            t = index / sample_rate
            a, b, amplitude = tones[min(239, int(t * 4))]
            envelope = math.sin(math.pi * (t * 4 % 1)) ** 2
            samples.append(round(32767 * amplitude * envelope * (0.42 * math.sin(2 * math.pi * a * t) + 0.24 * math.sin(2 * math.pi * b * t) + 0.05 * rng.uniform(-1, 1))))
        for index, asset in enumerate(self.assets):
            data = samples if index == 0 else [0] * 20000 + samples
            with wave.open(asset["local_path"], "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(sample_rate)
                output.writeframes(struct.pack("<" + "h" * len(data), *data))
            asset["duration"] = len(data) / sample_rate
        result = engine.synchronize(executable, self.assets, self.root / "run", self.ctx)
        self.assertEqual(len(result["groups"]), 1)
        offsets = [c["offset_seconds"] for c in result["clips"]]
        self.assertAlmostEqual(abs(offsets[0] - offsets[1]), 1.25, places=3)
        self.assertTrue(all(c["status"] == "proposed" for c in result["clips"]))
        for clip in result["clips"]:
            clip["status"] = "approved"
        exported = engine.export_fcpxml(executable, self.root / "run/align-result.json", result, self.root / "delivery", self.ctx,
                                        verified_paths=self.verified_paths)
        self.assertIn("<fcpxml", exported.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
