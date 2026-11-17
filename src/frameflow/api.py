"""Loopback-only local UI API. Not a LAN server or cloud media uploader."""

from __future__ import annotations

import json
import mimetypes
import secrets
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from . import __version__, engine
from .errors import Conflict, TaskError
from .jobs import JobManager
from .file_state import path_signature
from .waveform import cached_waveform


class RootInput(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    label: str = Field(min_length=1, max_length=80)
    kind: str = "camera"


class ProjectInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    roots: list[RootInput] = Field(min_length=1, max_length=32)


class ReviewInput(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, strict=True)
    revision: int = Field(ge=1)
    clip_id: str = Field(min_length=1)
    offset_seconds: float | None = None
    status: str
    reason: str = Field(min_length=1, max_length=1000)
    group_id: str | None = None


class ExportInput(BaseModel):
    model_config = ConfigDict(strict=True)
    revision: int = Field(ge=1)
    format: str = "fcpxml"


def portable_manifest(store, project_id, result):
    """An allowlist, not path redaction: local bindings/raw engine JSON cannot escape."""
    fields = {"id", "media_id", "name", "kind", "camera", "duration", "fps", "has_video", "has_audio",
              "width", "height", "audio_channels", "bytes", "group_id", "offset_seconds", "rate", "status",
              "reason", "evidence", "mapping", "blocked_reason", "review_reason",
              "suggested_offset_seconds", "suggested_group_id", "original_mapping",
              "video_duration", "audio_duration", "range_adjustment"}
    project = store.project(project_id)
    return {"schema_version": 1, "product": "Frameflow", "product_version": __version__,
            "project": {"id": project["id"], "name": project["name"]},
            "revision": result["revision"], "engine": result["engine"],
            "groups": result["groups"], "clips": [{k: v for k, v in clip.items() if k in fields} for clip in result["clips"]],
            "review_history": store.review_history(project_id),
            "validation": {"real_media_verified": False, "nle_import_verified": False,
                           "note": "人工确认与软件导入验收是不同记录；此清单不含原始素材。"}}


def create_app(workspace: Path, *, engine_path=None, token=None, allowed_hosts=None):
    session_token = token or secrets.token_urlsafe(32)
    hosts = set(allowed_hosts or {"127.0.0.1", "localhost", "::1"})
    static = Path(__file__).parent / "static"

    @asynccontextmanager
    async def lifespan(app):
        app.state.manager = JobManager(workspace, engine_path)
        try:
            yield
        finally:
            app.state.manager.close()

    app = FastAPI(title="Frameflow local API", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def local_session(request: Request, call_next):
        host = urlsplit("http://" + request.headers.get("host", "")).hostname
        if host not in hosts:
            return JSONResponse({"detail": "不接受此主机的请求。"}, status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
            return JSONResponse({"detail": "不接受跨站请求。"}, status_code=403)
        if request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "不接受跨站请求。"}, status_code=403)
        # Bound bodies before JSON decoding. The app has no upload endpoint.
        if request.method in {"POST", "PUT", "PATCH"}:
            try:
                if int(request.headers.get("content-length", "0")) > 65536:
                    return JSONResponse({"detail": "请求过大。"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "无效请求。"}, status_code=400)
            if "application/json" not in request.headers.get("content-type", ""):
                return JSONResponse({"detail": "请求须使用 JSON。"}, status_code=415)
            if len(await request.body()) > 65536:
                return JSONResponse({"detail": "请求过大。"}, status_code=413)
        if request.url.path.startswith("/api/") and request.url.path != "/api/session":
            supplied = request.cookies.get("frameflow_session", "")
            if not secrets.compare_digest(supplied, session_token):
                return JSONResponse({"detail": "会话已失效，请使用 Frameflow 启动时打开的窗口。"}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        return response

    @app.exception_handler(Conflict)
    async def conflict_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing_handler(request, exc):
        return JSONResponse({"detail": "项目、任务或素材不存在。"}, status_code=404)

    @app.exception_handler(ValueError)
    async def value_handler(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(TaskError)
    async def task_handler(request, exc):
        return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=422)

    def manager():
        return app.state.manager

    def store():
        return manager().store

    def present_result(project_id, result):
        for clip in result["clips"]:
            clip["media_url"] = f"/api/projects/{project_id}/media/{quote(clip['media_id'], safe='')}"
        return result

    @app.post("/api/session")
    async def session(request: Request):
        body = await request.json()
        supplied = body.get("token", "") if isinstance(body, dict) else ""
        if not isinstance(supplied, str) or not secrets.compare_digest(supplied, session_token):
            raise HTTPException(401, "启动凭证无效，请重新打开本机 Frameflow。")
        response = JSONResponse({"ok": True})
        response.set_cookie("frameflow_session", session_token, httponly=True, samesite="strict", path="/")
        return response

    @app.get("/api/system")
    def system():
        info = engine.inspect_engine(manager().engine_path)
        info.pop("executable", None)
        return {"version": __version__, "engine": info, "ffmpeg": bool(shutil.which("ffmpeg")),
                "ffprobe": bool(shutil.which("ffprobe")), "mode": "local"}

    @app.get("/api/projects")
    def projects():
        return store().projects()

    @app.post("/api/projects", status_code=201)
    def create_project(body: ProjectInput):
        return store().create_project(body.name, [r.model_dump() for r in body.roots])

    @app.get("/api/projects/{project_id}")
    def project(project_id: str):
        return store().project(project_id)

    @app.get("/api/projects/{project_id}/jobs")
    def jobs(project_id: str):
        return store().jobs(project_id)

    @app.post("/api/projects/{project_id}/jobs", status_code=202)
    def create_job(project_id: str):
        return manager().submit(project_id)

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return store().job(job_id)

    @app.get("/api/jobs/{job_id}/events")
    def events(job_id: str, after: int = 0):
        if after < 0:
            raise HTTPException(422, "事件位置无效。")
        return store().events(job_id, after=after)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return store().cancel(job_id)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str):
        previous = store().job(job_id)
        return manager().submit(previous["project_id"], retry_of=job_id)

    @app.get("/api/projects/{project_id}/result")
    def result(project_id: str):
        return present_result(project_id, store().result(project_id))

    @app.post("/api/projects/{project_id}/review")
    def review(project_id: str, body: ReviewInput):
        return present_result(project_id, store().review(project_id, **body.model_dump()))

    @app.get("/api/projects/{project_id}/media/{media_id}")
    @app.head("/api/projects/{project_id}/media/{media_id}")
    def media(project_id: str, media_id: str):
        path = store().checked_binding(project_id, media_id)
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream")

    @app.get("/api/projects/{project_id}/waveform/{media_id}")
    def waveform(project_id: str, media_id: str):
        path = store().checked_binding(project_id, media_id)
        signature = path_signature(path)
        image = cached_waveform(path, manager().workspace / "waveforms", media_id, signature)
        # A source changed during FFmpeg decoding must not be presented as current.
        verified = store().checked_binding(project_id, media_id)
        if verified != path or path_signature(verified) != signature:
            raise TaskError("SOURCE_CHANGED", "素材已变化，请重新扫描。")
        return FileResponse(image, media_type="image/png")

    @app.get("/api/projects/{project_id}/export")
    def manifest(project_id: str, format: str = "manifest"):
        if format != "manifest":
            raise HTTPException(405, "工程导出须创建可取消的导出任务。")
        result = store().require_exportable(project_id)
        data = json.dumps(portable_manifest(store(), project_id, result), ensure_ascii=False, allow_nan=False, indent=2)
        return Response(data, media_type="application/json", headers={
            "Content-Disposition": 'attachment; filename="frameflow-sync-manifest.json"'})

    @app.post("/api/projects/{project_id}/exports", status_code=202)
    def create_export(project_id: str, body: ExportInput):
        return manager().submit(project_id, kind="export", revision=body.revision, format=body.format)

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str):
        job = store().job(job_id)
        if job.get("kind") != "export" or job["state"] != "succeeded" or not job.get("artifact"):
            raise HTTPException(409, "工程尚未生成或任务未成功。")
        root = manager().workspace / "runs" / job_id
        path = (root / job["artifact"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file() or path.is_symlink():
            raise HTTPException(404, "交付文件不可用，请重新导出。")
        return FileResponse(path, filename="frameflow-experimental.fcpxml", media_type="application/xml")

    @app.post("/api/pick-directory")
    def pick_directory():
        script = ("import tkinter as tk; from tkinter import filedialog; "
                  "r=tk.Tk(); r.withdraw(); r.attributes('-topmost', True); "
                  "p=filedialog.askdirectory(title='Frameflow — 选择素材目录'); print(p); r.destroy()")
        try:
            completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                                       timeout=120, check=True)
        except (subprocess.SubprocessError, OSError):
            raise HTTPException(503, "本机目录选择器不可用，请粘贴素材目录路径。") from None
        return {"path": completed.stdout.strip()}

    @app.get("/")
    @app.get("/static/index.html")
    def index():
        return FileResponse(static / "index.html")

    @app.get("/app.js")
    @app.get("/static/app.js")
    def script():
        return FileResponse(static / "app.js", media_type="text/javascript")

    @app.get("/timeline.js")
    @app.get("/static/timeline.js")
    def timeline_script():
        return FileResponse(static / "timeline.js", media_type="text/javascript")

    @app.get("/style.css")
    @app.get("/static/style.css")
    def style():
        return FileResponse(static / "style.css", media_type="text/css")

    return app
