"""FFmpeg-rendered real audio peak images, cached only inside the local workspace."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from .errors import TaskError

_WAVEFORM_LOCK = threading.Lock()
_FILTER_VERSION = "showwavespic-1024x66-v1"
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def cached_waveform(path: Path, cache_root: Path, media_id: str, signature: dict) -> Path:
    """Only render caller-authorized media. Never ship a synthetic waveform as evidence."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise TaskError("FFMPEG_MISSING", "无法生成音频波形：本机缺少 FFmpeg。", retryable=False)
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    key = hashlib.sha256(json.dumps(
        {"id": media_id, "signature": signature, "filter": _FILTER_VERSION},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    output = cache_root / (key + ".png")
    if output.is_file():
        return output
    with _WAVEFORM_LOCK:
        if output.is_file():
            return output
        handle, temporary = tempfile.mkstemp(prefix="wave-", suffix=".png", dir=cache_root)
        os.close(handle)
        temp_path = Path(temporary)
        try:
            command = [
                ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-threads", "1", "-i", str(path),
                "-filter_complex",
                "[0:a:0]aformat=channel_layouts=mono,"
                "showwavespic=s=1024x66:colors=white:scale=sqrt[wave]",
                "-map", "[wave]", "-frames:v", "1", "-update", "1",
                "-y", str(temp_path),
            ]
            completed = subprocess.run(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=60, check=False,
            )
            if completed.returncode != 0 or not temp_path.is_file():
                raise TaskError("WAVEFORM_UNAVAILABLE", "该素材暂无可显示的音频波形。")
            with temp_path.open("rb") as stream:
                if stream.read(8) != _PNG_MAGIC:
                    raise TaskError("WAVEFORM_UNAVAILABLE", "音频波形生成失败。")
            if temp_path.stat().st_size > 2 * 1024 * 1024:
                raise TaskError("WAVEFORM_UNAVAILABLE", "音频波形超出预览限制。")
            os.replace(temp_path, output)
        except subprocess.TimeoutExpired as exc:
            raise TaskError("WAVEFORM_TIMEOUT", "音频波形处理超时，原始素材未改变。") from exc
        except (OSError, ValueError) as exc:
            raise TaskError("WAVEFORM_UNAVAILABLE", "无法生成当前素材的音频波形。") from exc
        finally:
            temp_path.unlink(missing_ok=True)
    return output
