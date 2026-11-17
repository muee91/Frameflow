"""Opt-in full HTTP/worker/media/engine/export acceptance using synthetic media.

This verifies generated project structure. It is not a target NLE import test.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

from fastapi.testclient import TestClient

from frameflow.api import create_app

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.synthetic_media import generate_media


@unittest.skipUnless(os.environ.get("FRAMEFLOW_TEST_ALIGN") and shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "Set FRAMEFLOW_TEST_ALIGN to Align 0.3.3 and install FFmpeg/ffprobe for the real synthetic-media workflow")
class RealEngineWorkflowTests(unittest.TestCase):
    def wait_for_job(self, client, job_id, *, timeout=180):
        deadline = time.monotonic() + timeout
        terminal = {"needs_review", "succeeded", "failed", "cancelled", "interrupted"}
        latest = None
        while time.monotonic() < deadline:
            response = client.get(f"/api/jobs/{job_id}")
            self.assertEqual(response.status_code, 200, response.text)
            latest = response.json()
            if latest["state"] in terminal:
                return latest
            time.sleep(0.05)
        self.fail(f"Synthetic real-engine task did not finish: {latest}")

    def test_real_create_sync_review_manifest_and_fcpxml_export(self):
        with tempfile.TemporaryDirectory(prefix="frameflow-real-workflow-") as temporary:
            root = Path(temporary).resolve()
            fixture_root = root / "fixtures"
            roots = generate_media(fixture_root)
            truth = json.loads((fixture_root / "ground_truth.json").read_text(encoding="utf-8"))
            expected = {clip["name"]: clip for clip in truth["clips"]}
            app = create_app(root / "workspace", engine_path=os.environ["FRAMEFLOW_TEST_ALIGN"],
                             token="synthetic-acceptance-session", allowed_hosts={"testserver"})
            with TestClient(app) as client:
                self.assertEqual(client.post("/api/session", json={"token": "synthetic-acceptance-session"}).status_code, 200)
                response = client.post("/api/projects", json={"name": "合成全链路验收", "roots": roots})
                self.assertEqual(response.status_code, 201, response.text)
                project_id = response.json()["id"]
                base = f"/api/projects/{project_id}"
                response = client.post(base + "/jobs", json={})
                self.assertEqual(response.status_code, 202, response.text)
                job = self.wait_for_job(client, response.json()["id"])
                self.assertEqual(job["state"], "needs_review", job)
                response = client.get(base + "/result")
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(result["engine"], {"name": "Align", "version": "0.3.3"})
                self.assertEqual(len(result["clips"]), 3)
                self.assertEqual(len(result["groups"]), 1, result)
                self.assertTrue(all(clip["status"] == "proposed" for clip in result["clips"]), result)
                by_name = {clip["name"]: clip for clip in result["clips"]}
                self.assertEqual(set(by_name), set(expected))
                reference = by_name["B.mp4"]["offset_seconds"]
                frame = 1 / truth["fps"]
                for name, clip in by_name.items():
                    with self.subTest(media=name):
                        self.assertAlmostEqual(clip["duration"], expected[name]["duration"], delta=frame)
                        self.assertIsNotNone(clip["offset_seconds"], clip)
                        self.assertAlmostEqual(clip["offset_seconds"] - reference, expected[name]["offset_seconds"], delta=frame)
                        self.assertIn("waveform", clip["evidence"])
                        self.assertNotIn("blocked_reason", clip)
                self.assertEqual(by_name["recorder.wav"]["audio_channels"], 2)
                self.assertTrue(all(by_name[name]["has_video"] for name in ("A.mp4", "B.mp4")))
                self.assertEqual(client.get(base + "/export").status_code, 409)
                self.assertEqual(client.post(base + "/exports", json={"revision": result["revision"], "format": "fcpxml"}).status_code, 409)
                # Exercise the same review API as the UI. The reason explicitly
                # records a synthetic truth check, never a real-shoot acceptance.
                for clip in result["clips"]:
                    response = client.post(base + "/review", json={
                        "revision": result["revision"], "clip_id": clip["id"], "status": "approved",
                        "reason": "根据生成器真值完成合成素材验收；不代表真实拍摄或 NLE 导入验收。"})
                    self.assertEqual(response.status_code, 200, response.text)
                    result = response.json()
                manifest = client.get(base + "/export")
                self.assertEqual(manifest.status_code, 200, manifest.text)
                self.assertNotIn(str(root), manifest.text)
                self.assertNotIn("local_path", manifest.text)
                self.assertNotIn("media_url", manifest.text)
                self.assertFalse(manifest.json()["validation"]["real_media_verified"])
                self.assertFalse(manifest.json()["validation"]["nle_import_verified"])
                response = client.post(base + "/exports", json={"revision": result["revision"], "format": "fcpxml"})
                self.assertEqual(response.status_code, 202, response.text)
                exported = self.wait_for_job(client, response.json()["id"])
                self.assertEqual(exported["state"], "succeeded", exported)
                download = client.get(f"/api/jobs/{exported['id']}/download")
                self.assertEqual(download.status_code, 200, download.text[:500])
                document = ET.fromstring(download.content)
                self.assertEqual(document.tag, "fcpxml")
                assets = document.findall("./resources/asset")
                self.assertGreaterEqual(len(assets), 3)
                self.assertTrue(set(expected) <= {asset.get("name") for asset in assets})
                self.assertTrue(any(asset.get("audioChannels") == "2" for asset in assets))
                self.assertIsNotNone(document.find(".//multicam"))
                self.assertIsNotNone(document.find(".//mc-clip"))
                self.assertGreaterEqual(len(document.findall(".//mc-angle")), 3)
                self.assertGreaterEqual(len(document.findall(".//sequence")), 2)
                events = client.get(f"/api/jobs/{job['id']}/events").json()
                self.assertTrue(any(event["stage"] == "sync" for event in events))
                self.assertTrue(any(event["stage"] == "review" for event in events))


if __name__ == "__main__":
    unittest.main()
