"""Pure handoff gates; these tests do not pretend to run a Resolve SDK."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.import_resolve import prepare


class ResolveDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest_path, self.xml_path = self.root / "manifest.json", self.root / "candidate.fcpxml"
        document = ET.Element("fcpxml", version="1.10")
        resources = ET.SubElement(document, "resources")
        clips = []
        for index, offset in enumerate((0.0, 1.25, 0.0)):
            path = self.root / f"source-{index}.bin"
            data = f"unique synthetic source {index}".encode()
            path.write_bytes(data)
            asset = ET.SubElement(resources, "asset")
            ET.SubElement(asset, "media-rep", kind="original-media", src=path.as_uri())
            clips.append({"media_id": "sha256:" + hashlib.sha256(data).hexdigest(),
                          "status": "approved", "group_id": "known-group", "offset_seconds": offset,
                          "rate": 1.0, "duration": 30.0, "has_video": index < 2,
                          "fps": "25/1" if index < 2 else None, "has_audio": True, "audio_channels": 2})
        ET.ElementTree(document).write(self.xml_path, encoding="utf-8")
        self.manifest = {"product": "Frameflow", "schema_version": 1, "revision": 4, "clips": clips}

    def plan(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        return prepare(self.manifest_path, self.xml_path)

    def test_native_sync_marks_use_one_common_time_and_keep_full_source(self):
        plan = self.plan()
        common_times = [c["offset_seconds"] + c["mark_in"] / plan["fps"] for c in plan["clips"]]
        self.assertLessEqual(max(common_times) - min(common_times), 1 / plan["fps"])
        self.assertEqual([c["mark_out"] for c in plan["clips"]], [749, 749, 749])
        self.assertEqual([c["duration"] for c in plan["clips"]], [30.0] * 3)
        self.assertEqual(plan["revision"], 4)

    def test_handoff_refuses_changed_source_even_when_file_name_matches(self):
        (self.root / "source-0.bin").write_bytes(b"wrong content")
        with self.assertRaisesRegex(ValueError, "完整内容身份"):
            self.plan()

    def test_handoff_refuses_unreviewed_drift_and_nonoverlapping_groups(self):
        original = deepcopy(self.manifest)
        for mode in ("unreviewed", "drift", "nonlinear", "separate_groups", "no_common_time"):
            self.manifest = deepcopy(original)
            clip = self.manifest["clips"][0]
            if mode == "unreviewed": clip["status"] = "proposed"
            if mode == "drift": clip["rate"] = 1.0001
            if mode == "nonlinear": clip["blocked_reason"] = "NONLINEAR_MAPPING"
            if mode == "separate_groups": clip["group_id"] = "different-group"
            if mode == "no_common_time": clip["offset_seconds"] = 100.0
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.plan()

    def test_unverified_channel_layout_is_blocked_instead_of_downmixed(self):
        self.manifest["clips"][2]["audio_channels"] = 8
        with self.assertRaisesRegex(ValueError, "声道布局"):
            self.plan()


if __name__ == "__main__":
    unittest.main()
