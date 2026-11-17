from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
import tempfile
import unittest
from urllib.parse import quote
import wave

from fastapi.testclient import TestClient

from frameflow.api import create_app


class APITests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.media = self.root / "media"
        self.media.mkdir()
        self.paths = [self.media / "first.wav", self.media / "second.wav"]
        for index, path in enumerate(self.paths):
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(bytes((index + 1, 0)) * 32000)
        self.app = create_app(self.root / "workspace", token="test-frameflow-token", allowed_hosts={"testserver"})
        self.client = self.enterContext(TestClient(self.app))

    def authenticate(self):
        response = self.client.post("/api/session", json={"token": "test-frameflow-token"})
        self.assertEqual(response.status_code, 200)
        return response

    def publish_result(self):
        self.authenticate()
        response = self.client.post("/api/projects", json={"name": "合成 API 验收", "roots": [
            {"path": str(self.media), "label": "录音", "kind": "recorder"}]})
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        store = self.app.state.manager.store
        clips = []
        for index, path in enumerate(self.paths):
            identity = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            asset = {"id": identity, "media_id": identity, "name": path.name, "duration": 2.0,
                     "has_audio": True, "has_video": False, "audio_channels": 1,
                     "camera": "录音", "kind": "recorder", "local_path": str(path)}
            store.save_asset(project["id"], asset)
            clips.append({k: v for k, v in asset.items() if k != "local_path"} | {
                "group_id": "group-1", "offset_seconds": index * 0.25, "rate": 1.0, "status": "proposed",
                "evidence": ["waveform"], "engine_clip_id": f"synthetic-{index}"})
        job = store.create_job(project["id"])
        store.start_job(job["id"])
        store.finish(job["id"], "needs_review", result={"engine": {"name": "synthetic-test", "version": "test"},
                     "groups": [{"id": "group-1", "name": "合成组"}], "clips": clips, "warnings": []})
        result = self.client.get(f"/api/projects/{project['id']}/result")
        self.assertEqual(result.status_code, 200)
        return project, result.json()

    def approve(self, project, result):
        for clip in result["clips"]:
            response = self.client.post(f"/api/projects/{project['id']}/review", json={
                "revision": result["revision"], "clip_id": clip["id"], "status": "approved", "reason": "已核对合成验收夹具。"})
            self.assertEqual(response.status_code, 200, response.text)
            result = response.json()
        return result

    def test_unauthorized_and_incorrect_token_are_rejected(self):
        self.assertEqual(self.client.get("/api/projects").status_code, 401)
        self.assertEqual(self.client.post("/api/session", json={"token": "wrong"}).status_code, 401)
        response = self.authenticate()
        cookie = response.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=strict", cookie)
        self.assertEqual(self.client.get("/api/projects").status_code, 200)

    def test_cross_origin_host_and_fetch_site_are_rejected(self):
        self.authenticate()
        for headers in [
            {"origin": "https://untrusted.invalid"}, {"host": "untrusted.invalid"},
            {"sec-fetch-site": "cross-site"},
        ]:
            with self.subTest(headers=headers):
                self.assertEqual(self.client.get("/api/projects", headers=headers).status_code, 403)
        self.assertEqual(self.client.get("/api/projects", headers={"origin": "http://testserver"}).status_code, 200)
        self.assertEqual(self.client.post("/api/session", json={"token": "test-frameflow-token"},
                                         headers={"origin": "https://untrusted.invalid"}).status_code, 403)

    def test_mutations_require_bounded_json_bodies(self):
        response = self.client.post("/api/session", content="x" * 70000, headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.client.post("/api/session", content="token=x").status_code, 415)

    def test_real_file_range_and_head_support_seek(self):
        _, result = self.publish_result()
        url = result["clips"][0]["media_url"]
        expected = self.paths[0].read_bytes()
        response = self.client.get(url, headers={"range": "bytes=0-11"})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, expected[:12])
        self.assertEqual(response.headers["content-range"], f"bytes 0-11/{len(expected)}")
        head = self.client.head(url)
        self.assertEqual(head.status_code, 200)
        self.assertEqual(head.content, b"")
        self.assertEqual(int(head.headers["content-length"]), len(expected))
        suffix = self.client.get(url, headers={"range": "bytes=-6"})
        self.assertEqual(suffix.status_code, 206)
        self.assertEqual(suffix.content, expected[-6:])
        self.assertEqual(self.client.get(url, headers={"range": f"bytes={len(expected) + 1}-"}).status_code, 416)

    def test_waveform_uses_ffmpeg_real_media_and_requires_session(self):
        if not shutil.which("ffmpeg"):
            self.skipTest("FFmpeg optional for basic API contract tests")
        project, result = self.publish_result()
        url = f"/api/projects/{project['id']}/waveform/{quote(result['clips'][0]['media_id'], safe='')}"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(response.content, self.client.get(url).content)
        self.client.cookies.clear()
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_undo_review_is_authenticated_and_preserves_pending_gate(self):
        project, result = self.publish_result()
        cid = result["clips"][0]["id"]
        base = f"/api/projects/{project['id']}"
        revised = self.client.post(base + "/review", json={
            "revision": result["revision"], "clip_id": cid, "status": "proposed",
            "offset_seconds": 0.25, "reason": "录音中的拍板偏移校对。"})
        self.assertEqual(revised.status_code, 200, revised.text)
        changed = revised.json()
        self.assertEqual(changed["undo_clip_id"], cid)
        stale = self.client.post(base + "/review/undo", json={
            "revision": result["revision"], "clip_id": cid})
        self.assertEqual(stale.status_code, 409)
        response = self.client.post(base + "/review/undo", json={
            "revision": changed["revision"], "clip_id": cid})
        self.assertEqual(response.status_code, 200, response.text)
        undone = response.json()
        self.assertEqual(undone["clips"][0]["offset_seconds"], 0)
        self.assertEqual(undone["clips"][0]["status"], "proposed")
        self.assertIsNone(undone["undo_clip_id"])
        self.assertEqual(self.client.get(base + "/export").status_code, 409)
        self.client.cookies.clear()
        self.assertEqual(self.client.post(base + "/review/undo", json={
            "revision": undone["revision"], "clip_id": cid}).status_code, 401)

    def test_changed_source_cannot_be_played_as_verified_asset(self):
        _, result = self.publish_result()
        with self.paths[0].open("ab") as output:
            output.write(b"changed")
        response = self.client.get(result["clips"][0]["media_url"])
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["code"], "SOURCE_CHANGED")
        self.assertNotIn(str(self.root), response.text)

    def test_identifiers_cannot_be_used_as_filesystem_paths(self):
        project, _ = self.publish_result()
        secret = self.root / "private.txt"
        secret.write_text("never-return-this-file")
        for identifier in [str(secret), "../../private.txt", "unknown", "x' OR 1=1 --"]:
            response = self.client.get(f"/api/projects/{project['id']}/media/{quote(identifier, safe='')}")
            self.assertEqual(response.status_code, 404)
            self.assertNotIn(secret.read_text(), response.text)
        self.assertEqual(self.client.get("/api/jobs/unknown/download").status_code, 404)

    def test_review_cas_manifest_paths_and_export_gate(self):
        project, result = self.publish_result()
        base = f"/api/projects/{project['id']}"
        self.assertEqual(self.client.get(base + "/export").status_code, 409)
        self.assertEqual(self.client.post(base + "/exports", json={"revision": result["revision"], "format": "fcpxml"}).status_code, 409)
        request = {"revision": result["revision"], "clip_id": result["clips"][0]["id"], "status": "approved", "reason": "合成夹具人工核对。"}
        updated = self.client.post(base + "/review", json=request)
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(self.client.post(base + "/review", json=request).status_code, 409)
        result = updated.json()
        result = self.approve(project, result)
        response = self.client.get(base + "/export")
        self.assertEqual(response.status_code, 200, response.text)
        manifest = response.json()
        self.assertFalse(manifest["validation"]["real_media_verified"])
        self.assertFalse(manifest["validation"]["nle_import_verified"])
        self.assertNotIn(str(self.root), response.text)
        self.assertNotIn("local_path", response.text)
        self.assertNotIn("media_url", response.text)
        self.assertNotIn("engine_clip_id", response.text)
        self.assertEqual(manifest["revision"], result["revision"])
        self.assertGreaterEqual(len(manifest["review_history"]), 2)
        invalid = dict(request, revision=True, offset_seconds=True)
        self.assertEqual(self.client.post(base + "/review", json=invalid).status_code, 422)

    def test_download_rejects_artifact_outside_its_own_run(self):
        project, result = self.publish_result()
        result = self.approve(project, result)
        store = self.app.state.manager.store
        export = store.create_job(project["id"], kind="export", revision=result["revision"], format="fcpxml")
        store.start_job(export["id"])
        store.finish(export["id"], "succeeded", artifact="../../../../private.txt")
        response = self.client.get(f"/api/jobs/{export['id']}/download")
        self.assertEqual(response.status_code, 404)

    def test_apply_correction_is_playable_but_not_approved_or_exportable(self):
        project, result = self.publish_result()
        base = f"/api/projects/{project['id']}"
        clip = result["clips"][0]
        request = {"revision": result["revision"], "clip_id": clip["id"], "status": "proposed",
                   "offset_seconds": 0.04, "reason": "应用修正，等待实际试听。"}
        response = self.client.post(base + "/review", json=request)
        self.assertEqual(response.status_code, 200, response.text)
        pending = response.json()
        corrected = pending["clips"][0]
        self.assertEqual(corrected["status"], "proposed")
        self.assertEqual(corrected["offset_seconds"], 0.04)
        self.assertEqual(self.client.get(corrected["media_url"]).status_code, 200)
        self.assertEqual(self.client.get(base + "/result").json(), pending)
        self.assertEqual(self.client.post(base + "/review", json=request).status_code, 409)
        self.assertEqual(self.client.get(base + "/export").status_code, 409)
        self.assertEqual(self.client.post(base + "/exports", json={"revision": pending["revision"]}).status_code, 409)
        result = self.approve(project, pending)
        manifest = self.client.get(base + "/export").json()
        self.assertEqual(manifest["revision"], result["revision"])
        self.assertEqual(manifest["review_history"][0]["reason"], request["reason"])


if __name__ == "__main__":
    unittest.main()
