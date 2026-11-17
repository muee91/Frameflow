/* Read-only track rendering. Persistent review and export stay in Frameflow core. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const model = {
    clips: [], projectId: "", start: 0, end: 1, position: 0,
    zoom: 1, group: "", selected: null, width: 1, playhead: null,
    onSelect: null, onSeek: null, onDragOffset: null,
  };
  const pick = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  };
  const number = (value) => {
    const parsed = Number(value);
    return value === null || value === undefined || value === "" || !Number.isFinite(parsed)
      ? null : parsed;
  };
  const span = () => Math.max(0.001, model.end - model.start);
  const ratio = (time) => (time - model.start) / span();
  const unit = (seconds, precise = false) => {
    const abs = Math.max(0, Math.abs(seconds));
    const whole = Math.floor(abs);
    const hour = String(Math.floor(whole / 3600)).padStart(2, "0");
    const minute = String(Math.floor((whole % 3600) / 60)).padStart(2, "0");
    const second = String(whole % 60).padStart(2, "0");
    const decimal = precise ? "." + String(Math.floor((abs - whole) * 1000)).padStart(3, "0") : "";
    return (seconds < 0 ? "−" : "") + hour + ":" + minute + ":" + second + decimal;
  };
  function mappingSegments(clip) {
    if (clip.blocked_reason !== "NONLINEAR_MAPPING" || !Array.isArray(clip.mapping)) return [];
    const points = clip.mapping.map((point) => {
      const sx = point?.source, sy = point?.island;
      const source = number(sx?.value), st = number(sx?.timescale);
      const island = number(sy?.value), it = number(sy?.timescale);
      if (source === null || st <= 0 || island === null || it <= 0) return null;
      return { source: source / st, timeline: island / it };
    });
    if (points.length < 2 || points.some(point => !point)) return [];
    const segments = [];
    for (let i = 1; i < points.length; i++) {
      const a = points[i - 1], b = points[i];
      if (b.source <= a.source || b.timeline <= a.timeline ||
          !Number.isFinite(b.timeline) || !Number.isFinite(a.timeline) ||
          Math.max(Math.abs(a.timeline), Math.abs(b.timeline)) > 7 * 86400) return [];
      segments.push({ start: a.timeline, end: b.timeline,
        sourceStart: a.source, sourceEnd: b.source, index: i });
    }
    return segments;
  }
  function positionExtent(clip) {
    const parts = mappingSegments(clip);
    if (parts.length) return { start: parts[0].start, end: parts[parts.length - 1].end };
    const start = number(clip.offset_seconds), duration = number(clip.duration);
    const rate = number(clip.rate) ?? 1;
    if (clip.blocked_reason || start === null || duration === null || duration <= 0 || rate <= 0) return null;
    return { start, end: start + duration * rate };
  }
  function tracksFor(clips) {
    const found = new Map();
    for (const clip of clips) {
      const recorder = clip.kind === "recorder";
      const name = clip.camera || (recorder ? "独立录音" : "未分类机位");
      const key = (recorder ? "rec:" : "cam:") + name;
      if (!found.has(key)) found.set(key, { name, recorder, clips: [] });
      found.get(key).clips.push(clip);
    }
    const tracks = [...found.values()];
    tracks.sort((a, b) => Number(a.recorder) - Number(b.recorder) || a.name.localeCompare(b.name, "zh-CN"));
    for (const track of tracks) {
      const ends = [];
      for (const clip of [...track.clips].sort((a, b) =>
        (number(a.offset_seconds) ?? Infinity) - (number(b.offset_seconds) ?? Infinity))) {
        clip._trackRow = 0;
        const extent = positionExtent(clip);
        if (!extent) continue;
        let row = ends.findIndex((end) => end <= extent.start + 0.0001);
        if (row === -1) row = ends.length;
        ends[row] = extent.end;
        clip._trackRow = row;
      }
      track.rows = Math.max(1, ends.length);
      track.height = track.rows * 46 + 8;
    }
    return tracks;
  }
  function setZoom(factor) {
    const viewport = $("timeline-viewport");
    const previousWidth = model.width || 1;
    const marker = ratio(model.position) * previousWidth;
    const screenPosition = viewport ? marker - viewport.scrollLeft : 0;
    model.zoom = Math.min(64, Math.max(1, factor));
    render();
    if (viewport) viewport.scrollLeft = Math.max(0, ratio(model.position) * model.width - screenPosition);
  }
  function niceStep(seconds) {
    const steps = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800, 86400];
    return steps.find((step) => step >= seconds) || 86400;
  }
  function seekAt(event) {
    if (!model.onSeek) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const x = Math.min(Math.max(event.clientX - rect.left, 0), model.width);
    model.onSeek(model.start + (x / model.width) * span());
  }
  function snapOffset(clip, proposed, ignoreSnap) {
    if (ignoreSnap) return { offset: proposed, snapped: false };
    const tolerance = span() * 9 / Math.max(1, model.width);
    const length = (number(clip.duration) || 0) * (number(clip.rate) || 1);
    const candidates = [model.position, model.start];
    for (const other of model.clips) {
      if (String(other.id) === String(clip.id) || other.status === "excluded") continue;
      const extent = positionExtent(other);
      if (extent) candidates.push(extent.start, extent.end);
    }
    let best = { offset: proposed, snapped: false, distance: tolerance };
    for (const edge of candidates) {
      for (const shift of [0, length]) {
        const target = edge - shift;
        const distance = Math.abs(proposed - target);
        if (distance < best.distance) best = { offset: target, snapped: true, distance };
      }
    }
    return best;
  }
  function beginDrag(event, clip, bar) {
    if (event.button !== 0) return;
    event.stopPropagation();
    model.onSelect?.(String(clip.id));
    const original = number(clip.offset_seconds);
    if (original === null || clip.blocked_reason || clip.status === "excluded") return;
    const startX = event.clientX;
    const width = model.width;
    let moved = false;
    let target = original;
    bar.setPointerCapture(event.pointerId);
    const move = (ev) => {
      const delta = ev.clientX - startX;
      if (Math.abs(delta) > 3) moved = true;
      if (!moved) return;
      const candidate = original + delta / width * span();
      const snapped = snapOffset(clip, candidate, ev.shiftKey);
      target = snapped.offset;
      bar.classList.toggle("is-snapped", snapped.snapped);
      const left = ratio(target) * 100;
      bar.style.left = left + "%";
      bar.classList.add("is-dragging");
      const adjustment = bar.querySelector(".track-adjustment");
      if (adjustment) adjustment.textContent = (target - original >= 0 ? "+" : "") + (target - original).toFixed(3) + "s";
    };
    const finish = (ev) => {
      bar.removeEventListener("pointermove", move);
      bar.removeEventListener("pointerup", finish);
      bar.removeEventListener("pointercancel", finish);
      if (bar.hasPointerCapture(ev.pointerId)) bar.releasePointerCapture(ev.pointerId);
      bar.classList.remove("is-dragging", "is-snapped");
      bar.style.left = ratio(original) * 100 + "%";
      const adjustment = bar.querySelector(".track-adjustment");
      if (adjustment) adjustment.textContent = "";
      if (ev.type === "pointerup" && moved && model.onDragOffset)
        model.onDragOffset(String(clip.id), Number(target.toFixed(6)));
    };
    bar.addEventListener("pointermove", move);
    bar.addEventListener("pointerup", finish);
    bar.addEventListener("pointercancel", finish);
  }
  function drawRuler(body, headings) {
    headings.append(pick("div", "track-heading ruler-heading", "时间"));
    const ruler = pick("div", "track-ruler");
    ruler.style.width = model.width + "px";
    const tick = niceStep(span() * 105 / model.width);
    const first = Math.ceil(model.start / tick) * tick;
    for (let time = first, count = 0; time <= model.end + tick * 0.001 && count < 700; time += tick, count++) {
      const line = pick("span", "ruler-tick");
      line.style.left = ratio(time) * 100 + "%";
      line.append(pick("span", "ruler-label", unit(time, tick < 1)));
      ruler.append(line);
    }
    ruler.addEventListener("pointerdown", seekAt);
    body.append(ruler);
  }
  function render() {
    const viewport = $("timeline-viewport");
    const content = $("timeline-content");
    const headings = $("timeline-track-headings");
    if (!viewport || !content || !headings) return;
    const previousScroll = viewport.scrollLeft;
    const previousWidth = model.width;
    model.width = Math.max(720, Math.round(Math.max(720, viewport.clientWidth || 900) * model.zoom));
    content.replaceChildren();
    headings.replaceChildren();
    content.style.width = model.width + "px";
    drawRuler(content, headings);
    const tracks = tracksFor(model.clips);
    for (const track of tracks) {
      const label = pick("div", "track-heading" + (track.recorder ? " is-recorder" : ""));
      label.style.height = track.height + "px";
      label.append(pick("span", "track-kind", track.recorder ? "REC" : "CAM"));
      label.append(pick("strong", "track-name", track.name));
      label.append(pick("small", "track-count", String(track.clips.length)));
      headings.append(label);
      const lane = pick("div", "track-lane" + (track.recorder ? " is-recorder" : ""));
      lane.style.height = track.height + "px";
      lane.style.width = model.width + "px";
      lane.addEventListener("pointerdown", (event) => {
        if (event.target === lane) seekAt(event);
      });
      for (const clip of track.clips) {
        const offset = number(clip.offset_seconds);
        const duration = number(clip.duration);
        const rate = number(clip.rate) ?? 1;
        const pieces = mappingSegments(clip);
        if (pieces.length) {
          for (const piece of pieces) {
            const part = pick("button", "track-segment", "段 " + piece.index);
            part.type = "button";
            part.dataset.clipId = String(clip.id);
            part.style.left = ratio(piece.start) * 100 + "%";
            part.style.width = Math.max(0.001, (piece.end - piece.start) / span() * 100) + "%";
            part.style.top = (4 + (clip._trackRow || 0) * 46) + "px";
            part.title = "候选分段映射：源 " + unit(piece.sourceStart, true) + " → " +
              unit(piece.sourceEnd, true) + "，公共时间 " + unit(piece.start, true) +
              " → " + unit(piece.end, true) + "；尚未通过核对";
            part.addEventListener("click", () => model.onSelect?.(String(clip.id)));
            lane.append(part);
          }
          continue;
        }
        if (clip.blocked_reason || offset === null || duration === null || duration <= 0 || rate <= 0) {
          const missing = pick("button", "track-unmatched" + (clip.blocked_reason ? " is-blocked" : ""), clip.name || "未定位素材");
          missing.type = "button";
          missing.title = clip.blocked_reason ? "映射存在无法表达的局部变化，不展示虚假的连续同步位置" : "尚无同步位置，选择后人工处理";
          missing.addEventListener("click", () => model.onSelect?.(String(clip.id)));
          lane.append(missing);
          continue;
        }
        const bar = pick("div", "track-clip" +
          (track.recorder ? " is-audio" : " is-video") +
          (clip.status === "approved" ? " is-approved" : "") +
          (clip.status === "excluded" ? " is-excluded" : "") +
          (clip.blocked_reason ? " is-blocked" : ""));
        bar.tabIndex = 0;
        bar.setAttribute("role", "button");
        bar.setAttribute("aria-label", "选择 " + (clip.name || "素材") + "，同步起点 " + unit(offset, true));
        bar.dataset.clipId = String(clip.id);
        bar.style.left = ratio(offset) * 100 + "%";
        bar.style.width = Math.max(0.001, duration * rate / span() * 100) + "%";
        bar.style.top = (4 + (clip._trackRow || 0) * 46) + "px";
        bar.title = (clip.name || "素材") + " · " + unit(offset, true) + " · " + (clip.status || "proposed");
        if (clip.has_audio && clip.media_id && model.projectId) {
          const image = pick("img", "track-waveform");
          image.src = "/api/projects/" + encodeURIComponent(model.projectId) +
            "/waveform/" + encodeURIComponent(clip.media_id);
          image.loading = "lazy";
          image.alt = "";
          image.draggable = false;
          image.onerror = () => image.remove();
          bar.append(image);
        }
        bar.append(pick("span", "track-clip-label", clip.name || "素材"));
        bar.append(pick("span", "track-adjustment"));
        bar.addEventListener("pointerdown", (event) => beginDrag(event, clip, bar));
        bar.addEventListener("keydown", (event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            model.onSelect?.(String(clip.id));
          }
        });
        lane.append(bar);
      }
      content.append(lane);
    }
    const marker = pick("div", "track-playhead");
    marker.setAttribute("aria-hidden", "true");
    content.append(marker);
    model.playhead = marker;
    viewport.scrollLeft = previousWidth ? previousScroll / previousWidth * model.width : 0;
    setPosition(model.position);
    selectClip(model.selected);
    const zoomValue = $("zoom-level");
    if (zoomValue) zoomValue.textContent = model.zoom === 1 ? "适应" : model.zoom.toFixed(1) + "×";
    const empty = $("timeline-empty");
    if (empty) empty.hidden = tracks.length > 0;
  }
  function setPosition(position) {
    const value = number(position);
    if (value === null) return;
    model.position = value;
    if (model.playhead) model.playhead.style.left = ratio(value) * 100 + "%";
    const display = $("timeline-playhead-time");
    if (display) display.textContent = unit(value, true);
  }
  function selectClip(id) {
    model.selected = id === null || id === undefined ? null : String(id);
    const root = $("timeline-content");
    if (!root) return;
    root.querySelectorAll(".track-clip, .track-segment, .track-unmatched").forEach((clip) =>
      clip.classList.toggle("is-selected", clip.dataset.clipId === model.selected));
  }
  function mount(options) {
    const changed = model.group !== String(options.group);
    model.group = String(options.group);
    model.clips = options.clips || [];
    model.projectId = options.projectId || "";
    model.start = options.start;
    model.end = options.end;
    model.position = options.position;
    model.onSelect = options.onSelect;
    model.onSeek = options.onSeek;
    model.onDragOffset = options.onDragOffset;
    model.selected = options.selected || null;
    if (changed) model.zoom = 1;
    render();
  }
  let initialized = false;
  function init() {
    if (initialized) return;
    initialized = true;
    $("zoom-in")?.addEventListener("click", () => setZoom(model.zoom * 1.6));
    $("zoom-out")?.addEventListener("click", () => setZoom(model.zoom / 1.6));
    $("zoom-fit")?.addEventListener("click", () => setZoom(1));
    $("timeline-viewport")?.addEventListener("wheel", (event) => {
      if (!event.altKey) return;
      event.preventDefault();
      setZoom(model.zoom * (event.deltaY < 0 ? 1.25 : 0.8));
    }, { passive: false });
    window.addEventListener("resize", () => {
      if (model.clips.length) render();
    });
  }
  init();
  window.FrameflowTimeline = { mount, setPosition, selectClip, refresh: render };
})();