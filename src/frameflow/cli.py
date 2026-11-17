from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import webbrowser
from pathlib import Path


def default_workspace():
    if sys.platform == "win32":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return root / "Frameflow"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Frameflow"
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "frameflow"


def main():
    parser = argparse.ArgumentParser(description="Frameflow — 本机多机位同步与核对")
    parser.add_argument("command", nargs="?", choices=["serve", "doctor"], default="serve")
    parser.add_argument("--workspace", type=Path, default=default_workspace(), help="本机运行数据目录，勿放进 Git")
    parser.add_argument("--align", help="Align v0.3.3 CLI 路径；也可使用 FRAMEFLOW_ALIGN")
    parser.add_argument("--port", type=int, default=0, help="本机端口；0 自动选择")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口须为 0–65535。")
    from .engine import inspect_engine
    if args.command == "doctor":
        info = inspect_engine(args.align)
        info.pop("executable", None)
        state = {"engine": info, "ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0 if info["available"] and state["ffmpeg"] and state["ffprobe"] else 1
    import uvicorn
    from .api import create_app
    token = secrets.token_urlsafe(32)
    app = create_app(args.workspace, engine_path=args.align, token=token)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", args.port))
    sock.listen(128)
    url = f"http://127.0.0.1:{sock.getsockname()[1]}/#token={token}"
    print("Frameflow 正在本机运行。原始媒体不会上传。")
    # The fragment bootstraps an HttpOnly session; never include it in request logs.
    print(f"本机启动链接（请勿分享）：{url}")
    if not args.no_browser:
        threading.Timer(1, lambda: webbrowser.open(url)).start()
    config = uvicorn.Config(app, host="127.0.0.1", access_log=False, log_level="warning")
    try:
        uvicorn.Server(config).run(sockets=[sock])
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
