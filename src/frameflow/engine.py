"""Versioned adapter for the separately installed Align CLI.

Align owns matching. Frameflow owns media identity, review and task lifecycle.
Raw engine JSON contains local paths and stays in the private task directory.
"""

from __future__ import annotations

from copy import deepcopy
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .errors import TaskError
from .process import run_command


SUPPORTED_VERSION = "0.3.3"
_MAX_RESULT_BYTES = 64 * 1024 * 1024
_PROGRESS = re.compile(r"^(inspect|fingerprint|match|refine|solve|export)\s+(\d+)/(\d+)(?:\s+(.*))?$")
_PHASES = {"inspect": "读取素材", "fingerprint": "提取音频指纹", "match": "查找共同声音", "refine": "精细对齐", "solve": "整理同步关系", "export": "生成交付文件"}
_EVIDENCE = {"waveform", "spannedMetadata", "timecode"}


def inspect_engine(executable: str | None = None) -> dict:
    """Fail closed on missing, unrecognised or untested engine versions."""
    candidate = executable or os.environ.get("FRAMEFLOW_ALIGN") or "align-cli"
    found = shutil.which(str(Path(candidate).expanduser()))
    if not found:
        return {"available": False, "version": None, "message": "未找到 Align 0.3.3，请安装后设置 FRAMEFLOW_ALIGN。", "executable": None}
    found = str(Path(found).absolute())
    try:
        checked = subprocess.run([found, "--version"], capture_output=True, text=True, timeout=5, check=False, shell=False)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        return {"available": False, "version": None, "message": "Align 版本检查失败，请检查本机安装。", "executable": found}
    match = re.fullmatch(r"align-cli (\d+\.\d+\.\d+)", checked.stdout.strip()) if checked.returncode == 0 else None
    version = match.group(1) if match else None
    available = version == SUPPORTED_VERSION
    return {
        "available": available,
        "version": version,
        "message": "Align 0.3.3 已就绪。" if available else "当前仅验证 Align 0.3.3；此版本未通过适配检查。",
        "executable": found,
    }


def _require_engine(executable: str) -> dict:
    info = inspect_engine(executable)
    if not info["available"]:
        code = "ENGINE_UNAVAILABLE" if info["executable"] is None else "ENGINE_VERSION_UNSUPPORTED"
        raise TaskError(code, info["message"], retryable=False)
    return info


def _invalid(message="同步引擎返回的数据无效，未生成可确认结果。") -> TaskError:
    return TaskError("ENGINE_INVALID_RESULT", message, retryable=False)


def _time(value) -> Fraction:
    if not isinstance(value, dict):
        raise _invalid()
    numerator, denominator = value.get("value"), value.get("timescale")
    if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
        raise _invalid()
    if abs(numerator) > 2**63 - 1 or denominator > 2**31 - 1:
        raise _invalid()
    return Fraction(numerator, denominator)


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _invalid()
    return float(value)


def _path_key(value) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise _invalid("同步引擎返回了无法对应本机素材的路径。")
    return os.path.normcase(os.path.normpath(value))


def _read_json(path: Path):
    try:
        if path.stat().st_size > _MAX_RESULT_BYTES:
            raise _invalid("同步结果过大，无法安全读取。")
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _invalid("同步引擎没有返回完整有效的 JSON 结果。") from exc


def _mapping(points, duration: float) -> tuple[float | None, float, str | None]:
    """Validate all points. Never collapse a nonlinear map to its endpoints."""
    if not isinstance(points, list) or len(points) < 2:
        raise _invalid("同步引擎缺少完整的时间映射。")
    pairs = []
    for point in points:
        if not isinstance(point, dict):
            raise _invalid()
        pairs.append((_time(point.get("source")), _time(point.get("island"))))
    if any(b[0] <= a[0] or b[1] <= a[1] for a, b in zip(pairs, pairs[1:])):
        raise _invalid("同步引擎返回了非递增时间映射。")
    # Export must retain the full source. A partial mapping is insufficient.
    if abs(float(pairs[0][0])) > 2e-6 or abs(float(pairs[-1][0]) - duration) > 0.001:
        raise _invalid("同步时间映射没有覆盖完整素材。")
    first, last = pairs[0], pairs[-1]
    rate = (last[1] - first[1]) / (last[0] - first[0])
    offset = first[1] - rate * first[0]
    if any(abs(float(y - (rate * x + offset))) > 2e-6 for x, y in pairs):
        # rate=1 is only a serializable placeholder; offset=None plus the block
        # prevents consumers from mistaking it for a playable affine mapping.
        return None, 1.0, "NONLINEAR_MAPPING"
    if not 0.5 <= float(rate) <= 2 or abs(float(offset)) > 7 * 86400:
        raise _invalid("同步引擎返回的偏移或时钟速率超出当前可核对范围。")
    return float(offset), float(rate), None


