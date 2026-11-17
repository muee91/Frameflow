#!/usr/bin/env python3
"""Exercise the real local UI, FFmpeg media and Align job in an approved CI browser.

Install the project test extra and Playwright Chromium in CI before running:
    python -m playwright install --with-deps chromium
    FRAMEFLOW_TEST_ALIGN=... python tools/browser_smoke.py --artifacts ...

There are no API, worker, media or browser mocks. All media is generated in a
TemporaryDirectory. Screenshots and the small validation summary go only to the
explicit artifact directory. Passing this gate does not verify a camera shoot or
any target NLE import. Do not run this script on a restricted preview surface.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile
import threading
import time
import traceback
import xml.etree.ElementTree as ET

# Support both `python tools/browser_smoke.py` and `python -m tools.browser_smoke`.
REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

from frameflow.api import create_app
from frameflow.engine import inspect_engine
from tools.synthetic_media import generate_media


class GateFailure(RuntimeError):
    """A user-observable workflow or precondition failed."""


@contextmanager
def local_server(workspace: Path, engine_path: str, token: str):
    """Bind an ephemeral loopback port before starting Uvicorn, avoiding a race."""
    import httpx
    import uvicorn

    app = create_app(workspace, engine_path=engine_path, token=token)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    origin = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", access_log=False))
    failures = []

    def serve():
        try:
            server.run(sockets=[listener])
        except BaseException as error:
            failures.append(error)

    thread = threading.Thread(target=serve, name="frameflow-browser-ci", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started:
            if failures or not thread.is_alive():
                raise GateFailure("The real local API did not start.")
            if time.monotonic() >= deadline:
                raise GateFailure("The local API startup timed out.")
            time.sleep(0.05)
        with httpx.Client(base_url=origin, timeout=15, trust_env=False) as client:
            client.post("/api/session", json={"token": token}).raise_for_status()
            yield origin, client
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()
        if thread.is_alive():
            raise GateFailure("The local API did not shut down safely.")


def get_json(client, path):
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def wait_job(client, project_id, *, kind, timeout, job_id=None):
    """Read the durable job state; a failed task fails the gate immediately."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if job_id:
            job = get_json(client, f"/api/jobs/{job_id}")
        else:
            jobs = get_json(client, f"/api/projects/{project_id}/jobs")
            job = next((item for item in jobs if item.get("kind", "sync") == kind), None)
        if job:
            if job["state"] in {"failed", "cancelled", "interrupted"}:
                raise GateFailure(f"{kind} task ended as {job['state']}: {job.get('error_code') or 'no error code'}")
            if job["state"] in {"needs_review", "succeeded"}:
                return job
        time.sleep(0.2)
    raise GateFailure(f"{kind} task did not finish within {timeout:g} seconds.")


def require(condition, message):
    if not condition:
        raise GateFailure(message)


def capture(page, artifacts: Path, filename: str):
    require(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"),
            "The workflow has horizontal overflow at this viewport.")
    page.screenshot(path=str(artifacts / filename), full_page=True, mask=[page.locator(".source-path")])



def failure_diagnostic(error, stage, page=None):
    """Allowlisted debugging evidence: no URLs, credentials or source paths."""
    locations = []
    for frame in traceback.extract_tb(error.__traceback__):
        try:
            relative = Path(frame.filename).resolve().relative_to(REPOSITORY)
        except ValueError:
            continue
        locations.append({"file": relative.as_posix(), "line": frame.lineno, "function": frame.name})
    diagnostic = {"stage": stage, "exception": type(error).__name__, "locations": locations}
    if isinstance(error, GateFailure):
        diagnostic["reason"] = str(error)
    if page is not None:
        try:
            diagnostic["ui"] = page.evaluate("""() => ({
                playbackState: document.querySelector('#toggle-play')?.dataset.playbackState || null,
                timelineSeconds: Number(document.querySelector('#timeline-seek')?.value || 0),
                reviewVisible: !document.querySelector('#review-panel')?.hidden,
                exportDownloadVisible: !document.querySelector('#download-export')?.hidden,
                media: [...document.querySelectorAll('.clip-card video, .clip-card audio')].map((media, index) => ({
                    index, kind: media.tagName, currentTime: media.currentTime,
                    duration: Number.isFinite(media.duration) ? media.duration : null,
                    readyState: media.readyState, networkState: media.networkState,
                    paused: media.paused, seeking: media.seeking, ended: media.ended,
                    muted: media.muted, playbackRate: media.playbackRate,
                    errorCode: media.error?.code || null,
                    buffered: Array.from({length: Math.min(media.buffered.length, 4)}, (_, i) =>
                        [media.buffered.start(i), media.buffered.end(i)])
                }))
            })""")
        except Exception:
            diagnostic["ui"] = {"unavailable": True}
    return diagnostic


