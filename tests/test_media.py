from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave

from frameflow.errors import TaskCancelled, TaskError
from frameflow.media import probe_and_hash, scan_roots


class Context:
    def __init__(self):
        self.events = []
        self.cancelled = False
        self.on_progress = None

    def check_cancel(self):
        if self.cancelled:
            raise TaskCancelled()

    def heartbeat(self):
        pass

    def progress(self, stage, completed, total=None, **kwargs):
        self.events.append((stage, completed, total, kwargs))
        if self.on_progress:
            self.on_progress(stage, completed, total)

    def activity(self, message, **kwargs):
        self.events.append(("activity", message, kwargs))


def make_wave(path: Path, frames=16000):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x01\x00" * frames)


def item(path: Path):
    return {"path": path, "camera": "录音", "kind": "audio"}


class MediaTests(unittest.TestCase):
    def test_scan_includes_independent_audio_and_video_with_unknown_total(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "A.mov").write_bytes(b"video to probe later")
            (root / "audio").mkdir()
            make_wave(root / "audio" / "recorder.WAV")
            (root / ".ignored.mov").write_bytes(b"hidden")
            (root / "notes.txt").write_text("not media")
            context = Context()
            result = scan_roots([{"path": root, "label": "A", "kind": "video"}], context)
            self.assertEqual({entry["path"].name for entry in result}, {"A.mov", "recorder.WAV"})
            scans = [event for event in context.events if event[0] == "scan"]
            self.assertTrue(all(event[2] is None for event in scans[:-1]))
            self.assertEqual(scans[-1][1:3], (2, 2))

    def test_missing_empty_and_overlapping_roots_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for roots, code in [
                ([{"path": root / "missing"}], "ROOT_MISSING"),
                ([{"path": root}], "NO_MEDIA"),
                ([{"path": root}, {"path": root}], "ROOTS_OVERLAP"),
            ]:
                with self.subTest(code=code), self.assertRaises(TaskError) as error:
                    scan_roots(roots, Context())
                self.assertEqual(error.exception.code, code)

    def test_symlink_cannot_hide_a_partial_scan(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_wave(root / "audio.wav")
            linked = root / ".hidden-link.wav"
            try:
                linked.symlink_to(root / "audio.wav")
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable on this host")
            with self.assertRaises(TaskError) as error:
                scan_roots([{"path": root}], Context())
            self.assertEqual(error.exception.code, "SYMLINK_NOT_ALLOWED")
            with self.assertRaises(TaskError) as error:
                scan_roots([{"path": linked}], Context())
            self.assertEqual(error.exception.code, "SYMLINK_NOT_ALLOWED")

    def test_inaccessible_subdirectory_does_not_publish_partial_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            make_wave(root / "readable.wav")
            denied = root / "denied"
            denied.mkdir()
            original = os.scandir

            def scandir(path):
                if Path(path) == denied:
                    raise PermissionError("test denied access")
                return original(path)

            with patch("frameflow.media.os.scandir", side_effect=scandir):
                with self.assertRaises(TaskError) as error:
                    scan_roots([{"path": root}], Context())
            self.assertEqual(error.exception.code, "ROOT_UNREADABLE")

    @unittest.skipUnless(shutil.which("ffprobe"), "real media probe requires ffprobe")
    def test_real_wav_metadata_and_full_hash_survive_a_new_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "recording.wav"
            make_wave(path, frames=32000)
            expected = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            result = probe_and_hash(item(path), Context())
            self.assertEqual(result["id"], expected)
            self.assertEqual(result["media_id"], expected)
            self.assertTrue(result["has_audio"])
            self.assertFalse(result["has_video"])
            self.assertEqual(result["audio_channels"], 1)
            self.assertIsNone(result["fps"])
            self.assertAlmostEqual(result["duration"], 2, places=5)
            renamed = path.with_name("renamed.wav")
            path.rename(renamed)
            moved = probe_and_hash(item(renamed), Context())
            self.assertEqual(moved["id"], expected)
            self.assertNotEqual(moved["local_path"], result["local_path"])

    def test_rational_rate_and_timecode_are_preserved_from_tool_metadata(self):
        metadata = {
            "format": {"duration": "2.5"},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001", "tags": {"timecode": "01:02:03;04"}},
                {"codec_type": "audio", "channels": 2},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.mov"
            path.write_bytes(b"synthetic bytes for metadata contract test")
            with patch("frameflow.media.run_command", return_value=json.dumps(metadata)):
                result = probe_and_hash({"path": path, "camera": "A", "kind": "video"}, Context())
            self.assertEqual(result["fps"], "30000/1001")
            self.assertEqual(result["timecode"], "01:02:03;04")
            self.assertEqual(result["audio_channels"], 2)

    def test_missing_duration_is_never_fabricated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.wav"
            path.write_bytes(b"test")
            with patch("frameflow.media.run_command", return_value='{"streams":[{"codec_type":"audio"}]}'):
                with self.assertRaises(TaskError) as error:
                    probe_and_hash(item(path), Context())
            self.assertEqual(error.exception.code, "INVALID_MEDIA")

    def test_hash_observes_middle_bytes_and_can_be_cancelled(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "long.wav"
            path.write_bytes(b"x" * (3 * 1024 * 1024))
            metadata = '{"format":{"duration":"2"},"streams":[{"codec_type":"audio","channels":1}]}'
            with patch("frameflow.media.run_command", return_value=metadata):
                first = probe_and_hash(item(path), Context())
                with path.open("r+b") as output:
                    output.seek(1536 * 1024)
                    output.write(b"y")
                second = probe_and_hash(item(path), Context())
                self.assertNotEqual(first["id"], second["id"])
                context = Context()
                context.on_progress = lambda stage, done, total: setattr(context, "cancelled", stage == "hash" and done > 0)
                with self.assertRaises(TaskCancelled):
                    probe_and_hash(item(path), context)

    def test_changing_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "changing.wav"
            make_wave(path)
            context = Context()
            changed = False

            def change(stage, done, total):
                nonlocal changed
                if stage == "hash" and done > 0 and not changed:
                    changed = True
                    with path.open("ab") as output:
                        output.write(b"changed")

            context.on_progress = change
            metadata = '{"format":{"duration":"1"},"streams":[{"codec_type":"audio","channels":1}]}'
            with patch("frameflow.media.run_command", return_value=metadata):
                with self.assertRaises(TaskError) as error:
                    probe_and_hash(item(path), context)
            self.assertEqual(error.exception.code, "SOURCE_CHANGED")


if __name__ == "__main__":
    unittest.main()
