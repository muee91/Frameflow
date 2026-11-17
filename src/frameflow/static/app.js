/* Frameflow local workflow. No media or project data leaves this origin. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = {
    project: null,
    job: null,
    result: null,
    system: null,
    generation: 0,
    eventAfter: 0,
    events: [],
    pollTimer: null,
    polling: false,
    sourceSequence: 0,
    loadedResultJob: null,
    loadingResultJob: null,
    selectedGroup: null,
    players: new Map(),
    playing: false,
    buffering: false,
    startingPlayback: false,
    playbackToken: 0,
    masterPlayer: null,
    playbackHelp: "",
    position: 0,
    start: 0,
    end: 1,
    clockTime: 0,
    clockPosition: 0,
    animation: null,
    reviewVisible: false,
    creating: false,
    actionPending: false,
    jobDisplayKey: null,
  };
  const activeStates = new Set(["queued", "running", "cancelling"]);
  const stateTitles = {
    queued: "任务已排队",
    running: "正在同步素材",
    cancelling: "正在安全取消",
    cancelled: "任务已取消",
    failed: "同步未完成",
    interrupted: "任务已中断",
    needs_review: "同步候选已准备好",
    succeeded: "同步结果已核对",
  };
  const stageTitles = {
    scan: "识别素材",
    probe: "读取媒体信息",
    hash: "校验素材身份",
    sync: "寻找同步关系",
    review: "准备核对",
    export: "导出工程候选",
  };
  const statusTitles = {
    proposed: "待核对",
    unmatched: "未匹配",
    approved: "已确认",
    excluded: "已排除",
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function showNotice(message, kind = "info") {
    $("notice").textContent = message;
    $("notice").className = `notice ${kind}`;
    $("notice").hidden = !message;
  }
  function errorText(value) {
    if (typeof value === "string") return value;
    if (Array.isArray(value))
      return value
        .map((item) => item.msg || item.message || String(item))
        .join("；");
    if (value && typeof value === "object")
      return (
        value.message ||
        value.error_message ||
        value.code ||
        JSON.stringify(value)
      );
    return "请求未完成，请重试。";
  }
  async function request(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
      ...options,
      headers: {
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...options.headers,
      },
    });
    if (!response.ok) {
      let message = `请求未完成（${response.status}）`;
      try {
        const data = await response.json();
        message = errorText(data.detail || data.error || data);
      } catch (_) {
        /* Preserve HTTP status. */
      }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    if (response.status === 204) return null;
    return response.json();
  }
  function post(url, body = {}) {
    return request(url, { method: "POST", body: JSON.stringify(body) });
  }
  function timestamp(value) {
    if (typeof value === "number") return value < 1e12 ? value * 1000 : value;
    if (!value) return null;
    const result = Date.parse(value);
    return Number.isFinite(result) ? result : null;
  }
  function ageSeconds(value) {
    const at = timestamp(value);
    return at === null ? null : Math.max(0, (Date.now() - at) / 1000);
  }
  function humanSeconds(value) {
    const seconds = Math.max(0, Math.floor(value || 0));
    if (seconds < 60) return `${seconds} 秒`;
    if (seconds < 3600)
      return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
    return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
  }
  function timecode(value) {
    const negative = value < 0;
    const total = Math.round(Math.abs(Number(value) || 0) * 1000);
    const ms = total % 1000;
    const sec = Math.floor(total / 1000) % 60;
    const min = Math.floor(total / 60000) % 60;
    const hrs = Math.floor(total / 3600000);
    return `${negative ? "−" : ""}${hrs ? `${String(hrs).padStart(2, "0")}:` : ""}${String(min).padStart(2, "0")}:${String(sec).padStart(2, "0")}.${String(ms).padStart(3, "0")}`;
  }
  function numeric(value, fallback = null) {
    if (value === null || value === undefined || value === "") return fallback;
    if (typeof value === "string" && value.includes("/")) {
      const [a, b] = value.split("/").map(Number);
      return b && Number.isFinite(a / b) ? a / b : fallback;
    }
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  }
  function positiveFrameRate(value) {
    if (typeof value === "number")
      return Number.isFinite(value) && value > 0 ? value : null;
    if (typeof value !== "string") return null;
    const match = value.trim().match(/^(\d+(?:\.\d+)?)(?:\/(\d+(?:\.\d+)?))?$/);
    if (!match) return null;
    const numerator = Number(match[1]);
    const denominator = match[2] === undefined ? 1 : Number(match[2]);
    return numerator > 0 &&
      denominator > 0 &&
      Number.isFinite(numerator / denominator)
      ? numerator / denominator
      : null;
  }
  function setStep(step) {
    document.querySelectorAll(".steps li").forEach((li) => {
      const index = Number(li.dataset.step);
      li.classList.toggle("done", index < step);
      if (index === step) li.setAttribute("aria-current", "step");
      else li.removeAttribute("aria-current");
    });
  }
  function available(value) {
    if (typeof value === "boolean") return value;
    if (value && typeof value === "object") return value.available !== false;
    return Boolean(value);
  }
  function renderSystem() {
    if (!state.system) return;
    const engine = state.system.engine || {};
    const missing = [];
    if (!available(state.system.ffprobe)) missing.push("ffprobe");
    if (!available(state.system.ffmpeg)) missing.push("FFmpeg");
    if (!engine.available) missing.push("Align 同步引擎");
    const target = $("system-status");
    target.classList.toggle("problem", missing.length > 0);
    if (missing.length) {
      target.textContent = `需要配置 ${missing.join("、")}。${engine.message || "创建项目后，任务会明确报告缺失项。"}`;
    } else {
      target.textContent = `本机处理工具已就绪${engine.version ? ` · Align ${engine.version}` : ""}。素材无需上传。`;
    }
  }
  function addSource(kind = "camera") {
    const id = ++state.sourceSequence;
    const row = element("div", "source-row");
    row.dataset.kind = kind;
    const count =
      document.querySelectorAll(`.source-row[data-kind="${kind}"]`).length + 1;
    const kindTag = element(
      "span",
      `source-kind ${kind === "recorder" ? "recorder" : ""}`,
      kind === "recorder" ? "独立录音" : "摄影机",
    );
    const name = element("input", "source-name");
    name.id = `source-name-${id}`;
    name.setAttribute(
      "aria-label",
      kind === "recorder" ? "录音来源名称" : "机位名称",
    );
    name.placeholder =
      kind === "recorder"
        ? `录音 ${count}`
        : `机位 ${String.fromCharCode(64 + Math.min(count, 26))}`;
    name.value = name.placeholder;
    name.maxLength = 80;
    const path = element("input", "source-path");
    path.id = `source-path-${id}`;
    path.setAttribute(
      "aria-label",
      `${kind === "recorder" ? "录音" : "视频"}目录`,
    );
    path.placeholder = "选择目录，或粘贴本机目录路径";
    path.autocomplete = "off";
    const pick = element("button", "button quiet pick-directory", "选择目录");
    pick.type = "button";
    pick.addEventListener("click", async () => {
      pick.disabled = true;
      pick.textContent = "选择中…";
      try {
        const answer = await post("/api/pick-directory");
        if (answer && answer.path) {
          path.value = answer.path;
          path.focus();
        }
      } catch (error) {
        showNotice(
          `目录选择器未能打开：${error.message} 可直接粘贴本机目录路径。`,
          "warning",
        );
        path.focus();
      } finally {
        pick.disabled = false;
        pick.textContent = "选择目录";
      }
    });
    const remove = element("button", "remove-source", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", "移除这个素材来源");
    remove.addEventListener("click", () => {
      if (document.querySelectorAll(".source-row").length === 1) {
        showNotice("请至少保留一个素材目录。", "warning");
        return;
      }
      row.remove();
    });
    row.append(kindTag, name, path, pick, remove);
    $("source-rows").append(row);
  }
  async function loadProjects(selectId = null) {
    const data = await request("/api/projects");
    const projects = Array.isArray(data) ? data : data.projects || [];
    $("project-select").replaceChildren(new Option("打开已有项目", ""));
    for (const project of projects)
      $("project-select").add(new Option(project.name, project.id));
    if (selectId) $("project-select").value = selectId;
    return projects;
  }
  function resetProject() {
    state.generation += 1;
    pausePlayback();
    state.project = null;
    state.job = null;
    state.result = null;
    state.eventAfter = 0;
    state.events = [];
    state.loadedResultJob = null;
    state.loadingResultJob = null;
    state.reviewVisible = false;
    state.players.clear();
    $("review-panel").hidden = true;
    $("export-panel").hidden = true;
    $("job-panel").hidden = true;
    $("start-sync").hidden = true;
    $("connection-notice").hidden = true;
    $("clip-grid").replaceChildren();
  }
  function newProject() {
    resetProject();
    $("project-view").hidden = true;
    $("create-view").hidden = false;
    $("project-select").value = "";
    showNotice("");
    setStep(0);
    $("project-name").focus();
  }
  async function openProject(id) {
    resetProject();
    const generation = state.generation;
    showNotice("");
    try {
      const project = await request(`/api/projects/${encodeURIComponent(id)}`);
      if (generation !== state.generation) return;
      state.project = project;
      $("create-view").hidden = true;
      $("project-view").hidden = false;
      $("project-title").textContent = project.name;
      $("project-select").value = project.id;
      const roots = project.roots || [];
      const cameras = roots.filter((root) => root.kind === "camera").length;
      const recorders = roots.filter((root) => root.kind === "recorder").length;
      $("project-summary").textContent =
        `${cameras} 个机位来源 · ${recorders} 个独立录音来源 · 本机目录绑定`;
      const data = await request(
        `/api/projects/${encodeURIComponent(id)}/jobs`,
      );
      if (generation !== state.generation) return;
      const jobs = Array.isArray(data) ? data : data.jobs || [];
      if (jobs.length) {
        state.job = jobs[0];
        renderJob();
        await loadEvents(generation);
        if (
          state.job.kind === "export" ||
          ["needs_review", "succeeded"].includes(state.job.state)
        )
          await loadResult(generation, true);
      } else {
        $("start-sync").hidden = false;
        setStep(1);
      }
    } catch (error) {
      if (generation === state.generation) showNotice(error.message, "error");
    }
  }
  async function createProject(event) {
    event.preventDefault();
    if (state.creating) return;
    const name = $("project-name").value.trim();
    const roots = [...document.querySelectorAll(".source-row")]
      .map((row) => ({
        path: row.querySelector(".source-path").value.trim(),
        label: row.querySelector(".source-name").value.trim(),
        kind: row.dataset.kind,
      }))
      .filter((source) => source.path);
    if (!name || !roots.length) {
      showNotice("请填写项目名称，并选择至少一个素材目录。", "warning");
      return;
    }
    if (roots.some((root) => !root.label)) {
      showNotice("请给每个素材来源填写机位或录音名称。", "warning");
      return;
    }
    state.creating = true;
    $("create-submit").disabled = true;
    $("create-submit").textContent = "正在创建…";
    showNotice("");
    try {
      const project = await post("/api/projects", { name, roots });
      await loadProjects(project.id);
      await openProject(project.id);
      await startJob();
    } catch (error) {
      showNotice(error.message, "error");
    } finally {
      state.creating = false;
      $("create-submit").disabled = false;
      $("create-submit").textContent = "创建项目并开始同步 ↗";
    }
  }
  async function startJob(retry = false) {
    if (!state.project || state.actionPending) return;
    state.actionPending = true;
    pausePlayback();
    const generation = state.generation;
    const projectId = state.project.id;
    try {
      const job =
        retry && state.job
          ? await post(`/api/jobs/${encodeURIComponent(state.job.id)}/retry`)
          : await post(`/api/projects/${encodeURIComponent(projectId)}/jobs`);
      if (generation !== state.generation) return;
      state.job = job;
      state.events = [];
      state.eventAfter = 0;
      if (job.kind !== "export") {
        state.result = null;
        state.loadedResultJob = null;
        state.reviewVisible = false;
        $("review-panel").hidden = true;
        $("export-panel").hidden = true;
      }
      $("start-sync").hidden = true;
      renderEvents();
      renderJob();
      showNotice("");
    } catch (error) {
      if (generation === state.generation) showNotice(error.message, "error");
    } finally {
      state.actionPending = false;
    }
  }
  async function cancelJob() {
    if (!state.job || state.actionPending) return;
    state.actionPending = true;
    $("cancel-job").disabled = true;
    const generation = state.generation;
    try {
      const updated = await post(
        `/api/jobs/${encodeURIComponent(state.job.id)}/cancel`,
      );
      if (generation !== state.generation) return;
      if (updated && updated.id) state.job = updated;
      else state.job = { ...state.job, state: "cancelling" };
      renderJob();
    } catch (error) {
      if (generation === state.generation) showNotice(error.message, "error");
    } finally {
      state.actionPending = false;
      $("cancel-job").disabled = false;
    }
  }
  function renderJob() {
    const job = state.job;
    if (!job) return;
    $("job-panel").hidden = false;
    const isFinished = ["needs_review", "succeeded"].includes(job.state);
    $("job-panel").classList.toggle("is-finished", isFinished);
    const displayKey = `${job.id}:${job.state}`;
    if (state.jobDisplayKey !== displayKey) {
      $("activity-details").open = !isFinished;
      state.jobDisplayKey = displayKey;
    }
    $("job-title").textContent =
      job.kind === "export"
        ? {
            running: "正在导出工程候选",
            queued: "导出任务已排队",
            succeeded: "工程候选已生成",
            failed: "工程导出未完成",
          }[job.state] ||
          stateTitles[job.state] ||
          job.state
        : stateTitles[job.state] || job.state;
    $("job-attempt").textContent =
      `${job.kind === "export" ? "工程导出" : "同步任务"} · 第 ${job.attempt || 1} 次尝试`;
    $("job-state-dot").className = `large-dot ${job.state}`;
    $("job-message").textContent =
      job.message ||
      (job.state === "needs_review"
        ? "候选位置已生成。请逐项播放核对，尚未视为可交付结果。"
        : "等待处理器更新进展。 ");
    $("cancel-job").hidden = !["queued", "running", "cancelling"].includes(
      job.state,
    );
    $("cancel-job").disabled = job.state === "cancelling";
    $("cancel-job").textContent =
      job.state === "cancelling" ? "等待子进程退出…" : "安全取消";
    $("retry-job").hidden = !["cancelled", "failed", "interrupted"].includes(
      job.state,
    );
    $("open-review").hidden = !state.result;
    $("download-export").hidden = !(
      job.kind === "export" &&
      job.state === "succeeded" &&
      job.artifact
    );
    document.querySelector(".stage-list").hidden = job.kind === "export";
    $("current-item").textContent =
      job.current_item ||
      (activeStates.has(job.state)
        ? stageTitles[job.stage] || "等待分配任务"
        : "当前没有运行中的文件");
    const total = numeric(job.total);
    const completed = numeric(job.completed, 0);
    const measurable = total !== null && total > 0;
    const percent = measurable
      ? Math.max(0, Math.min(100, (completed / total) * 100))
      : 0;
    $("progress-fill").style.width = `${percent}%`;
    const progress = $("progress-track");
    if (measurable) {
      progress.setAttribute("aria-valuemin", "0");
      progress.setAttribute("aria-valuemax", String(total));
      progress.setAttribute(
        "aria-valuenow",
        String(Math.min(total, completed)),
      );
      progress.setAttribute(
        "aria-valuetext",
        `${stageTitles[job.stage] || job.stage}，${completed} / ${total}`,
      );
    } else {
      progress.removeAttribute("aria-valuenow");
      progress.setAttribute("aria-valuetext", "当前步骤没有可量化进度");
    }
    const unitMap = {
      files: "个文件",
      bytes: "字节",
      items: "项",
      pairs: "对候选",
      clips: "段素材",
    };
    const unit = unitMap[job.unit] || job.unit || "项";
    $("progress-label").textContent =
      `${stageTitles[job.stage] || job.stage || "准备中"}${measurable ? ` · ${completed.toLocaleString()} / ${total.toLocaleString()} ${unit}` : " · 进度由处理器报告"}`;
    const stages = ["scan", "probe", "sync", "review"];
    const stageIndex = stages.indexOf(
      job.stage === "hash" ? "probe" : job.stage,
    );
    document.querySelectorAll(".stage-list li").forEach((li, index) => {
      li.classList.toggle("active", index === stageIndex);
      li.classList.toggle("done", stageIndex > index);
    });
    $("job-error").hidden = !job.error_code && !job.error_message;
    $("job-error-code").textContent = job.error_code || "任务异常";
    $("job-error-message").textContent =
      job.error_message || "请查看最近活动，修正问题后重试。";
    if (!state.reviewVisible) setStep(1);
    renderJobHealth();
  }
  function renderJobHealth() {
    const job = state.job;
    if (!job) return;
    const hbAge = ageSeconds(job.heartbeat_at);
    const progressAge = ageSeconds(job.progress_at);
    const started = timestamp(job.started_at || job.created_at);
    const ended = timestamp(job.finished_at) || Date.now();
    $("elapsed-time").textContent = started
      ? `已用时 ${humanSeconds((ended - started) / 1000)}`
      : "";
    $("heartbeat-label").textContent =
      hbAge === null
        ? "尚未收到处理器心跳"
        : `最近心跳 ${humanSeconds(hbAge)}前`;
    $("progress-age-label").textContent =
      progressAge === null
        ? "尚无阶段进展"
        : `最近进展 ${humanSeconds(progressAge)}前`;
    let health = "";
    if (activeStates.has(job.state)) {
      if (
        (hbAge !== null && hbAge > 10) ||
        (hbAge === null && ageSeconds(job.created_at) > 10)
      ) {
        health =
          "最近没有收到处理器心跳，任务可能已失去响应。当前状态尚未确认；可以安全取消或等待恢复。";
      } else if (
        progressAge !== null &&
        progressAge > 30 &&
        job.state === "running"
      ) {
        health = `仍收到处理器心跳，当前步骤已 ${humanSeconds(progressAge)}没有新进展。可继续等待或安全取消。`;
      }
    }
    $("job-health").textContent = health;
    $("job-health").hidden = !health;
  }
  async function loadEvents(generation) {
    if (!state.job) return;
    const jobId = state.job.id;
    const data = await request(
      `/api/jobs/${encodeURIComponent(jobId)}/events?after=${state.eventAfter}`,
    );
    if (generation !== state.generation || state.job?.id !== jobId) return;
    const events = Array.isArray(data) ? data : data.events || [];
    if (!events.length) return;
    for (const event of events) {
      state.eventAfter = Math.max(state.eventAfter, Number(event.seq) || 0);
      if (!state.events.some((existing) => existing.seq === event.seq))
        state.events.push(event);
    }
    state.events = state.events.slice(-100);
    renderEvents();
  }
  function renderEvents() {
    const target = $("activity-list");
    target.replaceChildren();
    if (!state.events.length)
      target.append(
        element("li", "empty-activity", "任务产生新活动后会显示在这里。"),
      );
    for (const event of [...state.events].reverse()) {
      const li = element("li", event.level || "info");
      const time = element(
        "time",
        "",
        timestamp(event.created_at)
          ? new Date(timestamp(event.created_at)).toLocaleTimeString("zh-CN", {
              hour12: false,
            })
          : "—",
      );
      const body = element(
        "span",
        "event-message",
        event.message || stageTitles[event.stage] || event.stage || "状态更新",
      );
      if (event.current_item)
        body.append(element("span", "event-item", event.current_item));
      li.append(time, body);
      target.append(li);
    }
    $("activity-count").textContent = state.events.length
      ? `最近 ${state.events.length} 条`
      : "";
  }
  async function loadResult(generation = state.generation, reveal = false) {
    if (!state.project || !state.job) return;
    const jobId = state.job.id;
    if (state.loadingResultJob === jobId) return;
    state.loadingResultJob = jobId;
    try {
      const result = await request(
        `/api/projects/${encodeURIComponent(state.project.id)}/result`,
      );
      if (generation !== state.generation || state.job?.id !== jobId) return;
      if (
        result.job_id &&
        result.job_id !== jobId &&
        state.job.kind !== "export"
      )
        return;
      state.result = result;
      state.loadedResultJob = result.job_id || jobId;
      renderResult();
      $("open-review").hidden = false;
      if (reveal) revealReview(false);
    } catch (error) {
      if (error.status !== 404 && generation === state.generation)
        showNotice(`读取同步结果失败：${error.message}`, "error");
    } finally {
      if (state.loadingResultJob === jobId) state.loadingResultJob = null;
    }
  }
  function revealReview(scroll = true) {
    if (!state.result) return;
    state.reviewVisible = true;
    $("review-panel").hidden = false;
    $("export-panel").hidden = false;
    setStep(2);
    if (scroll)
      $("review-panel").scrollIntoView({
        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
          ? "auto"
          : "smooth",
        block: "start",
      });
  }
  function renderResult() {
    if (!state.result) return;
    pausePlayback();
    const result = state.result;
    const clips = result.clips || [];
    const reviewed = clips.filter((clip) =>
      ["approved", "excluded"].includes(clip.status),
    ).length;
    $("review-count").textContent = `${reviewed} / ${clips.length} 项已处理`;
    $("result-revision").textContent =
      `修订 ${result.revision ?? "—"} · ${typeof result.engine === "string" ? result.engine : result.engine?.name || "同步结果"}`;
    const warnings = Array.isArray(result.warnings)
      ? result.warnings
      : result.warnings
        ? [result.warnings]
        : [];
    $("result-warnings").replaceChildren();
    for (const warning of warnings)
      $("result-warnings").append(
        element(
          "p",
          "",
          typeof warning === "string"
            ? warning
            : warning.message || JSON.stringify(warning),
        ),
      );
    $("result-warnings").hidden = warnings.length === 0;
    const groups = [...(result.groups || [])];
    for (const clip of clips) {
      if (
        clip.group_id !== null &&
        clip.group_id !== undefined &&
        !groups.some((group) => String(group.id) === String(clip.group_id))
      )
        groups.push({ id: clip.group_id, name: `同步组 ${groups.length + 1}` });
    }
    const groupSelect = $("group-select");
    groupSelect.replaceChildren();
    for (const group of groups)
      groupSelect.add(
        new Option(group.name || `同步组 ${group.id}`, String(group.id)),
      );
    if (
      clips.some(
        (clip) => clip.group_id === null || clip.group_id === undefined,
      )
    )
      groupSelect.add(new Option("未匹配素材 · 单独核对", "__unmatched__"));
    if (!groupSelect.options.length)
      groupSelect.add(new Option("暂无素材", "__empty__"));
    const oldGroup = state.selectedGroup;
    if ([...groupSelect.options].some((option) => option.value === oldGroup))
      groupSelect.value = oldGroup;
    state.selectedGroup = groupSelect.value;
    renderGroup();
    const approved = clips.filter((clip) => clip.status === "approved").length;
    const pending = clips.length - reviewed;
    const ready = pending === 0 && approved > 0;
    $("export-manifest").disabled = !ready;
    $("export-fcpxml").disabled = !ready;
    $("export-gate").textContent = ready
      ? `${approved} 项已确认，允许生成候选交付`
      : pending
        ? `还有 ${pending} 项需要核对`
        : "至少确认一项素材后交付";
    $("export-gate").classList.toggle("ready", ready);
    $("export-description").textContent = ready
      ? `本次确认 ${approved} 项素材，明确排除 ${clips.length - approved} 项。可导出同步关系；工程兼容性仍需目标软件验证。`
      : "先确认需要交付的素材；不参与本次交付的素材可以明确排除。";
  }
  function groupClips() {
    return (state.result?.clips || []).filter((clip) =>
      state.selectedGroup === "__unmatched__"
        ? clip.group_id === null || clip.group_id === undefined
        : String(clip.group_id) === state.selectedGroup,
    );
  }
  function hasMapping(clip) {
    return (
      numeric(clip.offset_seconds) !== null &&
      numeric(clip.rate, 1) > 0 &&
      clip.group_id !== null &&
      clip.group_id !== undefined
    );
  }
  function safeMediaUrl(value) {
    if (!value || typeof value !== "string") return null;
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin && url.pathname.startsWith("/api/")
        ? url.href
        : null;
    } catch (_) {
      return null;
    }
  }
  function renderGroup() {
    pausePlayback();
    state.players.clear();
    const clips = groupClips();
    const mapped = clips.filter(
      (clip) => hasMapping(clip) && clip.status !== "excluded",
    );
    state.start = mapped.length
      ? Math.min(...mapped.map((clip) => Number(clip.offset_seconds)))
      : 0;
    state.end = mapped.length
      ? Math.max(
          ...mapped.map(
            (clip) =>
              Number(clip.offset_seconds) +
              Number(clip.duration || 0) * numeric(clip.rate, 1),
          ),
        )
      : 1;
    if (!(state.end > state.start)) state.end = state.start + 1;
    state.position = state.start;
    $("timeline-seek").min = String(state.start);
    $("timeline-seek").max = String(state.end);
    $("timeline-seek").value = String(state.start);
    $("timeline-seek").disabled = !mapped.length;
    $("timeline-time").textContent = timecode(state.start);
    $("timeline-duration").textContent = `/ ${timecode(state.end)}`;
    $("toggle-play").disabled = !mapped.length;
    $("group-summary").textContent =
      `${clips.length} 项素材${mapped.length ? ` · 跨度 ${timecode(state.end - state.start)}` : " · 没有公共同步位置"}`;
    $("playback-help").textContent = mapped.length
      ? "公共时间轴按同步位置控制素材，监听单路声音。浏览器预览用于检查；精确到帧的交付仍需在目标软件验收。"
      : "这些素材缺少可靠同步位置。可先单独播放，再选择同步组并填写人工位置，或明确排除。";
    state.playbackHelp = $("playback-help").textContent;
    const audioSelect = $("audio-select");
    audioSelect.replaceChildren(new Option("静音", ""));
    for (const clip of mapped.filter((item) => item.has_audio))
      audioSelect.add(
        new Option(
          `${clip.camera || clip.kind || "音轨"} · ${clip.name}`,
          String(clip.id),
        ),
      );
    const preferred =
      mapped.find((clip) => clip.kind === "recorder" && clip.has_audio) ||
      mapped.find((clip) => clip.has_audio);
    audioSelect.value = preferred ? String(preferred.id) : "";
    $("clip-grid").replaceChildren();
    for (const clip of clips) $("clip-grid").append(createClipCard(clip));
    if (!clips.length)
      $("clip-grid").append(element("p", "no-clips", "当前没有可核对的素材。"));
    updateTransport();
  }
  function createClipCard(clip) {
    const mapped = hasMapping(clip);
    const linked = mapped && clip.status !== "excluded";
    const card = element("article", "clip-card");
    const top = element("div", "clip-top");
    const heading = element("div", "clip-heading");
    heading.append(
      element(
        "span",
        "eyebrow",
        clip.camera || (clip.kind === "recorder" ? "独立录音" : "摄影机"),
      ),
      element("h3", "", clip.name || "未命名素材"),
    );
    top.append(
      heading,
      element(
        "span",
        `clip-status ${clip.status || "proposed"}`,
        statusTitles[clip.status] || clip.status || "待核对",
      ),
    );
    const wrap = element(
      "div",
      `media-wrap${clip.has_video ? "" : " audio-only"}`,
    );
    const media = element(clip.has_video ? "video" : "audio");
    media.preload = "metadata";
    media.controls = !linked;
    media.playsInline = true;
    media.muted = true;
    media.setAttribute("aria-label", `${clip.name} 播放器`);
    const url = safeMediaUrl(clip.media_url);
    if (url) media.src = url;
    if (!clip.has_video)
      wrap.append(element("span", "audio-symbol", "▂ ▆ ▃ ▇ ▄ ▂"));
    wrap.append(media);
    const overlay = element(
      "span",
      "media-overlay",
      mapped ? "等待共同播放" : "单独播放 · 未同步",
    );
    wrap.append(overlay);
    const playbackError = element(
      "div",
      "media-error",
      "浏览器无法播放此素材，当前页不能确认同步。请检查文件与编码；仍可明确排除本次交付。",
    );
    playbackError.hidden = true;
    wrap.append(playbackError);
    const player = { media, clip, overlay, wrap, failed: false, active: false };
    state.players.set(String(clip.id), player);
    media.addEventListener("error", () => {
      if (state.players.get(String(clip.id)) !== player) return;
      player.failed = true;
      if (player.approveButton) player.approveButton.disabled = true;
      playbackError.hidden = false;
      if (state.playing) {
        pausePlayback();
        showNotice(
          "某项素材播放失败，已暂停公共时间轴。请查看对应素材窗口。",
          "warning",
        );
      }
    });
    media.addEventListener("loadedmetadata", () => {
      if (mapped) syncPlayer(player, false);
    });
    media.addEventListener("waiting", () => {
      if (
        state.players.get(String(clip.id)) === player &&
        state.playing &&
        player.active &&
        (media.readyState < 3 || media.seeking)
      )
        beginBuffering();
    });
    for (const event of ["canplay", "canplaythrough", "seeked", "loadeddata"]) {
      media.addEventListener(event, () => {
        if (state.players.get(String(clip.id)) === player) tryResumePlayback();
      });
    }
    if (!url) {
      player.failed = true;
      playbackError.textContent =
        "没有可用的本机播放地址，请检查素材绑定。当前页不能确认同步。";
      playbackError.hidden = false;
    }
    if (!linked) {
      media.controls = true;
      media.addEventListener("play", () => {
        pausePlayback(media);
        for (const other of state.players.values())
          if (other !== player) other.media.pause();
        media.muted = false;
      });
    }
    const info = element("div", "clip-info");
    const evidence = Array.isArray(clip.evidence)
      ? clip.evidence
          .map((item) =>
            typeof item === "string"
              ? item
              : item.type || item.evidence || item.kind || "同步证据",
          )
          .join(" · ")
      : typeof clip.evidence === "object" && clip.evidence
        ? clip.evidence.type ||
          clip.evidence.kind ||
          JSON.stringify(clip.evidence)
        : clip.evidence;
    const evidenceNames = {
      waveform: "音频波形",
      timecode: "时间码",
      spannedMetadata: "连续录制元数据",
      manual: "人工位置",
      reference: "参考素材",
    };
    const label = evidenceNames[evidence] || evidence || "尚无可靠匹配证据";
    info.append(
      element(
        "p",
        "clip-evidence",
        `${timecode(clip.duration || 0)} · ${label}${numeric(clip.rate, 1) !== 1 ? ` · 时间比例 ${numeric(clip.rate, 1).toFixed(7)}` : ""}`,
      ),
    );
    if (clip.reason)
      info.append(
        element(
          "p",
          "clip-reason",
          typeof clip.reason === "string"
            ? clip.reason
            : JSON.stringify(clip.reason),
        ),
      );
    if (clip.blocked_reason)
      info.append(
        element(
          "p",
          "clip-reason",
          `无法自动交付：${clip.blocked_reason}。请单独核对，当前可排除。`,
        ),
      );
    const form = element("form", "clip-form");
    const offsetLabel = element("label", "field");
    offsetLabel.append(element("span", "", "在同步组中的起点（秒）"));
    const offset = element("input");
    offset.type = "number";
    offset.step = "0.001";
    offset.value = mapped ? String(numeric(clip.offset_seconds, 0)) : "";
    offset.placeholder = "填写人工位置";
    offset.setAttribute("aria-label", `${clip.name} 同步起点秒数`);
    offsetLabel.append(offset);
    const nudges = element("div", "frame-nudges");
    const fps = clip.has_video ? positiveFrameRate(clip.fps) : null;
    const rate = numeric(clip.rate, 1);
    const step = fps && rate > 0 ? rate / fps : 0.01;
    const stepLabel = fps && rate > 0 ? "1 帧" : "10 毫秒";
    const nudgeBack = element(
      "button",
      "button quiet compact",
      `− ${stepLabel}`,
    );
    nudgeBack.type = "button";
    nudgeBack.setAttribute("aria-label", `${clip.name} 向前微调${stepLabel}`);
    nudgeBack.addEventListener("click", () => {
      offset.value = (numeric(offset.value, 0) - step).toFixed(6);
      pausePlayback();
    });
    const nudge = element("button", "button quiet compact", `+ ${stepLabel}`);
    nudge.type = "button";
    nudge.setAttribute("aria-label", `${clip.name} 向后微调${stepLabel}`);
    nudge.addEventListener("click", () => {
      offset.value = (numeric(offset.value, 0) + step).toFixed(6);
      pausePlayback();
    });
    nudges.append(nudgeBack, nudge);
    form.append(offsetLabel, nudges);
    let groupInput = null;
    if (!mapped) {
      const groupLabel = element("label", "field full");
      groupLabel.append(element("span", "", "人工加入同步组"));
      groupInput = element("select");
      groupInput.add(new Option("请选择同步组", ""));
      for (const group of state.result.groups || [])
        groupInput.add(
          new Option(group.name || `同步组 ${group.id}`, String(group.id)),
        );
      groupLabel.append(groupInput);
      form.append(groupLabel);
    }
    const reasonLabel = element("label", "field full");
    reasonLabel.append(element("span", "", "核对备注 / 修正依据"));
    const reason = element("textarea");
    reason.rows = 1;
    reason.maxLength = 1000;
    reason.placeholder = "例如：口型与独立录音一致；或说明排除原因";
    reason.setAttribute("aria-label", `${clip.name} 核对备注`);
    reasonLabel.append(reason);
    form.append(reasonLabel);
    const actions = element("div", "clip-save-actions");
    const approve = element(
      "button",
      "button primary compact",
      clip.status === "approved" ? "保存修正并确认" : "确认位置",
    );
    approve.type = "submit";
    const exclude = element(
      "button",
      "button quiet compact",
      clip.status === "excluded" ? "更新排除说明" : "排除本次交付",
    );
    exclude.type = "button";
    player.approveButton = approve;
    approve.disabled = Boolean(clip.blocked_reason || player.failed);
    offset.disabled = Boolean(clip.blocked_reason);
    nudge.disabled = Boolean(clip.blocked_reason);
    nudgeBack.disabled = Boolean(clip.blocked_reason);
    if (groupInput) groupInput.disabled = Boolean(clip.blocked_reason);
    actions.append(approve, exclude);
    form.append(actions);
    const save = async (status) => {
      if (status === "approved" && (clip.blocked_reason || player.failed)) {
        showNotice(
          "当前素材无法在此页完成播放核对，不能确认；可修复播放问题或明确排除。",
          "warning",
        );
        return;
      }
      const offsetValue = numeric(offset.value);
      const groupValue = groupInput ? groupInput.value : clip.group_id;
      if (
        status === "approved" &&
        (offsetValue === null ||
          groupValue === "" ||
          groupValue === null ||
          groupValue === undefined)
      ) {
        showNotice("确认位置前，请填写有效起点并选择同步组。", "warning");
        return;
      }
      if (!reason.value.trim()) {
        showNotice("请填写修正或排除的依据，方便之后追溯。", "warning");
        reason.focus();
        return;
      }
      pausePlayback();
      approve.disabled = true;
      exclude.disabled = true;
      const generation = state.generation;
      try {
        const body = {
          revision: state.result.revision,
          clip_id: clip.id,
          offset_seconds: offsetValue,
          status,
          reason: reason.value.trim(),
        };
        if (
          groupValue !== "" &&
          groupValue !== null &&
          groupValue !== undefined
        )
          body.group_id = groupValue;
        const answer = await post(
          `/api/projects/${encodeURIComponent(state.project.id)}/review`,
          body,
        );
        if (generation !== state.generation) return;
        if (answer && Array.isArray(answer.clips)) {
          state.result = answer;
          renderResult();
        } else await loadResult(generation);
        showNotice(
          status === "approved"
            ? "位置已保存。修正与核对记录已更新。"
            : "已明确排除该素材，交付时会记录排除状态。",
        );
      } catch (error) {
        if (generation !== state.generation) return;
        if (error.status === 409) {
          showNotice(
            "结果已被其他操作更新，已重新读取。请检查最新位置后再次保存。",
            "warning",
          );
          await loadResult(generation);
        } else showNotice(error.message, "error");
      } finally {
        approve.disabled = Boolean(clip.blocked_reason || player.failed);
        exclude.disabled = false;
      }
    };
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      save("approved");
    });
    exclude.addEventListener("click", () => save("excluded"));
    info.append(form);
    info.append(
      element(
        "p",
        "clip-annotation",
        mapped
          ? `起点 ${numeric(clip.offset_seconds).toFixed(3)} s · 修改后保存才会更新公共时间轴`
          : "未匹配素材不会自动填入零偏移。",
      ),
    );
    card.append(top, wrap, info);
    return card;
  }
  function sourcePosition(player, timeline = state.position) {
    return (
      (timeline - Number(player.clip.offset_seconds)) /
      numeric(player.clip.rate, 1)
    );
  }
  function activePlayers() {
    return [...state.players.values()].filter((player) => {
      if (!hasMapping(player.clip) || player.clip.status === "excluded")
        return false;
      const source = sourcePosition(player);
      return source >= 0 && source < Number(player.clip.duration || 0);
    });
  }
  function playbackPosition() {
    if (!state.playing || state.buffering) return state.position;
    const master = state.players.get(state.masterPlayer);
    const source = master ? sourcePosition(master) : -1;
    if (
      master &&
      source >= 0 &&
      source < Number(master.clip.duration || 0) &&
      !master.media.paused &&
      !master.media.seeking &&
      !master.media.ended
    ) {
      return Math.min(
        state.end,
        Number(master.clip.offset_seconds) +
          master.media.currentTime * numeric(master.clip.rate, 1),
      );
    }
    return Math.min(
      state.end,
      state.clockPosition + (performance.now() - state.clockTime) / 1000,
    );
  }
  function syncPlayer(player, play) {
    const { clip, media } = player;
    if (!hasMapping(clip) || clip.status === "excluded" || player.failed) {
      if (play) media.pause();
      return;
    }
    const source = sourcePosition(player);
    const active = source >= 0 && source < Number(clip.duration || 0);
    player.active = active;
    player.overlay.textContent = active
      ? `源片 ${timecode(source)}${String(clip.id) === $("audio-select").value ? " · 监听中" : ""}`
      : source < 0
        ? "这段素材尚未开始"
        : "这段素材已结束";
    media.muted = String(clip.id) !== $("audio-select").value;
    const playbackRate = 1 / numeric(clip.rate, 1);
    if (playbackRate < 0.0625 || playbackRate > 16) {
      player.failed = true;
      media.pause();
      return;
    }
    media.playbackRate = playbackRate;
    const target = Math.min(
      Math.max(0, source),
      Math.max(0, numeric(clip.duration, 0) - 0.001),
    );
    // Reassigning currentTime on every paused frame restarts decoding and can
    // itself emit waiting. Seek only when the mapped target actually differs.
    if (
      media.readyState > 0 &&
      !media.seeking &&
      Math.abs(media.currentTime - target) > (play ? 0.08 : 0.002)
    ) {
      try {
        media.currentTime = target;
      } catch (_) {
        /* Metadata is not seekable yet. */
      }
    }
    // A readiness barrier starts all active players together. Never call play()
    // repeatedly from requestAnimationFrame while decoding is still starting.
    if (!play || !active) media.pause();
  }
  function drawTransport() {
    $("timeline-seek").value = String(state.position);
    $("timeline-time").textContent = timecode(state.position);
    $("toggle-play").textContent = state.playing ? "暂停" : "播放";
    $("toggle-play").setAttribute(
      "aria-label",
      state.playing ? "暂停当前同步组" : "播放当前同步组",
    );
    $("toggle-play").dataset.playbackState = state.playing
      ? state.buffering
        ? "buffering"
        : "playing"
      : "paused";
    if (state.playbackHelp)
      $("playback-help").textContent = state.buffering
        ? "素材正在缓冲，公共时间轴保持在当前位置；媒体就绪后继续。点击暂停可取消播放。"
        : state.playbackHelp;
  }
  function updateTransport() {
    drawTransport();
    // Pending play() promises must not be aborted by an incidental UI redraw.
    if (!state.startingPlayback) {
      for (const player of state.players.values())
        syncPlayer(player, state.playing && !state.buffering);
    }
  }
  function tryResumePlayback() {
    if (!state.playing || !state.buffering || state.startingPlayback) return;
    const players = activePlayers();
    if (players.some((player) => player.failed || player.media.error)) {
      pausePlayback();
      showNotice(
        "某项素材播放失败，已停止联动。请查看对应素材窗口。",
        "warning",
      );
      return;
    }
    if (players.some(({ media }) => media.seeking || media.readyState < 3))
      return;
    const attempt = ++state.playbackToken;
    state.startingPlayback = true;
    Promise.all(players.map(({ media }) => media.play()))
      .then(() => {
        if (
          attempt !== state.playbackToken ||
          !state.playing ||
          !state.buffering
        )
          return;
        state.startingPlayback = false;
        if (
          players.some(({ media }) => media.seeking || media.readyState < 3)
        ) {
          beginBuffering();
          return;
        }
        const master =
          players.find(
            (player) => String(player.clip.id) === $("audio-select").value,
          ) || players[0];
        state.masterPlayer = master ? String(master.clip.id) : null;
        state.clockPosition = state.position;
        state.clockTime = performance.now();
        state.buffering = false;
        state.position = playbackPosition();
        drawTransport();
      })
      .catch((error) => {
        // A user pause, group change or later buffering attempt invalidates this
        // callback. It must never restart a superseded playback intention.
        if (attempt !== state.playbackToken || !state.playing) return;
        state.startingPlayback = false;
        if (error.name === "AbortError" && state.buffering) {
          beginBuffering();
          return;
        }
        pausePlayback();
        showNotice(
          "浏览器未能开始某路播放。请检查素材是否可解码，然后重新播放。",
          "warning",
        );
      });
  }
  function beginBuffering() {
    if (!state.playing) return;
    state.position = playbackPosition();
    state.playbackToken += 1;
    state.buffering = true;
    state.startingPlayback = false;
    state.masterPlayer = null;
    state.clockPosition = state.position;
    state.clockTime = performance.now();
    for (const player of state.players.values()) {
      player.media.pause();
      if (hasMapping(player.clip) && player.clip.status !== "excluded")
        player.media.preload = "auto";
    }
    updateTransport();
  }
  function pausePlayback(exceptMedia = null) {
    state.position = playbackPosition();
    state.playing = false;
    state.buffering = false;
    state.startingPlayback = false;
    state.playbackToken += 1;
    state.masterPlayer = null;
    if (state.animation !== null) cancelAnimationFrame(state.animation);
    state.animation = null;
    for (const player of state.players.values())
      if (player.media !== exceptMedia) player.media.pause();
    drawTransport();
  }
  function animationFrame() {
    if (!state.playing) return;
    if (state.buffering) {
      updateTransport();
      tryResumePlayback();
    } else {
      state.position = playbackPosition();
      state.clockPosition = state.position;
      state.clockTime = performance.now();
      if (state.position >= state.end) {
        pausePlayback();
        updateTransport();
        return;
      }
      // A newly active clip, decode stall or pending seek freezes the whole
      // group; once all active media has future data, the barrier resumes it.
      if (
        activePlayers().some(
          ({ media }) => media.paused || media.seeking || media.readyState < 3,
        )
      )
        beginBuffering();
      else updateTransport();
    }
    if (state.playing) state.animation = requestAnimationFrame(animationFrame);
  }
  function togglePlay() {
    if (state.playing) {
      pausePlayback();
      return;
    }
    if (
      !groupClips().some(
        (clip) => hasMapping(clip) && clip.status !== "excluded",
      )
    )
      return;
    if (state.position >= state.end - 0.01) state.position = state.start;
    state.playing = true;
    // Start at the requested common position. The shared clock is stopped
    // until all active play() calls have resolved with actual decoded data.
    state.buffering = true;
    beginBuffering();
    tryResumePlayback();
    state.animation = requestAnimationFrame(animationFrame);
  }
  async function downloadResponse(url, fallbackName) {
    const response = await fetch(url, {
      credentials: "same-origin",
      cache: "no-store",
    });
    if (!response.ok) {
      let message = `导出未完成（${response.status}）`;
      try {
        const body = await response.json();
        message = errorText(body.detail || body);
      } catch (_) {
        /* HTTP error fallback. */
      }
      throw new Error(message);
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    const anchor = element("a");
    anchor.href = objectUrl;
    const disposition = response.headers.get("Content-Disposition") || "";
    const filename = disposition.match(/filename="?([^";]+)"?/i)?.[1];
    anchor.download = filename || fallbackName;
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 30000);
  }
  async function exportFile() {
    if (!state.project) return;
    const button = $("export-manifest");
    button.disabled = true;
    try {
      await downloadResponse(
        `/api/projects/${encodeURIComponent(state.project.id)}/export?format=manifest`,
        "frameflow-sync.json",
      );
      setStep(3);
      showNotice(
        "同步清单已生成。该文件共享素材身份与同步关系，不包含本机绝对路径。",
      );
    } catch (error) {
      showNotice(error.message, "error");
    } finally {
      button.disabled = false;
    }
  }
  async function startExport() {
    if (!state.project || !state.result || state.actionPending) return;
    pausePlayback();
    state.actionPending = true;
    $("export-fcpxml").disabled = true;
    const generation = state.generation;
    try {
      const job = await post(
        `/api/projects/${encodeURIComponent(state.project.id)}/exports`,
        { format: "fcpxml", revision: state.result.revision },
      );
      if (generation !== state.generation) return;
      state.job = job;
      state.events = [];
      state.eventAfter = 0;
      renderEvents();
      renderJob();
      setStep(3);
      showNotice("导出任务已开始。上方会显示真实处理状态，也可以安全取消。");
      $("job-panel").scrollIntoView({ block: "start" });
    } catch (error) {
      if (generation === state.generation) showNotice(error.message, "error");
    } finally {
      state.actionPending = false;
      $("export-fcpxml").disabled = false;
    }
  }
  async function downloadExport() {
    if (!state.job || state.job.kind !== "export") return;
    $("download-export").disabled = true;
    try {
      await downloadResponse(
        `/api/jobs/${encodeURIComponent(state.job.id)}/download`,
        "frameflow-candidate.zip",
      );
      showNotice(
        "工程候选已下载。请在目标 Final Cut Pro 中核验多机位、独立音轨和时长，验收通过后再正式交付。",
      );
    } catch (error) {
      showNotice(error.message, "error");
    } finally {
      $("download-export").disabled = false;
    }
  }
  async function poll() {
    if (state.polling) return;
    state.polling = true;
    const generation = state.generation;
    try {
      if (state.project && state.job) {
        const id = state.job.id;
        const job = await request(`/api/jobs/${encodeURIComponent(id)}`);
        if (generation !== state.generation || state.job?.id !== id) return;
        state.job = job;
        renderJob();
        await loadEvents(generation);
        if (
          job.kind !== "export" &&
          ["needs_review", "succeeded"].includes(job.state) &&
          state.loadedResultJob !== id
        )
          await loadResult(generation, true);
        if (job.kind === "export" && !state.result)
          await loadResult(generation, true);
        $("connection-notice").hidden = true;
      }
      renderJobHealth();
    } catch (error) {
      if (generation === state.generation) {
        $("connection-notice").textContent =
          error.status === 401
            ? "本机连接凭据已失效。请通过 Frameflow 启动命令重新打开页面；已有项目与任务记录仍会保留。"
            : `暂时无法连接本机处理器：${error.message}。页面保留最后一次已知状态，正在尝试重连。`;
        $("connection-notice").hidden = false;
      }
    } finally {
      state.polling = false;
      state.pollTimer = setTimeout(poll, 1000);
    }
  }
  async function initialize() {
    $("create-form").addEventListener("submit", createProject);
    $("new-project").addEventListener("click", newProject);
    $("add-camera").addEventListener("click", () => addSource("camera"));
    $("add-recorder").addEventListener("click", () => addSource("recorder"));
    $("project-select").addEventListener("change", (event) => {
      if (event.target.value) openProject(event.target.value);
    });
    $("start-sync").addEventListener("click", () => startJob());
    $("cancel-job").addEventListener("click", cancelJob);
    $("retry-job").addEventListener("click", () => startJob(true));
    $("open-review").addEventListener("click", () => revealReview());
    $("group-select").addEventListener("change", () => {
      state.selectedGroup = $("group-select").value;
      renderGroup();
    });
    $("audio-select").addEventListener("change", () => {
      for (const player of state.players.values())
        player.media.muted = String(player.clip.id) !== $("audio-select").value;
    });
    $("toggle-play").addEventListener("click", togglePlay);
    $("timeline-seek").addEventListener("input", (event) => {
      const requestedPosition = numeric(event.target.value, state.start);
      pausePlayback();
      state.position = requestedPosition;
      updateTransport();
    });
    $("export-manifest").addEventListener("click", () =>
      exportFile("manifest"),
    );
    $("export-fcpxml").addEventListener("click", startExport);
    $("download-export").addEventListener("click", downloadExport);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) pausePlayback();
    });
    window.addEventListener("beforeunload", pausePlayback);
    addSource("camera");
    addSource("recorder");
    try {
      const fragment = new URLSearchParams(location.hash.slice(1));
      const token = fragment.get("token");
      if (token) {
        history.replaceState(null, "", location.pathname + location.search);
        await post("/api/session", { token });
      }
      const results = await Promise.allSettled([
        request("/api/system"),
        loadProjects(),
      ]);
      if (results[0].status === "fulfilled") {
        state.system = results[0].value;
        renderSystem();
      } else {
        $("system-status").textContent = "尚未连接到本机处理器。";
        throw results[0].reason;
      }
      if (results[1].status === "rejected") throw results[1].reason;
    } catch (error) {
      showNotice(
        error.status === 401
          ? "请通过 Frameflow 启动命令重新打开本机页面。连接凭据不会保存在项目或浏览器地址中。"
          : `初始化未完成：${error.message}`,
        "error",
      );
    }
    poll();
  }
  initialize();
})();
