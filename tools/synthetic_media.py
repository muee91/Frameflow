"""Generate deterministic, non-private media for Frameflow acceptance checks.

The B camera soundtrack has 1.25 seconds of leading silence. To align the common
sound, B starts at zero and A/the recorder start at +1.25 seconds in the shared
timeline. These are synthetic synchronization fixtures, not footage from a shoot.

Usage: python tools/synthetic_media.py /an/empty/output/directory
"""

from __future__ import annotations

import argparse
from array import array
import json
import math
from pathlib import Path
import random
import shutil
import subprocess
import sys
import tempfile
import wave


def _write_wave(path: Path, samples: array, sample_rate: int, channels=1) -> None:
    data = samples
    if channels == 2:
        data = array("h")
        for value in samples:
            data.extend((value, round(value * 0.85)))
    if sys.byteorder != "little":
        data = array("h", data)
        data.byteswap()
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(data.tobytes())


def generate_media(output_dir: Path, *, duration: float = 60.0, delay: float = 1.25, ffmpeg=None) -> list[dict]:
    """Create two H264/AAC-LC MP4s and one stereo PCM WAV in an empty folder.

    Returns API-ready roots. ``ground_truth.json`` contains relative filenames,
    source durations and the known alignment; no existing files are overwritten.
    60 seconds gives the conservative matcher adequate unique anchor coverage.
    """
    if not math.isfinite(duration) or not 30 <= duration <= 180:
        raise ValueError("duration must be between 30 and 180 seconds")
    if not math.isfinite(delay) or not 0 < delay <= 5:
        raise ValueError("delay must be between 0 and 5 seconds")
    executable = shutil.which(str(ffmpeg or "ffmpeg"))
    if not executable:
        raise RuntimeError("FFmpeg is required to generate synthetic video")
    root = Path(output_dir).expanduser().resolve()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("Choose a new or empty output directory")
    root.mkdir(parents=True, exist_ok=True)
    sample_rate, fps = 16000, 25
    rng = random.Random(37128)
    count = math.ceil(duration * 4)
    tones = [(rng.uniform(220, 1100), rng.uniform(1500, 2800), rng.uniform(0.3, 0.85)) for _ in range(count)]
    samples = array("h")
    for index in range(round(sample_rate * duration)):
        timestamp = index / sample_rate
        low, high, amplitude = tones[min(count - 1, int(timestamp * 4))]
        envelope = math.sin(math.pi * (timestamp * 4 % 1)) ** 2
        value = round(32767 * amplitude * envelope * (
            0.42 * math.sin(2 * math.pi * low * timestamp)
            + 0.24 * math.sin(2 * math.pi * high * timestamp)
            + 0.05 * rng.uniform(-1, 1)))
        samples.append(value)
    camera_a, camera_b, recorder = root / "camera_A", root / "camera_B", root / "recorder"
    for directory in (camera_a, camera_b, recorder):
        directory.mkdir()
    _write_wave(recorder / "recorder.wav", samples, sample_rate, channels=2)
    with tempfile.TemporaryDirectory(prefix="frameflow-fixture-", dir=root) as staging:
        staging = Path(staging)
        source = staging / "reference.wav"
        shifted = staging / "shifted.wav"
        _write_wave(source, samples, sample_rate)
        _write_wave(shifted, array("h", [0] * round(delay * sample_rate)) + samples, sample_rate)
        for target, soundtrack, length, visual in [
            (camera_a / "A.mp4", source, duration, "testsrc2"),
            (camera_b / "B.mp4", shifted, duration + delay, "smptebars"),
        ]:
            command = [executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                       "-f", "lavfi", "-i", f"{visual}=size=320x180:rate={fps}", "-i", str(soundtrack),
                       "-map", "0:v:0", "-map", "1:a:0", "-t", str(length),
                       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "128k", "-ar", "48000",
                       "-movflags", "+faststart", str(target)]
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=180, check=False)
            if result.returncode:
                raise RuntimeError("FFmpeg synthetic fixture generation failed: " + result.stderr.decode(errors="replace")[-1000:])
    truth = {
        "synthetic": True,
        "seed": 37128,
        "fps": fps,
        "note": "B includes leading silence; B starts before A/recorder to align the identical sound.",
        "clips": [
            {"name": "A.mp4", "relative_path": "camera_A/A.mp4", "duration": duration, "offset_seconds": delay},
            {"name": "B.mp4", "relative_path": "camera_B/B.mp4", "duration": math.ceil((duration + delay) * fps) / fps, "offset_seconds": 0.0},
            {"name": "recorder.wav", "relative_path": "recorder/recorder.wav", "duration": duration, "offset_seconds": delay},
        ],
    }
    (root / "ground_truth.json").write_text(json.dumps(truth, ensure_ascii=False, indent=2), encoding="utf-8")
    return [{"path": str(camera_a), "label": "A", "kind": "camera"},
            {"path": str(camera_b), "label": "B", "kind": "camera"},
            {"path": str(recorder), "label": "独立录音", "kind": "recorder"}]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--delay", type=float, default=1.25)
    parser.add_argument("--ffmpeg")
    arguments = parser.parse_args()
    roots = generate_media(arguments.output, duration=arguments.duration, delay=arguments.delay, ffmpeg=arguments.ffmpeg)
    print(json.dumps({"name": "Frameflow 合成验收项目", "roots": roots}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
