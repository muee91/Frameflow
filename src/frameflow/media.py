"""Read-only local inventory, real ffprobe metadata and full content identity."""

from __future__ import annotations

from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import stat

from .errors import TaskError
from .file_state import descriptor_signature, path_signature
from .process import run_command


VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mxf", ".mts", ".m2ts", ".avi", ".mkv", ".m4v", ".webm", ".mpg", ".mpeg", ".3gp"})
AUDIO_EXTENSIONS = frozenset({".wav", ".wave", ".bwf", ".aif", ".aiff", ".flac", ".m4a", ".mp3", ".aac", ".ogg"})
_CHUNK_BYTES = 1024 * 1024


def _regular_stat(path: Path) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise TaskError("MEDIA_UNREADABLE", f"无法读取素材 {path.name}。", retryable=True) from exc
    if stat.S_ISLNK(value.st_mode):
        raise TaskError("SYMLINK_NOT_ALLOWED", f"素材 {path.name} 是符号链接，请选择实际文件。", retryable=False)
    if not stat.S_ISREG(value.st_mode):
        raise TaskError("MEDIA_UNREADABLE", f"素材 {path.name} 不是普通文件。", retryable=False)
    return value


def _source_signature(path: Path) -> dict:
    _regular_stat(path)
    try:
        return path_signature(path)
    except OSError as exc:
        raise TaskError("MEDIA_UNREADABLE", f"无法验证素材 {path.name} 的文件状态。", retryable=True) from exc


def scan_roots(roots: list[dict], ctx) -> list[dict]:
    """Scan explicitly selected roots without following links or hidden folders.

    Counts are discovery counts: the total is unknown until traversal ends.
    Symlinks are an explicit error, including hidden links, instead of silently
    making a partial inventory look complete.
    """
    ctx.check_cancel()
    if not roots:
        raise TaskError("NO_MEDIA", "请先选择视频或独立录音目录。", retryable=False)
    checked: list[tuple[Path, str, str]] = []
    for root in roots:
        raw_path = root.get("path")
        if not raw_path or not isinstance(raw_path, (str, Path)):
            raise TaskError("ROOT_MISSING", "素材目录未设置。", retryable=False)
        path = Path(os.path.abspath(Path(raw_path).expanduser()))
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            raise TaskError("ROOT_MISSING", f"素材目录 {path.name} 不存在。", retryable=True) from exc
        except OSError as exc:
            raise TaskError("ROOT_UNREADABLE", f"无法读取素材目录 {path.name}。", retryable=True) from exc
        if stat.S_ISLNK(info.st_mode):
            raise TaskError("SYMLINK_NOT_ALLOWED", f"素材目录 {path.name} 是符号链接，请选择实际目录。", retryable=False)
        if not stat.S_ISDIR(info.st_mode):
            raise TaskError("ROOT_NOT_DIRECTORY", f"{path.name} 不是素材目录。", retryable=False)
        for previous, _, _ in checked:
            if path == previous or path in previous.parents or previous in path.parents:
                raise TaskError("ROOTS_OVERLAP", "所选素材目录重复或相互包含，请分别选择独立目录。", retryable=False)
        checked.append((path, str(root.get("label") or "未分类"), str(root.get("kind") or "video")))

    items: list[dict] = []
    ctx.progress("scan", 0, None, unit="files", message="正在发现素材，文件总数尚未确定。")
    for path, camera, kind in checked:
        directories = [path]
        while directories:
            ctx.check_cancel()
            ctx.heartbeat()
            directory = directories.pop()
            # Recheck before traversal in case a directory was replaced after
            # discovery. We never intentionally traverse a symlink.
            if directory.is_symlink():
                raise TaskError("SYMLINK_NOT_ALLOWED", f"目录 {directory.name} 是符号链接。", retryable=False)
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        ctx.check_cancel()
                        ctx.heartbeat()
                        if entry.is_symlink():
                            raise TaskError("SYMLINK_NOT_ALLOWED", f"发现符号链接 {entry.name}，请移除链接或选择实际目录。", retryable=False)
                        if entry.name.startswith("."):
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            directories.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        candidate = Path(entry.path)
                        if candidate.suffix.lower() not in VIDEO_EXTENSIONS | AUDIO_EXTENSIONS:
                            continue
                        # Probe/hash do the complete read later; fail permission
                        # problems already during discovery instead of dropping
                        # an inaccessible media file from the inventory.
                        with candidate.open("rb") as source:
                            source.read(1)
                        items.append({"path": candidate, "camera": camera, "kind": kind})
                        ctx.progress("scan", len(items), None, unit="files", current_item=entry.name, message="已发现素材。")
            except OSError as exc:
                raise TaskError("ROOT_UNREADABLE", f"无法完整读取素材目录 {directory.name}。", retryable=True) from exc
    if not items:
        raise TaskError("NO_MEDIA", "所选目录中没有支持的视频或独立录音。", retryable=False)
    items.sort(key=lambda item: (item["camera"], str(item["path"])))
    ctx.progress("scan", len(items), len(items), unit="files", message="素材发现完成。")
    ctx.activity(f"发现 {len(items)} 个视频或录音文件。")
    return items


