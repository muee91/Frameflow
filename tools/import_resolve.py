#!/usr/bin/env python3
"""Import one reviewed Frameflow group into a NEW Resolve 21.1 project.

Run with Resolve's bundled ResolvePython, with Resolve at Project Manager:
    ResolvePython tools/import_resolve.py --manifest ... --fcpxml ... --name ...

The portable manifest owns timing; FCPXML supplies private local bindings and the
flat master. Native multicam uses the official SDK, never a second audio match.
Files are read-only. No network, project overwrite or automatic NLE acceptance.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import json
import math
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname
import xml.etree.ElementTree as ET


def prepare(manifest_path, fcpxml_path):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest.get("product") != "Frameflow" or manifest.get("schema_version") != 1:
        raise ValueError("需要 Frameflow v1 同步清单。")
    clips = manifest.get("clips", [])
    if not clips or any(c.get("status") not in {"approved", "excluded"} for c in clips):
        raise ValueError("请先核对全部素材。")
    clips = [dict(c) for c in clips if c["status"] == "approved"]
    if not clips or len({c.get("group_id") for c in clips}) != 1 or not clips[0].get("group_id"):
        raise ValueError("原生交付仅接受一个已核对同步组。")
    if any(c.get("blocked_reason") or not math.isfinite(c.get("rate", 0)) or
           abs(c["rate"] - 1) > 1e-9 for c in clips):
        raise ValueError("分段/漂移交付尚未验证，不能在 Resolve 中压平成恒定偏移。")
    rates = {Fraction(c["fps"]) for c in clips if c.get("has_video")}
    if len(rates) != 1 or sum(bool(c.get("has_video")) for c in clips) < 2:
        raise ValueError("首验适配要求至少两机位、相同帧率。")
    fps = rates.pop()
    if not 1 <= fps <= 120:
        raise ValueError("项目帧率超出首验范围。")
    for c in clips:
        if (not math.isfinite(c.get("offset_seconds", math.nan)) or
                not math.isfinite(c.get("duration", math.nan)) or c["duration"] <= 0):
            raise ValueError("同步清单的时间范围无效。")
        if c.get("has_audio") and c.get("audio_channels") not in {1, 2}:
            raise ValueError("此首验桥接仅验证了单声道/立体声；其它声道布局保持阻断。")
    common_start = max(c["offset_seconds"] for c in clips)
    common_end = min(c["offset_seconds"] + c["duration"] for c in clips)
    if common_end - common_start < 2 / float(fps):
        raise ValueError("此原生多机位入口需要共同重叠区间；不会裁切素材制造重叠。")
    common = common_start + min(1.0, (common_end - common_start) / 2)
    paths = {}
    doc = ET.parse(fcpxml_path)
    for rep in doc.findall("./resources/asset/media-rep"):
        if rep.get("kind") != "original-media":
            continue
        url = urlsplit(rep.get("src", ""))
        if url.scheme != "file" or url.netloc not in {"", "localhost"}:
            raise ValueError("FCPXML 必须仅引用本机原始媒体。")
        path = Path(url2pathname(url.path))
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ValueError("导出引用的本机素材已离线。")
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("读取期间源素材发生变化。")
        paths["sha256:" + digest.hexdigest()] = str(path.resolve())
    for c in clips:
        if c.get("media_id") not in paths:
            raise ValueError("FCPXML 本机绑定与同步清单的完整内容身份不一致。")
        c["local_path"] = paths[c["media_id"]]
        c["mark_in"] = round((common - c["offset_seconds"]) * float(fps))
        c["mark_out"] = math.floor(c["duration"] * float(fps)) - 1
        if not 0 <= c["mark_in"] <= c["mark_out"]:
            raise ValueError("同步参考点不在完整来源范围内。")
    return {"clips": clips, "fps": float(fps), "revision": manifest["revision"]}


def _items(folder):
    yield from folder.GetClipList()
    for child in folder.GetSubFolderList():
        yield from _items(child)


def import_project(plan, fcpxml_path, name):
    import DaVinciResolveScript

    resolve = DaVinciResolveScript.scriptapp("Resolve")
    if not resolve or not resolve.GetVersionString().startswith("21.1."):
        raise RuntimeError("需要正在运行、允许本机 SDK 连接的 Resolve 21.1；不会自动更改权限。")
    pm = resolve.GetProjectManager()
    current = pm.GetCurrentProject()
    if current and (current.GetTimelineCount() or current.GetMediaPool().GetRootFolder().GetClipList() or
                    current.GetMediaPool().GetRootFolder().GetSubFolderList()):
        raise RuntimeError("请先保存并关闭当前工程、返回项目管理器；导入不会替你关闭现有工作。")
    if name in pm.GetProjectListInCurrentFolder():
        raise RuntimeError("项目同名，未覆盖。请使用新名称。")
    project = pm.CreateProject(name)
    if not project:
        raise RuntimeError("无法创建独立验收项目。")
    if not project.SetSetting("timelineFrameRate", str(plan["fps"])):
        raise RuntimeError("Resolve 拒绝项目帧率。")
    mp = project.GetMediaPool()
    # Import only the flat master: the experimental FCPXML multicam is not a
    # verified Resolve object. The original exported document stays untouched.
    import tempfile
    with tempfile.TemporaryDirectory(prefix="frameflow-resolve-") as temporary:
        doc = ET.parse(fcpxml_path)
        for event in doc.findall(".//event"):
            for entry in list(event):
                if entry.tag == "project" and entry.find(".//mc-clip") is not None:
                    event.remove(entry)
        master_path = Path(temporary) / "master.fcpxml"
        doc.write(master_path, encoding="utf-8", xml_declaration=True)
        master = mp.ImportTimelineFromFile(str(master_path), {"timelineName": "Frameflow 完整多轨母版"})
    if not master:
        raise RuntimeError("Resolve 未导入多轨母版；本验收项目保留供检查。")
    by_path = {str(Path(item.GetClipProperty("File Path")).resolve()): item
               for item in _items(mp.GetRootFolder()) if item.GetClipProperty("File Path")}
    sources = []
    try:
        for clip in plan["clips"]:
            item = by_path.get(clip["local_path"])
            if not item:
                raise RuntimeError("导入后的素材绑定不完整。")
            if any(item.GetMarkInOut().values()):
                raise RuntimeError("新导入素材已有入出点，未覆盖现有标记。")
            if not item.SetMarkInOut(clip["mark_in"], clip["mark_out"]):
                raise RuntimeError("Resolve 拒绝同步参考入点。")
            sources.append(item)
        native = mp.CreateMulticamClip(sources, {
            "name": "Frameflow 原生多机位", "frameRate": plan["fps"],
            "angleSyncMode": resolve.MULTICAM_ANGLE_SYNC_IN, "useFullClipExtents": True,
            "multicamAudioMode": resolve.MULTICAM_AUDIO_ALL,
            "angleNameMode": resolve.MULTICAM_ANGLE_NAME_FILE, "createBinForSourceClips": False})
        if not native or len(native) != 1:
            raise RuntimeError("Resolve 未产生唯一原生多机位对象，母版保留。")
        check = mp.CreateTimelineFromClips("Frameflow 多机位验收", [{"mediaPoolItem": native[0]}])
        if not check:
            raise RuntimeError("无法建立独立多机位验收时间线。")
        project.SetCurrentTimeline(check)
    finally:
        for item in sources:
            if not item.ClearMarkInOut():
                raise RuntimeError("验收项目的临时入出点未能清除，请检查。")
        pm.SaveProject()
    return {"project": name, "resolve": resolve.GetVersionString(), "revision": plan["revision"],
            "source_count": len(sources), "native_multicam_objects": len(native),
            "native_type": native[0].GetClipProperty("Type"),
            "nle_import_verified": False,
            "note": "已建立母版和原生对象；仍需界面验收角度、监听、完整范围、保存重开与重链。"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--fcpxml", type=Path, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    plan = prepare(args.manifest, args.fcpxml)
    print(json.dumps(import_project(plan, args.fcpxml, args.name), ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
