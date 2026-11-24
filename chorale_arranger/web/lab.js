"use strict";

(() => {
  const VOICES = ["soprano", "alto", "tenor", "bass"];
  const VOICE_LABEL = {
    soprano: "女高 S", alto: "女低 A", tenor: "男高 T", bass: "男低 B",
  };
  const NOTE_LETTERS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
  const POLICY_LOCAL_KEY = "chorale-lab-default-policy-v1";

  const $ = (id) => document.getElementById(id);

  let catalog = [];
  let scoreMap = {};
  let voiceRanges = {};
  let currentScoreId = "breath_phrase";
  let compareData = null;
  let benchmarkData = null;
  let savedPolicy = null;
  let activeInspector = null;
  let activeTraceStep = null;
  let activePathDetail = null;
  let revealed = false;
  let chosenBlind = null;
  let blindMap = Math.random() < 0.5
    ? { A: "phrase", B: "lookahead" }
    : { A: "lookahead", B: "phrase" };

  let audioContext = null;
  let activeSource = null;
  const synthCache = {};
  const pathSynthCache = {};

  function h(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value == null) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "html") node.innerHTML = value;
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child == null || child === false) continue;
      node.appendChild(typeof child === "string" || typeof child === "number"
        ? document.createTextNode(String(child))
        : child);
    }
    return node;
  }

  async function api(path, body) {
    const resp = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
    return data;
  }

  function midiName(midi, keyRootPc = 0) {
    const pc = ((Number(midi) % 12) + 12) % 12;
    const octave = Math.floor(Number(midi) / 12) - 1;
    return `${NOTE_LETTERS[pc]}${octave}`;
  }

  function scoreMeta() {
    return scoreMap[currentScoreId] || catalog[0];
  }

  function scoreDocument() {
    return scoreMeta().document;
  }

  function formLock() {
    const pitchValue = $("lab-lock-pitch").value;
    if (pitchValue === "" || pitchValue == null) return null;
    return {
      segment: Number($("lab-lock-segment").value),
      voice: $("lab-lock-voice").value,
      midi: Number(pitchValue),
    };
  }

  // -------------------------------------------------------------------------
  // Initialization and policy
  // -------------------------------------------------------------------------

  async function init() {
    try {
      const [scoreResp, stylesResp] = await Promise.all([
        fetch("/api/experiment/scores").then((r) => r.json()),
        fetch("/api/styles").then((r) => r.json()),
      ]);
      catalog = scoreResp.scores || [];
      scoreMap = Object.fromEntries(catalog.map((s) => [s.score_id, s]));
      voiceRanges = stylesResp.voice_ranges || {};

      populateScoreSelect();
      bindControls();
      await loadPolicy();
      populateSegmentSelect();
      populatePitchSelect();
      applyPolicyToForm(savedPolicy);
      await runCompare();
      restoreBlindChoice(savedPolicy);
    } catch (err) {
      const box = $("lab-policy-status");
      if (box) {
        box.className = "banner error";
        box.textContent = `盲比实验室初始化失败：${err.message}`;
      }
      console.error(err);
    }
  }

  function populateScoreSelect() {
    const sel = $("lab-score");
    sel.innerHTML = "";
    catalog.forEach((score) => {
      sel.appendChild(h("option", {
        value: score.score_id,
        text: score.title || score.score_id,
      }));
    });
    sel.value = currentScoreId;
  }

  function bindControls() {
    $("lab-score").addEventListener("change", async (e) => {
      currentScoreId = e.target.value;
      savedPolicy = await fetchPolicy(currentScoreId);
      populateSegmentSelect();
      populatePitchSelect();
      applyPolicyToForm(savedPolicy);
      await runCompare();
      restoreBlindChoice(savedPolicy);
    });
    $("lab-lock-segment").addEventListener("change", () => {
      populatePitchSelect();
      runCompare();
    });
    $("lab-lock-voice").addEventListener("change", () => {
      populatePitchSelect();
      runCompare();
    });
    $("lab-lock-pitch").addEventListener("change", () => runCompare());
    $("lab-priority").addEventListener("change", () => runCompare());
    $("lab-planner").addEventListener("change", switchPlannerFocus);
    $("lab-run").addEventListener("click", () => runCompare());
    $("lab-save-policy").addEventListener("click", () => savePolicy());
    $("lab-benchmark").addEventListener("click", runBenchmark);
    $("lab-import").addEventListener("click", importToMainEditor);
    $("lab-play-a").addEventListener("click", () => playSample("A"));
    $("lab-play-b").addEventListener("click", () => playSample("B"));
    $("lab-stop").addEventListener("click", stopAudio);
    $("lab-reveal").addEventListener("click", () => {
      revealed = true;
      renderBlindStatus();
      renderCards();
      renderPaths();
    });
    $("lab-choose-a").addEventListener("click", () => chooseBlind("A"));
    $("lab-choose-b").addEventListener("click", () => chooseBlind("B"));
    $("lab-undo-next").addEventListener("click", undoNextLock);
    $("lab-before-first-violation").addEventListener("click", showBeforeFirstViolation);

    $("lab-sample-voice-buttons").addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-sample]");
      if (!btn) return;
      playSample(btn.dataset.sample, btn.dataset.voice);
    });
    $("lab-paths").addEventListener("click", (e) => {
      const preview = e.target.closest("button[data-path-audio]");
      const detail = e.target.closest("button[data-path-detail]");
      if (preview) auditionLabPath(preview.dataset.pathAudio);
      if (detail) {
        activePathDetail = detail.dataset.pathDetail;
        renderPathDetail();
      }
    });
  }

  function populateSegmentSelect() {
    const sel = $("lab-lock-segment");
    const doc = scoreDocument();
    sel.innerHTML = "";
    doc.segments.forEach((seg, i) => {
      const chord = seg.chord_candidates?.[0]?.function
        || seg.chord_candidates?.[0]?.label
        || seg.chord_candidates?.[0]?.quality
        || "?";
      sel.appendChild(h("option", { value: i, text: `第 ${i + 1} 段 · ${chord}` }));
    });
    sel.value = "0";
  }

  function pitchOptions() {
    const segment = Number($("lab-lock-segment").value || 0);
    const voice = $("lab-lock-voice").value;
    const entry = (scoreMeta().lock_options || [])
      .find((x) => x.segment === segment && x.voice === voice);
    return entry?.pitches || [];
  }

  function populatePitchSelect(preferredMidi = null) {
    const sel = $("lab-lock-pitch");
    sel.innerHTML = "";
    sel.appendChild(h("option", { value: "", text: "不额外锁音（保留谱面固定音）" }));
    pitchOptions().forEach((p) => {
      sel.appendChild(h("option", { value: p.midi, text: `${p.name} (MIDI ${p.midi})` }));
    });
    if (preferredMidi != null && [...sel.options].some((o) => o.value === String(preferredMidi))) {
      sel.value = String(preferredMidi);
    } else {
      sel.value = "";
    }
  }

  async function fetchPolicy(scoreId) {
    try {
      const fp = scoreMeta()?.fingerprint || "";
      const qs = new URLSearchParams({ score_id: scoreId, fingerprint: fp });
      const data = await fetch(`/api/experiment/policy?${qs}`).then((r) => r.json());
      if (data.policy) return data.policy;
    } catch (_) {
      // Fall through to localStorage.
    }
    const local = readLocalPolicy();
    const fingerprint = scoreMeta()?.fingerprint || "";
    if (!local) return null;
    if (local.score_id && local.score_id !== scoreId) return null;
    if (fingerprint && local.fingerprint && local.fingerprint !== fingerprint) return null;
    return local;
  }

  function readLocalPolicy() {
    try { return JSON.parse(localStorage.getItem(POLICY_LOCAL_KEY) || "null"); }
    catch (_) { return null; }
  }

  async function loadPolicy() {
    savedPolicy = await fetchPolicy(currentScoreId);
  }

  function applyPolicyToForm(policy) {
    if (!policy) {
      renderPolicyStatus();
      return;
    }
    if (policy.score_id && scoreMap[policy.score_id]) {
      currentScoreId = policy.score_id;
      $("lab-score").value = currentScoreId;
      populateSegmentSelect();
      populatePitchSelect();
    }
    if (policy.priority) $("lab-priority").value = policy.priority;
    if (policy.planner) $("lab-planner").value = policy.planner;
    if (policy.lock) {
      $("lab-lock-segment").value = String(policy.lock.segment);
      $("lab-lock-voice").value = policy.lock.voice;
      populatePitchSelect(policy.lock.midi);
    }
    chosenBlind = policy.blind_choice || null;
    renderPolicyStatus();
  }

  function policyRecord(extra = {}) {
    return {
      score_id: currentScoreId,
      fingerprint: compareData?.fingerprint || scoreMeta()?.fingerprint,
      planner: $("lab-planner").value,
      priority: $("lab-priority").value,
      lock: formLock(),
      ...extra,
    };
  }

  async function savePolicy(extra = {}) {
    const record = policyRecord(extra);
    try {
      const data = await api("/api/experiment/policy", { policy: record });
      savedPolicy = data.policy;
      localStorage.setItem(POLICY_LOCAL_KEY, JSON.stringify(savedPolicy));
      renderPolicyStatus("默认政策已保存；下次打开相同谱例会应用这些选择。");
      return savedPolicy;
    } catch (err) {
      renderPolicyStatus(`保存失败：${err.message}`, true);
      return null;
    }
  }

  function renderPolicyStatus(message = "", isError = false) {
    const box = $("lab-policy-status");
    box.className = isError ? "banner error" : "style-note";
    const p = savedPolicy;
    const parts = [];
    if (message) parts.push(message);
    if (p) {
      parts.push(`当前默认：${p.planner === "lookahead" ? "有限前瞻" : "全句搜索"} / ${p.priority === "local" ? "局部规则优先" : "全句约束优先"}`);
      if (p.lock) parts.push(`锁 ${VOICE_LABEL[p.lock.voice]} 第 ${p.lock.segment + 1} 段 ${midiName(p.lock.midi)}`);
      if (p.blind_choice) parts.push(`盲比选择：样本 ${p.blind_choice}`);
    } else {
      parts.push("尚未保存服务器默认政策；首次实测后可一键保存。");
    }
    box.textContent = parts.join(" · ");
  }

  // -------------------------------------------------------------------------
  // Compare
  // -------------------------------------------------------------------------

  function requestBody(overrides = {}) {
    return {
      score_id: currentScoreId,
      priority: $("lab-priority").value,
      planner: $("lab-planner").value,
      lock: formLock(),
      include_trace: true,
      benchmark: true,
      inspect_segment: Number($("lab-lock-segment").value || 1),
      ...overrides,
    };
  }

  async function runCompare() {
    const status = $("lab-blind-status");
    if (status) status.textContent = "正在求解全句路径与有限前瞻路径…";
    stopAudio();
    Object.keys(synthCache).forEach((k) => delete synthCache[k]);
    try {
      compareData = await api("/api/experiment/compare", requestBody({
        benchmark: !savedPolicy,
      }));
      activeInspector = compareData.inspector;
      activeTraceStep = compareData.lock_trace?.return_to_step ?? null;
      activePathDetail = compareData.variants[compareData.requested_planner]?.selected_path_id || null;
      revealed = false;
      chosenBlind = null;
      blindMap = Math.random() < 0.5
        ? { A: "phrase", B: "lookahead" }
        : { A: "lookahead", B: "phrase" };
      renderAll();
      const rec = compareData.policy_decision;
      if (rec && !savedPolicy) {
        await savePolicy({ planner: rec.planner, priority: rec.priority });
      }
      if (status) status.textContent = "已就绪：请盲听 A/B，再揭盲。";
    } catch (err) {
      $("lab-cards").innerHTML = "";
      $("lab-paths").innerHTML = "";
      $("lab-trace").innerHTML = "";
      $("lab-inspector").innerHTML = "";
      const box = $("lab-blind-status");
      box.className = "banner error";
      box.textContent = `实验失败：${err.message}`;
    }
  }

  function renderAll() {
    renderBlindStatus();
    renderVoiceButtons();
    renderCards();
    renderPaths();
    renderTrace();
    renderInspector();
    renderBenchmarkDecision();
  }

  function plannerName(planner) {
    return planner === "phrase" ? "全句搜索" : "有限前瞻";
  }

  function switchPlannerFocus() {
    if (!compareData) return;
    const planner = $("lab-planner").value;
    compareData.requested_planner = planner;
    const sounding = compareData.inspector?.actual_soundings?.[planner];
    if (sounding && activeInspector?.segment === compareData.inspector.segment) {
      activeInspector = { ...activeInspector, actual_sounding: sounding };
    }
    activePathDetail = compareData.variants[planner]?.selected_path_id || activePathDetail;
    renderCards();
    renderPaths();
    renderInspector();
  }

  function sampleOf(planner) {
    return Object.entries(blindMap).find(([, p]) => p === planner)?.[0] || "?";
  }

  function renderBlindStatus() {
    const box = $("lab-blind-status");
    if (!compareData) return;
    if (revealed) {
      box.textContent = `揭盲：A = ${plannerName(blindMap.A)}；B = ${plannerName(blindMap.B)}。`
        + (chosenBlind ? ` 你选择了 ${chosenBlind}，已作为盲比偏好保存。` : " 可标记你的盲听选择。");
    } else {
      box.textContent = chosenBlind
        ? `已记录盲选 ${chosenBlind}，揭盲后保存对应政策。`
        : "先盲听 A/B，再揭盲；选择会随默认政策保存。";
    }
  }

  function renderVoiceButtons() {
    const wrap = $("lab-sample-voice-buttons");
    wrap.innerHTML = "";
    ["A", "B"].forEach((letter) => {
      VOICES.forEach((voice) => {
        wrap.appendChild(h("button", {
          "data-sample": letter,
          "data-voice": voice,
          class: "mini-voice",
          text: `${letter} · ${VOICE_LABEL[voice]}`,
        }));
      });
    });
  }

  function variantForLetter(letter) {
    return compareData.variants[blindMap[letter]];
  }

  function renderCards() {
    const wrap = $("lab-cards");
    wrap.innerHTML = "";
    ["A", "B"].forEach((letter) => {
      const variant = variantForLetter(letter);
      const metrics = variant.metrics || {};
      const card = h("div", { class: "lab-card" });
      card.appendChild(h("h3", {
        text: revealed
          ? `样本 ${letter} · ${plannerName(variant.planner)}`
          : `样本 ${letter}`,
      }));
      card.appendChild(h("div", {
        class: `mini-banner ${variant.result.status}`,
        text: `${variant.result.status} · 代价 ${variant.result.total_cost ?? "∞"}`,
      }));
      const dl = h("div", { class: "metric-grid" });
      appendMetric(dl, "首次平行运动", formatFirstParallel(metrics.first_parallel));
      appendMetric(dl, "两方案改动音数", String(metrics.pairwise_difference?.changed_notes ?? "—"));
      appendMetric(dl, "禁行/超限", `${metrics.forbidden_count ?? "—"}（平行 ${metrics.parallel_perfect_count ?? 0} / 交叉 ${metrics.crossing_count ?? 0}）`);
      appendMetric(dl, "音域违例", String(metrics.range_violation_count ?? "—"));
      const lockImpact = metrics.lock_impact || {};
      appendMetric(dl, "锁音总改写", String(lockImpact.changed_notes ?? "—"));
      appendMetric(dl, "较远声部改写", String(lockImpact.far_voice_rewrites ?? "—"));
      card.appendChild(dl);
      card.appendChild(renderRangeTable(metrics.actual_ranges));
      wrap.appendChild(card);
    });
  }

  function appendMetric(parent, label, value) {
    parent.appendChild(h("div", { class: "metric" },
      h("span", { class: "metric-label", text: label }),
      h("strong", { text: value })));
  }

  function formatFirstParallel(fp) {
    if (!fp) return "无平行五/八度";
    const voices = fp.voices.map((v) => VOICE_LABEL[v] || v).join("–");
    return `第 ${fp.segment + 1} 段 ${voices}；${fp.before_semitones}→${fp.after_semitones} 半音`;
  }

  function formatFarRewrites(metrics) {
    const impact = metrics?.lock_impact;
    if (!impact) return "—";
    return String(impact.far_voice_rewrites ?? metrics.far_voice_rewrites ?? 0);
  }

  function renderRangeTable(ranges) {
    if (!ranges) return h("div");
    const table = h("table", { class: "range-table" });
    table.appendChild(h("thead", {}, h("tr", {},
      h("th", { text: "声区" }), h("th", { text: "最低" }),
      h("th", { text: "最高" }), h("th", { text: "实际区" }), h("th", { text: "状态" }))));
    const tbody = h("tbody");
    VOICES.forEach((voice) => {
      const r = ranges[voice];
      if (!r) return;
      tbody.appendChild(h("tr", {},
        h("td", { text: VOICE_LABEL[voice] }),
        h("td", { text: `${r.min_name} (${r.min_midi})` }),
        h("td", { text: `${r.max_name} (${r.max_midi})` }),
        h("td", { text: r.register }),
        h("td", { text: r.in_range ? "在声区内" : `越界 ${r.out_of_range.length} 音` })));
    });
    table.appendChild(tbody);
    return table;
  }

  // -------------------------------------------------------------------------
  // Paths
  // -------------------------------------------------------------------------

  function actualRows(variant) {
    return (variant.result.segments || []).map((seg) => ({
      segment: seg.index,
      chord_symbol: seg.chord?.symbol || "",
      names: seg.note_names || Object.fromEntries(
        VOICES.map((v) => [v, midiName(seg.voicing[v], compareData.key_root_pc || 0)])
      ),
      voicing: seg.voicing,
    }));
  }

  function renderPaths() {
    const wrap = $("lab-paths");
    wrap.innerHTML = "";
    ["A", "B"].forEach((letter) => {
      const variant = variantForLetter(letter);
      const block = h("div", { class: "selected-path-block" });
      block.appendChild(h("h4", {
        text: revealed
          ? `${letter} · ${plannerName(variant.planner)} 入选路径`
          : `${letter} 入选路径`,
      }));
      block.appendChild(renderPathMatrix(actualRows(variant)));
      wrap.appendChild(block);
    });

    const labPaths = compareData.path_lab?.paths || [];
    if (labPaths.length) {
      wrap.appendChild(h("h4", { text: "决策图中的入选/被淘汰完整路径" }));
      const table = h("table", { class: "path-table" });
      table.appendChild(h("thead", {}, h("tr", {},
        h("th", { text: "路径" }), h("th", { text: "代价" }),
        h("th", { text: "禁行" }), h("th", { text: "音域" }),
        h("th", { text: "远声部改写" }),
        h("th", { text: "首次平行" }), h("th", { text: "结论/原因" }),
        h("th", { text: "操作" }))));
      const tbody = h("tbody");
      labPaths.slice(0, 8).forEach((p) => {
        const selectedBy = Object.entries(compareData.variants)
          .filter(([, v]) => v.selected_path_id === p.id)
          .map(([planner]) => revealed ? sampleOf(planner) + "/" + plannerName(planner) : "样本" + sampleOf(planner))
          .join(", ");
        tbody.appendChild(h("tr", {},
          h("td", { text: `#${p.rank} ${p.id}` }),
          h("td", { text: String(p.total_cost) }),
          h("td", { text: `${p.metrics.forbidden_count} (交${p.metrics.crossing_count})` }),
          h("td", { text: String(p.metrics.range_violation_count) }),
          h("td", { text: formatFarRewrites(p.metrics) }),
          h("td", { text: formatFirstParallel(p.metrics.first_parallel) }),
          h("td", { text: selectedBy ? `入选：${selectedBy}` : (p.rejection_reason || "淘汰") }),
          h("td", { class: "row-actions" },
            h("button", { "data-path-detail": p.id, text: "查看" }),
            h("button", { "data-path-audio": p.id, text: "试听" }))));
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
    }

    const rejectedPaths = compareData.path_lab?.rejected_paths || [];
    if (rejectedPaths.length) {
      wrap.appendChild(h("h4", { text: "含禁行、越界或内声交叉而被淘汰的路径证据" }));
      const table = h("table", { class: "path-table rejected-table" });
      table.appendChild(h("thead", {}, h("tr", {},
        h("th", { text: "类型" }), h("th", { text: "路径" }),
        h("th", { text: "禁行/交叉" }), h("th", { text: "音域" }),
        h("th", { text: "远声部改写" }),
        h("th", { text: "首次平行" }), h("th", { text: "淘汰原因" }),
        h("th", { text: "操作" }))));
      const tbody = h("tbody");
      rejectedPaths.forEach((p) => {
        tbody.appendChild(h("tr", {},
          h("td", { text: p.evidence_category || "violation" }),
          h("td", { text: p.id }),
          h("td", { text: `${p.metrics.forbidden_count} / 交叉 ${p.metrics.crossing_count} / 平行 ${p.metrics.parallel_perfect_count}` }),
          h("td", { text: String(p.metrics.range_violation_count) }),
          h("td", { text: formatFarRewrites(p.metrics) }),
          h("td", { text: formatFirstParallel(p.metrics.first_parallel) }),
          h("td", { text: p.rejection_reason || "淘汰" }),
          h("td", { class: "row-actions" },
            h("button", { "data-path-detail": p.id, text: "查看" }),
            h("button", { "data-path-audio": p.id, text: "试听" }))));
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
    }

    if (labPaths.length || rejectedPaths.length) {
      wrap.appendChild(h("div", { id: "lab-path-detail", class: "path-detail" }));
      renderPathDetail();
    }
  }

  function renderPathMatrix(rows) {
    if (!rows?.length) return h("p", { class: "muted", text: "该方案没有可演奏路径。" });
    const table = h("table", { class: "matrix-table" });
    const thead = h("thead");
    const headRow = h("tr", {}, h("th", { text: "声部" }));
    rows.forEach((row) => headRow.appendChild(h("th", {
      text: `${row.segment + 1} · ${row.chord_symbol}`,
    })));
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = h("tbody");
    VOICES.forEach((voice) => {
      const tr = h("tr", {}, h("td", {
        class: `voice-name-${voice}`,
        text: VOICE_LABEL[voice],
      }));
      rows.forEach((row) => {
        const name = row.names?.[voice]
          || (row.voicing ? midiName(row.voicing[voice]) : "—");
        tr.appendChild(h("td", { text: name }));
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    return h("div", { class: "matrix-scroll" }, table);
  }

  function findPath(pathId) {
    const lab = compareData.path_lab || {};
    return [...(lab.paths || []), ...(lab.rejected_paths || [])]
      .find((p) => p.id === pathId);
  }

  function renderPathDetail() {
    const host = $("lab-path-detail");
    if (!host) return;
    host.innerHTML = "";
    const path = findPath(activePathDetail);
    if (!path) {
      host.appendChild(h("p", { class: "muted", text: "点击“查看”显示被淘汰路径的实际声部归属。" }));
      return;
    }
    host.appendChild(h("h4", { text: `${path.id} · ${path.rejection_reason || "入选路径"}` }));
    host.appendChild(renderPathMatrix(path.rows));
  }

  function labPathEvents(path) {
    const doc = scoreDocument();
    const events = [];
    let cursor = 0;
    path.rows.forEach((row) => {
      const raw = doc.segments[row.segment] || {};
      const duration = Number(raw.duration || 1);
      const breath = Number(raw.breath_after || 0);
      const sounding = Math.max(0.125, duration - breath);
      VOICES.forEach((voice) => {
        const midi = row.voicing[voice];
        events.push({
          event_id: `${path.id}:${row.segment}:${voice}`,
          segment: row.segment, voice, midi,
          name: row.names?.[voice] || midiName(midi),
          start_beat: cursor,
          duration_beats: duration,
          sounding_beats: sounding,
          breath_after_beats: breath,
          velocity: 72,
        });
      });
      cursor += duration;
    });
    return {
      format: "chorale_arranger_events",
      version: 1,
      total_beats: cursor,
      voices: VOICES,
      events,
    };
  }

  // -------------------------------------------------------------------------
  // Audio
  // -------------------------------------------------------------------------

  function decodeB64(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return bytes.buffer;
  }

  function stopAudio() {
    if (activeSource) {
      try { activeSource.stop(); } catch (_) { /* ended */ }
      activeSource = null;
    }
  }

  async function playWavBase64(b64) {
    stopAudio();
    audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
    const buffer = await audioContext.decodeAudioData(decodeB64(b64));
    activeSource = audioContext.createBufferSource();
    activeSource.buffer = buffer;
    activeSource.connect(audioContext.destination);
    activeSource.start();
    activeSource.onended = () => { activeSource = null; };
  }

  async function ensureSampleSynth(letter) {
    if (synthCache[letter]) return synthCache[letter];
    const variant = variantForLetter(letter);
    if (!variant.events) throw new Error("该样本没有可试听事件");
    synthCache[letter] = await api("/api/synth", {
      events: variant.events, bpm: 88, sample_rate: 22050,
    });
    return synthCache[letter];
  }

  async function playSample(letter, voice = "ensemble") {
    try {
      $("lab-blind-status").textContent = `正在合成样本 ${letter}…`;
      const audio = await ensureSampleSynth(letter);
      const b64 = voice === "ensemble" ? audio.ensemble_wav : audio.per_voice_wav[voice];
      await playWavBase64(b64);
      $("lab-blind-status").textContent = `正在试听样本 ${letter}${voice === "ensemble" ? " 合奏" : " · " + VOICE_LABEL[voice]}。`;
    } catch (err) {
      alert(`试听失败：${err.message}`);
    }
  }

  async function auditionLabPath(pathId) {
    const path = findPath(pathId);
    if (!path) return;
    try {
      let audio = pathSynthCache[pathId];
      if (!audio) {
        audio = await api("/api/synth", {
          events: labPathEvents(path), bpm: 88, sample_rate: 22050,
        });
        pathSynthCache[pathId] = audio;
      }
      await playWavBase64(audio.ensemble_wav);
    } catch (err) {
      alert(`路径试听失败：${err.message}`);
    }
  }

  // -------------------------------------------------------------------------
  // Blind choice / benchmark / import
  // -------------------------------------------------------------------------

  async function chooseBlind(letter) {
    chosenBlind = letter;
    revealed = true;
    $("lab-planner").value = blindMap[letter];
    await savePolicy({ blind_choice: letter, planner: blindMap[letter] });
    renderBlindStatus();
    renderCards();
    renderPaths();
  }

  function restoreBlindChoice(policy) {
    const letter = policy?.blind_choice;
    const planner = policy?.planner;
    if (!compareData || !letter || !planner || !compareData.variants[planner]) return;
    const other = planner === "phrase" ? "lookahead" : "phrase";
    blindMap = letter === "A"
      ? { A: planner, B: other }
      : { A: other, B: planner };
    chosenBlind = letter;
    revealed = true;
    $("lab-planner").value = planner;
    renderBlindStatus();
    renderCards();
    renderPaths();
  }

  async function runBenchmark() {
    const box = $("lab-benchmark");
    box.innerHTML = `<p class="muted">正在重复实测求解耗时与违例指标…</p>`;
    try {
      benchmarkData = await api("/api/experiment/benchmark", {
        score_id: currentScoreId, repeats: 3,
      });
      const rec = benchmarkData.recommendation;
      $("lab-planner").value = rec.planner;
      $("lab-priority").value = rec.priority;
      await savePolicy({ planner: rec.planner, priority: rec.priority });
      renderBenchmarkTable();
      await runCompare();
    } catch (err) {
      box.innerHTML = `<div class="banner error">实测失败：${err.message}</div>`;
    }
  }

  function renderBenchmarkDecision() {
    const box = $("lab-benchmark");
    if (benchmarkData) return renderBenchmarkTable();
    const rec = compareData?.policy_decision;
    if (!box) return;
    if (!rec) {
      box.innerHTML = "";
      return;
    }
    box.innerHTML = "";
    box.appendChild(h("div", { class: "style-note" },
      `实测默认政策建议：${plannerName(rec.planner)} / ${rec.priority === "local" ? "局部规则优先" : "全句约束优先"}。${rec.reason}`));
  }

  function renderBenchmarkTable() {
    const box = $("lab-benchmark");
    box.innerHTML = "";
    const rec = benchmarkData.recommendation;
    box.appendChild(h("div", { class: "style-note" },
      `实测选定：${plannerName(rec.planner)} / ${rec.priority === "local" ? "局部规则优先" : "全句约束优先"}，并已保存为默认政策。`));
    const table = h("table");
    table.appendChild(h("thead", {}, h("tr", {},
      h("th", { text: "谱例" }), h("th", { text: "编配器" }), h("th", { text: "优先级" }),
      h("th", { text: "状态" }), h("th", { text: "代价" }), h("th", { text: "禁行" }),
      h("th", { text: "音域" }), h("th", { text: "中位 ms" }), h("th", { text: "实测分" }))));
    const tbody = h("tbody");
    benchmarkData.measurements
      .filter((m) => m.score_id === currentScoreId)
      .forEach((m) => tbody.appendChild(h("tr", {},
        h("td", { text: m.score_id }),
        h("td", { text: plannerName(m.planner) }),
        h("td", { text: m.priority }),
        h("td", { text: m.status }),
        h("td", { text: String(m.total_cost) }),
        h("td", { text: String(m.forbidden_count) }),
        h("td", { text: String(m.range_violation_count) }),
        h("td", { text: String(m.elapsed_ms) }),
        h("td", { text: String(m.measured_score) }))));
    table.appendChild(tbody);
    box.appendChild(table);
  }

  function importToMainEditor() {
    if (!window.choraleUI?.loadDocument) {
      alert("现有编配器接口尚未就绪");
      return;
    }
    const doc = JSON.parse(JSON.stringify(scoreDocument()));
    const lock = formLock();
    if (lock) {
      doc.segments[lock.segment].fixed = doc.segments[lock.segment].fixed || {};
      doc.segments[lock.segment].fixed[lock.voice] = lock.midi;
    }
    doc.planner = $("lab-planner").value;
    doc.priority = $("lab-priority").value;
    window.choraleUI.loadDocument(doc, { solve: true });
    $("panel-input").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // -------------------------------------------------------------------------
  // Lock trace and inspector
  // -------------------------------------------------------------------------

  function undoNextLock() {
    const trace = compareData?.lock_trace;
    if (!trace?.steps?.length) return;
    const maxStep = trace.steps.length - 1;
    activeTraceStep = activeTraceStep == null ? 0 : Math.min(maxStep, activeTraceStep + 1);
    const step = trace.steps[activeTraceStep];
    activeInspector = step.inspector;
    renderTrace();
    renderInspector();
  }

  function showBeforeFirstViolation() {
    const trace = compareData?.lock_trace;
    if (!trace) return;
    activeTraceStep = trace.return_to_step;
    const step = trace.steps.find((s) => s.step === activeTraceStep);
    activeInspector = step?.inspector || compareData.inspector;
    renderTrace();
    renderInspector();
  }

  function renderTrace() {
    const wrap = $("lab-trace");
    wrap.innerHTML = "";
    const trace = compareData?.lock_trace;
    if (!trace?.steps?.length) {
      wrap.appendChild(h("p", {
        class: "muted",
        text: "当前谱例没有预置多锁序列；使用上方锁音后，指标会比较同政策的未加锁路径。",
      }));
      return;
    }
    const bar = h("div", { class: "trace-steps" });
    trace.steps.forEach((step) => {
      const locks = step.active_locks?.length || 0;
      const label = step.step === 0
        ? `全锁 (${locks})`
        : step.removed ? `撤销 ${VOICE_LABEL[step.removed.voice]}@${step.removed.segment + 1}`
        : `步骤 ${step.step}`;
      bar.appendChild(h("button", {
        class: `trace-step ${step.clean ? "clean" : "dirty"} ${activeTraceStep === step.step ? "active" : ""}`,
        onClick: () => {
          activeTraceStep = step.step;
          activeInspector = step.inspector;
          renderTrace();
          renderInspector();
        },
        text: label,
      }));
    });
    wrap.appendChild(bar);
    const current = trace.steps.find((s) => s.step === activeTraceStep) || trace.steps[0];
    wrap.appendChild(h("p", {
      class: current.clean ? "trace-note ok" : "trace-note bad",
      text: `步骤 ${current.step}：phrase=${current.phrase_status}, lookahead=${current.lookahead_status}, `
        + `记录违例 P${current.phrase_violations}/L${current.lookahead_violations}，指标违例 ${current.metric_violations}。`
        + (trace.return_to_step != null ? ` 首次违例前检查点＝步骤 ${trace.return_to_step}。` : ""),
    }));
  }

  function renderInspector() {
    const wrap = $("lab-inspector");
    wrap.innerHTML = "";
    const data = activeInspector || compareData?.inspector;
    if (!data) return;
    wrap.appendChild(h("h4", { text: `第 ${data.segment + 1} 段候选与实际发声检查` }));
    const counts = h("div", { class: "inspector-counts" },
      countBox("实际候选状态", data.candidate_count),
      countBox("合法有序候选", data.legal_count),
      countBox("原始组合数", data.raw_combination_count));
    wrap.appendChild(counts);

    const sounding = data.actual_sounding;
    if (sounding) {
      wrap.appendChild(h("div", { class: "inspector-pitch-title",
        text: `和弦 ${sounding.chord_symbol}：实际音高 / 声部归属` }));
      const table = h("table");
      table.appendChild(h("thead", {}, h("tr", {},
        h("th", { text: "声部归属" }), h("th", { text: "音名" }), h("th", { text: "MIDI" }))));
      const tbody = h("tbody");
      VOICES.forEach((voice) => {
        tbody.appendChild(h("tr", {},
          h("td", { class: `voice-name-${voice}`, text: sounding.ownership?.[voice] || VOICE_LABEL[voice] }),
          h("td", { text: sounding.names?.[voice] || "—" }),
          h("td", { text: String(sounding.pitches?.[voice] ?? "—") })));
      });
      table.appendChild(tbody);
      wrap.appendChild(table);
    } else {
      wrap.appendChild(h("p", { class: "banner infeasible", text: "该检查点没有实际发声路径。" }));
    }

    const crosses = data.crossing_candidates || [];
    if (crosses.length) {
      wrap.appendChild(h("h4", { text: "听起来协和但内声交叉的候选" }));
      crosses.slice(0, 3).forEach((c) => {
        const names = VOICES.map((v) => `${VOICE_LABEL[v]}=${c.names[v]}`).join("，");
        wrap.appendChild(h("div", { class: "cross-candidate" },
          h("strong", { text: `${c.chord_symbol} · 交叉 ${c.crossing_pairs.map((p) => p.join("/")).join("; ")}` }),
          h("div", { text: names }),
          h("div", { class: "muted", text: `全部为和弦音：${c.audibly_consonant ? "是，音响协和" : "否"}；这些路径会被 no_crossing 硬约束淘汰。` })));
      });
    }
  }

  function countBox(label, value) {
    return h("div", { class: "count-box" },
      h("strong", { text: String(value ?? 0) }),
      h("span", { text: label }));
  }

  init();
})();