def run_gate(artifacts: Path, timeout: float):
    from playwright.sync_api import sync_playwright

    executable = os.environ.get("FRAMEFLOW_TEST_ALIGN")
    if not executable:
        raise GateFailure("FRAMEFLOW_TEST_ALIGN must name the separately installed Align CLI; this gate cannot skip or use a fake engine.")
    info = inspect_engine(executable)
    require(info["available"], "The installed Align version does not pass the adapter version gate.")
    artifacts.mkdir(parents=True, exist_ok=True)
    stage = "generate-synthetic-media"

    def checkpoint(name):
        nonlocal stage
        stage = name
        print(f"frameflow-browser stage={stage}", flush=True)

    checkpoint(stage)
    token = secrets.token_urlsafe(32)
    with tempfile.TemporaryDirectory(prefix="frameflow-browser-") as temporary:
        root = Path(temporary)
        roots = generate_media(root / "synthetic-media")
        require(len(roots) >= 3, "The synthetic fixture must contain two cameras and an independent recorder.")
        with local_server(root / "workspace", executable, token) as (origin, client):
            with sync_playwright() as playwright:
                # No executable override or sandbox bypass: use CI's installed browser.
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1050}, accept_downloads=True)
                page = context.new_page()
                javascript_errors = []
                page.on("pageerror", lambda error: javascript_errors.append(str(error)))
                try:
                    checkpoint("open-local-ui")
                    page.goto(f"{origin}/#token={token}", wait_until="networkidle")
                    checkpoint("check-tool-readiness")
                    page.wait_for_function("document.querySelector('#system-status').textContent.includes('已就绪')")
                    require("#token" not in page.url, "The bootstrap token remained in the address bar.")
                    capture(page, artifacts, "01-create-desktop.png")
                    checkpoint("create-project")
                    page.get_by_label("项目名称", exact=True).fill("Synthetic browser workflow")
                    # Keep actual user-facing selection controls in the tested path.
                    page.locator("#source-rows .remove-source").last.click()
                    for index, source in enumerate(roots):
                        if index:
                            page.locator("#add-recorder" if source["kind"] == "recorder" else "#add-camera").click()
                        row = page.locator(".source-row").nth(index)
                        row.locator(".source-name").fill(source["label"])
                        row.locator(".source-path").fill(source["path"])
                    with page.expect_response(lambda response: response.url == f"{origin}/api/projects"
                                              and response.request.method == "POST") as created:
                        page.locator("#create-submit").click()
                    require(created.value.status == 201, "Creating the project through the UI failed.")
                    project_id = created.value.json()["id"]
                    page.locator("#job-panel").wait_for(state="visible")
                    checkpoint("wait-sync-task")
                    sync_job = wait_job(client, project_id, kind="sync", timeout=timeout)
                    require(sync_job["state"] == "needs_review", "Automatic sync bypassed the human review state.")
                    events = get_json(client, f"/api/jobs/{sync_job['id']}/events")
                    require(bool(events), "The real sync task did not emit observable events.")
                    checkpoint("read-sync-result")
                    page.locator("#review-panel").wait_for(state="visible", timeout=20_000)
                    result = get_json(client, f"/api/projects/{project_id}/result")
                    clips = result["clips"]
                    require(len(clips) == 3, "The browser fixture lost or duplicated media.")
                    require(len(result["groups"]) == 1, "The fixture must produce a single evidence-backed sync group.")
                    require(all(clip["status"] == "proposed" for clip in clips),
                            "Fixture media was unmatched, prematurely approved or silently excluded.")
                    require(all(clip.get("offset_seconds") is not None and not clip.get("blocked_reason") for clip in clips),
                            "The fixture does not have playable mappings.")
                    require(page.locator(".clip-card").count() == len(clips), "The review UI omitted a clip.")
                    require(page.locator("#export-fcpxml").is_disabled(), "The UI allowed export before review.")
                    require(page.locator(".clip-form textarea").evaluate_all("items => items.every(item => item.value === '')"), "Human review reasons must not be prefilled as if playback had been verified.")
                    checkpoint("load-media")
                    page.wait_for_function("""() => {
                        const media = [...document.querySelectorAll('.clip-card video, .clip-card audio')];
                        return media.length === 3 && media.every(item => item.readyState >= 2 && !item.error);
                    }""", timeout=30_000)
                    checkpoint("seek-common-timeline")
                    overlap_start = max(clip["offset_seconds"] for clip in clips)
                    overlap_end = min(clip["offset_seconds"] + clip["duration"] * clip["rate"] for clip in clips)
                    require(overlap_end - overlap_start > 3, "The synthetic fixture has insufficient playback overlap.")
                    target = overlap_start + 1
                    page.locator("#timeline-seek").evaluate("""(range, target) => {
                        range.value = String(target);
                        range.dispatchEvent(new Event('input', {bubbles: true}));
                    }""", target)
                    page.wait_for_function("(target) => Math.abs(Number(document.querySelector('#timeline-seek').value) - target) < 0.01", arg=target)
                    expected = {clip["name"]: (target - clip["offset_seconds"]) / clip["rate"] for clip in clips}
                    page.wait_for_function("""(expected) => [...document.querySelectorAll('.clip-card')].every(card => {
                        const media = card.querySelector('video, audio');
                        return !media.seeking && Math.abs(media.currentTime - expected[card.querySelector('h3').textContent]) < 0.12;
                    })""", arg=expected, timeout=15_000)
                    checkpoint("play-common-timeline")
                    before = page.locator(".clip-card video").evaluate_all("items => items.map(item => item.currentTime)")
                    page.locator("#toggle-play").click()
                    page.wait_for_function("""(before) => {
                        const videos = [...document.querySelectorAll('.clip-card video')];
                        return videos.length === 2 && videos.every((item, i) => !item.paused && item.currentTime > before[i] + 0.4);
                    }""", arg=before, timeout=15_000)
                    page.locator("#toggle-play").click()
                    checkpoint("check-independent-audio")
                    audible = page.locator(".clip-card video, .clip-card audio").evaluate_all("items => items.filter(item => !item.muted).map(item => item.tagName)")
                    require(audible == ["AUDIO"], "Playback did not isolate the independent recorder audio.")
                    checkpoint("capture-layout")
                    capture(page, artifacts, "02-review-desktop.png")
                    page.set_viewport_size({"width": 390, "height": 844})
                    capture(page, artifacts, "03-review-mobile.png")
                    page.set_viewport_size({"width": 1440, "height": 1050})
                    for index, clip in enumerate(clips):
                        checkpoint(f"review-clip-{index + 1}")
                        card = page.locator(".clip-card").filter(has=page.get_by_role("heading", name=clip["name"], exact=True))
                        card.get_by_label(f"{clip['name']} 核对备注", exact=True).fill("合成测试：浏览器已播放并核对共同时间轴")
                        with page.expect_response(lambda response: response.url == f"{origin}/api/projects/{project_id}/review"
                                                  and response.request.method == "POST") as reviewed:
                            card.get_by_role("button", name="确认位置", exact=True).click()
                        require(reviewed.value.status == 200, "A real revision-checked UI review failed.")
                        revision = reviewed.value.json()["revision"]
                        page.wait_for_function("(revision) => document.querySelector('#result-revision').textContent.includes('修订 ' + revision)", arg=revision)
                    approved = get_json(client, f"/api/projects/{project_id}/result")
                    require(all(clip["status"] == "approved" for clip in approved["clips"]), "The UI did not persist all human review decisions.")
                    checkpoint("submit-export")
                    with page.expect_response(lambda response: response.url == f"{origin}/api/projects/{project_id}/exports"
                                              and response.request.method == "POST") as exporting:
                        page.locator("#export-fcpxml").click()
                    require(exporting.value.status == 202, "The UI did not create an asynchronous export job.")
                    checkpoint("wait-export-task")
                    export_job = wait_job(client, project_id, kind="export", timeout=timeout, job_id=exporting.value.json()["id"])
                    require(export_job["state"] == "succeeded" and bool(export_job["artifact"]), "The export did not publish an artifact.")
                    checkpoint("download-export")
                    page.locator("#download-export").wait_for(state="visible", timeout=20_000)
                    require(page.locator("#review-panel").is_visible(), "Starting export discarded the review view.")
                    with page.expect_download() as downloaded:
                        page.locator("#download-export").click()
                    download = downloaded.value
                    require(download.failure() is None, "The browser download failed.")
                    checkpoint("validate-download")
                    document = ET.parse(download.path()).getroot()
                    require(document.tag == "fcpxml", "The downloaded artifact is not FCPXML.")
                    asset_count = len(document.findall("./resources/asset"))
                    require(asset_count >= 3, "The candidate export omitted a source media asset.")
                    capture(page, artifacts, "04-export-desktop.png")
                    require(not javascript_errors, "The browser raised uncaught JavaScript errors.")
                    summary = {
                        "browser_workflow": "passed",
                        "api": "real local FastAPI and durable jobs",
                        "engine": f"Align {info['version']}",
                        "media": "FFmpeg-generated synthetic H264/AAC and independent WAV",
                        "clips_reviewed": len(clips), "export_assets": asset_count,
                        "playback": "two video clocks advanced after a mapped seek; recorder audio isolated",
                        "real_shoot_verified": False, "target_nle_import_verified": False,
                    }
                    (artifacts / "browser-validation.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
                    checkpoint("complete")
                    print(json.dumps(summary))
                except BaseException as error:
                    diagnostic = failure_diagnostic(error, stage, page)
                    (artifacts / "failure.json").write_text(json.dumps(diagnostic, indent=2) + "\n", encoding="utf-8")
                    print("frameflow-browser failure=" + json.dumps(diagnostic), file=sys.stderr, flush=True)
                    # Only synthetic content; mask directory inputs, never the
                    # actual playback state or errors. Do not save URL-bearing traces.
                    try:
                        page.screenshot(path=str(artifacts / "failure.png"), full_page=True,
                                        mask=[page.locator(".source-path")])
                    except Exception:
                        pass
                    raise
                finally:
                    context.close()
                    browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", type=Path, required=True, help="Directory for synthetic workflow screenshots and validation summary")
    parser.add_argument("--timeout", type=float, default=180, help="Maximum seconds for each real engine task")
    args = parser.parse_args()
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be a finite positive number")
    if args.artifacts.resolve() == REPOSITORY or args.artifacts.resolve() in REPOSITORY.parents:
        parser.error("--artifacts must be a dedicated output directory, not the repository root or its parent")
    try:
        run_gate(args.artifacts.resolve(), args.timeout)
    except Exception as error:
        # Exceptions from navigation can include a URL fragment. Do not print
        # secrets or a machine's absolute temporary paths in public CI logs.
        print(f"Browser workflow gate failed: {type(error).__name__}. See synthetic screenshots and the CI step context.", file=sys.stderr)
        if isinstance(error, GateFailure):
            print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