def _clip(asset: dict) -> dict:
    media_id = asset.get("media_id") or asset.get("id")
    if not isinstance(media_id, str) or not media_id:
        raise _invalid("素材缺少稳定身份，不能建立同步关系。")
    duration = _number(asset.get("duration"))
    if duration <= 0:
        raise _invalid("素材时长无效。")
    return {
        "id": media_id, "media_id": media_id,
        "name": str(asset.get("name") or Path(asset["local_path"]).name),
        "kind": asset.get("kind") if asset.get("kind") in {"camera", "recorder"} else ("camera" if asset.get("has_video") else "recorder"),
        "camera": asset.get("camera", "未分类"),
        "duration": duration, "fps": asset.get("fps"),
        "has_video": bool(asset.get("has_video")), "has_audio": bool(asset.get("has_audio")),
        "width": asset.get("width", 0), "height": asset.get("height", 0),
        "audio_channels": asset.get("audio_channels", 0),
        "group_id": None, "offset_seconds": None, "rate": 1.0,
        "status": "unmatched", "reason": "未找到足够的同步证据，请补充核对或明确排除。",
        "evidence": [], "engine_clip_id": None,
    }


def _normalise(raw, assets: list[dict]) -> dict:
    if not isinstance(raw, dict) or not isinstance(raw.get("project"), dict):
        raise _invalid()
    if raw.get("stopped"):
        raise _invalid("同步引擎只返回了中断的阶段，不能当作完整结果。")
    project = raw["project"]
    for field, owner in [("clips", project), ("warnings", project), ("islands", raw), ("unmatched", raw), ("matches", raw)]:
        if not isinstance(owner.get(field), list):
            raise _invalid()
    clips = [_clip(asset) for asset in assets]
    by_path = {_path_key(asset["local_path"]): clip for asset, clip in zip(assets, clips)}
    if len(by_path) != len(assets) or len({c["media_id"] for c in clips}) != len(clips):
        raise TaskError("DUPLICATE_MEDIA", "素材重复，请先合并相同内容的本机绑定。", retryable=False)
    by_engine: dict[str, dict] = {}
    for item in project["clips"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            raise _invalid()
        key = _path_key(item.get("url"))
        if key not in by_path or item["id"] in by_engine or by_path[key]["engine_clip_id"] is not None:
            raise _invalid("同步引擎返回了重复或未授权的素材引用。")
        clip = by_path[key]
        if abs(float(_time(item.get("duration"))) - clip["duration"]) > 0.1:
            raise _invalid("同步引擎读取的素材时长与已验证素材不一致。")
        clip["engine_clip_id"] = item["id"]
        by_engine[item["id"]] = clip
    evidence_edges = []
    for match in raw["matches"]:
        if not isinstance(match, dict) or match.get("left") not in by_engine or match.get("right") not in by_engine:
            raise _invalid()
        left, right, evidence = match["left"], match["right"], match.get("evidence")
        if left == right or evidence not in _EVIDENCE:
            continue
        confidence = _number(match.get("confidence"))
        if not 0 <= confidence <= 1:
            raise _invalid()
        if evidence == "waveform":
            anchors = match.get("anchors")
            if type(anchors) is not int or anchors < 3 or _time(match.get("covered")) < 3 or not 0 <= _time(match.get("residual")) <= Fraction(1, 10):
                continue
        evidence_edges.append((left, right, evidence))
    unmatched = raw["unmatched"]
    if any(not isinstance(clip_id, str) or clip_id not in by_engine for clip_id in unmatched):
        raise _invalid()
    unmatched_ids = set(unmatched)
    placed = set()
    groups = []
    for island in raw["islands"]:
        if not isinstance(island, dict) or not isinstance(island.get("placements"), list):
            raise _invalid()
        placements = {}
        for placement in island["placements"]:
            if not isinstance(placement, dict) or not isinstance(placement.get("clipID"), str):
                raise _invalid()
            clip_id = placement["clipID"]
            if clip_id not in by_engine or clip_id in placed or clip_id in unmatched_ids:
                raise _invalid("同步引擎的片段状态相互冲突。")
            placed.add(clip_id)
            placements[clip_id] = placement
        # Only components joined by explicit evidence receive a proposed group.
        # A singleton or an unsupported edge never becomes a sync result.
        neighbours = {clip_id: set() for clip_id in placements}
        for left, right, evidence in evidence_edges:
            if left in placements and right in placements:
                neighbours[left].add(right)
                neighbours[right].add(left)
        unseen = set(placements)
        while unseen:
            pending = [min(unseen)]
            component = set()
            while pending:
                current = pending.pop()
                if current in component:
                    continue
                component.add(current)
                pending.extend(neighbours[current] - component)
            unseen -= component
            if len(component) < 2:
                continue
            stable_ids = sorted(by_engine[key]["media_id"] for key in component)
            group_id = "group-" + hashlib.sha256("\n".join(stable_ids).encode()).hexdigest()[:16]
            groups.append({"id": group_id, "name": f"同步组 {len(groups) + 1}"})
            for key in sorted(component):
                clip, placement = by_engine[key], placements[key]
                mapping = placement.get("mapping")
                if not isinstance(mapping, dict):
                    raise _invalid()
                points = mapping.get("points")
                offset, rate, blocked = _mapping(points, clip["duration"])
                evidence = sorted({e for a, b, e in evidence_edges if a in component and b in component and key in {a, b}})
                reason = "已有音频同步建议，请播放核对后确认。" if "waveform" in evidence else "仅有时间码或连续文件元数据证据，必须人工核对。"
                if abs(rate - 1.0) > 1e-9:
                    reason += f" 检测到时钟漂移 {(rate - 1) * 1_000_000:.3f} ppm；漂移交付尚未验收。"
                if blocked:
                    reason = "存在局部分段时间映射，已保留全部锚点；当前预览与导出不作线性近似。"
                clip.update(group_id=group_id, offset_seconds=offset, rate=rate, status="proposed", reason=reason, evidence=evidence, mapping=deepcopy(points))
                if blocked:
                    clip["blocked_reason"] = blocked
    warnings = []
    for warning in project["warnings"]:
        if not isinstance(warning, dict):
            raise _invalid()
        # Full original diagnostics remain in local raw JSON. Shareable results
        # name an enrolled asset only, never forward arbitrary absolute paths.
        source = by_path.get(_path_key(warning["url"])) if isinstance(warning.get("url"), str) and Path(warning["url"]).is_absolute() else None
        message = str(warning.get("message", "")).lower()
        category = "音频解码异常" if any(term in message for term in ("decode", "audio", "pcm")) else "媒体分析异常"
        warnings.append(f"{source['name'] if source else '部分素材'}：{category}，请查看本机诊断并人工核对。")
    omitted = [clip for clip in clips if clip["engine_clip_id"] is None]
    for clip in omitted:
        clip["reason"] = "同步引擎未返回此素材，可能不支持其格式；素材仍保留在清单中。"
    if omitted:
        warnings.append(f"同步引擎未返回 {len(omitted)} 个素材，已全部保留为未匹配。")
    return {"engine": {"name": "Align", "version": SUPPORTED_VERSION}, "groups": groups, "warnings": warnings, "clips": clips}


def _progress(ctx, *, exporting=False, allowed_names=None):
    def receive(line: str):
        match = _PROGRESS.fullmatch(line.strip())
        if not match:
            return
        phase, done, total, name = match.groups()
        completed, count = int(done), int(total)
        if completed > count:
            return
        current = name if name and (allowed_names is None or name in allowed_names) else None
        ctx.progress("export" if exporting else "sync", completed, count, unit="pairs" if phase in {"match", "refine"} else "files", current_item=current, message=f"{_PHASES[phase]}（{completed}/{count}）。")
    return receive


def _run_engine(argv, ctx, *, on_line, **options):
    """Expose useful failure categories without forwarding private tool text."""
    failure = None

    def receive(line):
        nonlocal failure
        text = line.lower()
        if text.startswith("error:"):
            if "decode" in text or "invalid pcm" in text:
                failure = ("ENGINE_AUDIO_DECODE_FAILED", "同步工具无法解码音频，请检查素材编码与本机 FFmpeg。")
            elif "permission denied" in text:
                failure = ("ENGINE_PERMISSION_DENIED", "同步工具没有素材或输出目录的读写权限。")
            elif "out of memory" in text or "memory allocation" in text:
                failure = ("ENGINE_MEMORY_LIMIT", "同步工具内存不足，请缩小本次素材范围后重试。")
            elif "not found" in text or "no such file" in text:
                failure = ("ENGINE_SOURCE_MISSING", "同步工具无法找到所需文件或依赖，请检查素材绑定与工具安装。")
        on_line(line)

    try:
        return run_command(argv, ctx, on_line=receive, **options)
    except TaskError as exc:
        if exc.code == "PROCESS_FAILED" and failure:
            raise TaskError(failure[0], failure[1], retryable=True) from exc
        raise


def synchronize(executable: str, assets: list[dict], run_dir: Path, ctx) -> dict:
    ctx.check_cancel()
    info = _require_engine(executable)
    if not assets:
        raise TaskError("NO_MEDIA", "没有可同步的素材。", retryable=False)
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    target, partial = run_dir / "align-result.json", run_dir / "align-result.json.part"
    if target.exists() or partial.exists():
        raise TaskError("ENGINE_RUN_EXISTS", "本次任务目录已有结果，请创建新的重试记录。", retryable=False)
    ctx.activity("开始本机音频与时间证据分析。")
    try:
        _run_engine([info["executable"], "sync", "--match-threshold", "conservative", *[asset["local_path"] for asset in assets]], ctx, timeout=12 * 3600, stdout_path=partial, on_line=_progress(ctx, allowed_names={asset["name"] for asset in assets}), cwd=run_dir)
        raw = _read_json(partial)
        result = _normalise(raw, assets)
        ctx.check_cancel()
        os.replace(partial, target)
        ctx.activity(f"分析完成：{len(result['groups'])} 个同步组，{sum(c['status'] == 'unmatched' for c in result['clips'])} 个未匹配素材；等待人工核对。")
        return result
    finally:
        partial.unlink(missing_ok=True)


def _media_time(value: Fraction) -> dict:
    value = value.limit_denominator(1_000_000_000)
    return {"value": value.numerator, "timescale": value.denominator}


def export_fcpxml(executable: str, raw_path: Path, result: dict, output_dir: Path, ctx, *,
                  verified_paths: dict[str, Path] | None = None) -> Path:
    """Generate experimental FCPXML after explicit review; no NLE claim.

The output directory must be empty and reserved for this invocation. On failure
its partial artifacts are removed; the immutable source result is never edited.
The caller must supply current, identity-checked local bindings. Raw engine URLs
are historical evidence and must never select the media used for delivery.
"""
    ctx.check_cancel()
    info = _require_engine(executable)
    clips = result.get("clips")
    if not isinstance(clips, list) or any(c.get("status") not in {"approved", "excluded"} for c in clips):
        raise TaskError("REVIEW_REQUIRED", "请逐一确认或排除素材后再导出。", retryable=False)
    approved = [c for c in clips if c["status"] == "approved"]
    if not approved:
        raise TaskError("NO_APPROVED_MEDIA", "没有已确认素材可供导出。", retryable=False)
    if any(c.get("blocked_reason") or abs(_number(c.get("rate")) - 1.0) > 1e-9 for c in approved):
        raise TaskError("DRIFT_EXPORT_UNVERIFIED", "漂移或分段时间映射的交付尚未验收，请先排除此类片段。", retryable=False)
    groups = {c.get("group_id") for c in approved}
    if None in groups or not groups <= {g["id"] for g in result.get("groups", [])}:
        raise TaskError("REVIEW_REQUIRED", "已确认素材缺少有效的同步组。", retryable=False)
    if len(groups) != 1:
        raise TaskError("MULTIPLE_GROUPS_EXPORT_PENDING", "当前交付仅支持一个已核对同步组；多个独立组不会自动拼接。", retryable=False)
    raw = deepcopy(_read_json(Path(raw_path)))
    if not isinstance(raw, dict) or not isinstance(raw.get("project"), dict) or not isinstance(raw["project"].get("clips"), list):
        raise _invalid()
    by_engine = {c["id"]: c for c in raw["project"]["clips"]}
    keys = [c.get("engine_clip_id") for c in approved]
    if any(k not in by_engine for k in keys) or len(keys) != len(set(keys)):
        raise TaskError("EXPORT_MEDIA_MISSING", "已确认素材缺少可导出的引擎媒体记录。", retryable=False)
    # Recheck immutable mappings, so setting a displayed rate to 1 cannot hide
    # an originally nonlinear/drifting result from the export gate.
    original = {p["clipID"]: p for g in raw.get("islands", []) for p in g.get("placements", [])}
    for clip in approved:
        placement = original.get(clip["engine_clip_id"])
        if placement:
            _, rate, blocked = _mapping(placement.get("mapping", {}).get("points"), clip["duration"])
            if blocked or abs(rate - 1.0) > 1e-9:
                raise TaskError("DRIFT_EXPORT_UNVERIFIED", "原同步结果包含未验收的漂移或分段映射。", retryable=False)
    if not isinstance(verified_paths, dict) or any(c["media_id"] not in verified_paths for c in approved):
        raise TaskError("EXPORT_BINDINGS_REQUIRED", "导出需要逐一核验当前本机素材位置，请重新发起导出任务。", retryable=False)
    for clip in approved:
        try:
            path = Path(verified_paths[clip["media_id"]])
            if not path.is_absolute() or not path.is_file() or path.is_symlink() or path.resolve() != path:
                raise ValueError("invalid local binding")
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise TaskError("EXPORT_BINDING_INVALID", "已核验素材位置已失效，请重新检查本机素材。", retryable=True) from exc
        by_engine[clip["engine_clip_id"]]["url"] = str(path)
    offsets = [_number(c.get("offset_seconds")) for c in approved]
    if any(abs(x) > 7 * 86400 for x in offsets):
        raise _invalid("人工偏移超出允许范围。")
    origin = min(0.0, min(offsets))
    placements = []
    for clip, offset in zip(approved, offsets):
        duration = _time(by_engine[clip["engine_clip_id"]]["duration"])
        start = Fraction(str(offset)) - Fraction(str(origin))
        confidence = original.get(clip["engine_clip_id"], {}).get("confidence", 0.0)
        placements.append({"clipID": clip["engine_clip_id"], "confidence": confidence, "mapping": {"points": [{"source": {"value": 0, "timescale": 1}, "island": _media_time(start)}, {"source": _media_time(duration), "island": _media_time(start + duration)}]}})
    raw["project"]["clips"] = [by_engine[key] for key in keys]
    raw["project"]["warnings"] = []
    raw["project"]["importedTimeline"] = None
    raw.update(islands=[{"id": 0, "placements": placements}], unmatched=[], matches=[])
    for key in ("stages", "selectedStage", "stopped", "preserveEditingTracks"):
        raw.pop(key, None)
    output_dir = Path(output_dir).absolute()
    if output_dir.is_symlink() or (output_dir.exists() and (not output_dir.is_dir() or any(output_dir.iterdir()))):
        raise TaskError("OUTPUT_EXISTS", "交付目录已有文件，请使用新的空目录。", retryable=False)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="frameflow-export-", dir=output_dir.parent) as temporary:
            reviewed_path = Path(temporary) / "reviewed.json"
            reviewed_path.write_text(json.dumps(raw, ensure_ascii=False, allow_nan=False), encoding="utf-8")
            returned = _run_engine([info["executable"], "export-json", "--no-drift", "--unmatched", "remove", "--prevent-group-overlaps", str(reviewed_path), str(output_dir)], ctx, timeout=12 * 3600, on_line=_progress(ctx, exporting=True, allowed_names={c["name"] for c in approved}), cwd=output_dir)
        try:
            artifacts = json.loads(returned)
        except (json.JSONDecodeError, TypeError) as exc:
            raise _invalid("交付工具没有返回有效文件清单。") from exc
        if not isinstance(artifacts, list):
            raise _invalid()
        fcpxml = []
        for artifact in artifacts:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("url"), str):
                raise _invalid()
            path = Path(artifact["url"])
            if not path.is_absolute() or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(output_dir.resolve()):
                raise TaskError("EXPORT_PATH_INVALID", "交付工具返回了任务目录外或无效的文件。", retryable=False)
            if artifact.get("format") == "finalCutProXML":
                fcpxml.append(path)
        if len(fcpxml) != 1:
            raise TaskError("FCPXML_MISSING", "交付工具没有生成唯一的 FCPXML 文件。", retryable=False)
        ctx.check_cancel()
        ctx.activity("FCPXML 已生成；仍需在目标剪辑软件中验证导入、机位切换与音频。")
        return fcpxml[0]
    except BaseException:
        # This directory was empty and reserved for this call, so all contents
        # are our incomplete artifacts; original media is never in this tree.
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