def _positive_number(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _positive_int(value) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return result if result > 0 else 0


def _rate(stream: dict) -> str | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            value = Fraction(str(stream.get(key, "")))
        except (ValueError, ZeroDivisionError):
            continue
        if value > 0:
            return f"{value.numerator}/{value.denominator}"
    return None


def _stream_duration(stream: dict) -> float | None:
    direct = _positive_number(stream.get("duration"))
    if direct is not None:
        return direct
    try:
        return _positive_number(Fraction(str(stream["time_base"])) * int(stream["duration_ts"]))
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def probe_and_hash(item: dict, ctx) -> dict:
    """Inspect actual streams and hash every byte, refusing a changing source."""
    path = Path(item["path"])
    ctx.check_cancel()
    before = _source_signature(path)
    if before["size"] <= 0:
        raise TaskError("INVALID_MEDIA", f"素材 {path.name} 是空文件。", retryable=False)
    ctx.progress("probe", 0, 1, unit="files", current_item=path.name, message="读取媒体信息。")
    raw = run_command(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", "-i", str(path)],
        ctx,
        timeout=30,
    )
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise TaskError("INVALID_MEDIA", f"素材 {path.name} 的媒体信息无法解析。", retryable=False) from exc
    if not isinstance(payload, dict):
        raise TaskError("INVALID_MEDIA", f"素材 {path.name} 的媒体信息格式无效。", retryable=False)
    streams = payload.get("streams", [])
    if not isinstance(streams, list):
        raise TaskError("INVALID_MEDIA", f"素材 {path.name} 缺少有效媒体流。", retryable=False)
    streams = [stream for stream in streams if isinstance(stream, dict)]
    videos = [stream for stream in streams if stream.get("codec_type") == "video" and not (stream.get("disposition") or {}).get("attached_pic")]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if not videos and not audios:
        raise TaskError("INVALID_MEDIA", f"素材 {path.name} 没有可用的视频或音频流。", retryable=False)
    media_format = payload.get("format", {})
    if not isinstance(media_format, dict):
        media_format = {}
    duration = _positive_number(media_format.get("duration"))
    if duration is None:
        durations = [value for stream in videos + audios if (value := _stream_duration(stream)) is not None]
        duration = max(durations) if durations else None
    if duration is None:
        raise TaskError("INVALID_MEDIA", f"素材 {path.name} 缺少有效时长。", retryable=False)
    ctx.progress("probe", 1, 1, unit="files", current_item=path.name, message="媒体信息已读取。")
    digest = hashlib.sha256()
    count = 0
    ctx.progress("hash", 0, before["size"], unit="bytes", current_item=path.name, message="验证素材完整身份。")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as source:
            if descriptor_signature(source.fileno()) != before:
                raise TaskError("SOURCE_CHANGED", f"素材 {path.name} 在读取期间发生变化，请稍后重试。", retryable=True)
            while True:
                ctx.check_cancel()
                chunk = source.read(_CHUNK_BYTES)
                if not chunk:
                    break
                digest.update(chunk)
                count += len(chunk)
                if count > before["size"]:
                    raise TaskError("SOURCE_CHANGED", f"素材 {path.name} 在读取期间发生变化，请稍后重试。", retryable=True)
                ctx.heartbeat()
                ctx.progress("hash", count, before["size"], unit="bytes", current_item=path.name)
            unchanged_handle = descriptor_signature(source.fileno()) == before
    except OSError as exc:
        raise TaskError("MEDIA_UNREADABLE", f"无法完整读取素材 {path.name}。", retryable=True) from exc
    after = _source_signature(path)
    if not unchanged_handle or after != before or count != before["size"]:
        raise TaskError("SOURCE_CHANGED", f"素材 {path.name} 在读取期间发生变化，请稍后重试。", retryable=True)
    ctx.check_cancel()
    timecode = None
    for source in videos + [media_format] + audios:
        tags = source.get("tags", {})
        if isinstance(tags, dict):
            for key, value in tags.items():
                if str(key).lower() == "timecode" and isinstance(value, str) and value.strip():
                    timecode = value.strip()
                    break
        if timecode:
            break
    video = videos[0] if videos else {}
    identity = "sha256:" + digest.hexdigest()
    return {
        "id": identity,
        "media_id": identity,
        "name": path.name,
        "duration": duration,
        "fps": _rate(video) if videos else None,
        "has_video": bool(videos),
        "has_audio": bool(audios),
        "width": _positive_int(video.get("width")),
        "height": _positive_int(video.get("height")),
        "audio_channels": sum(_positive_int(stream.get("channels")) for stream in audios),
        "camera": item["camera"],
        "kind": item["kind"],
        "bytes": before["size"],
        "local_path": str(path.absolute()),
        "_source_signature": after,
        "timecode": timecode,
    }
