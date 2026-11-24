"use strict";

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

const VOICES = ["soprano", "alto", "tenor", "bass"];
const VOICE_LABEL = { soprano: "女高 S", alto: "女低 A", tenor: "男高 T", bass: "男低 B" };
const VOICE_COLOR = {
  soprano: "#be123c", alto: "#c2410c", tenor: "#1d4ed8", bass: "#065f46",
};
const NOTE_LETTERS = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"];
const QUALITIES = [
  "maj", "min", "dim", "aug", "sus2", "sus4",
  "maj7", "min7", "7", "dim7", "m7b5", "maj9", "min9", "9", "6", "min6", "add9", "power",
];
const FUNCTIONS = ["I", "ii", "iii", "IV", "V", "vi", "vii°", "I7", "ii7", "V7", "vi7", "vii°7"];
const LEVELS = ["hard", "preference", "off"];
const KEY_NAMES = ["C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"];

let styles = [];
let styleMap = {};
let voiceRanges = {};
let state = null;       // editable input document
let lastResult = null;  // last solve response
let lastEvents = null;
let audioCache = null;
let audioContext = null;
let activeSource = null;

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

async function api(path, body) {
  const resp = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json();
  if (!resp.ok) {
    throw new Error(data.error ? `${data.error}${data.traceback ? "\n" + data.traceback : ""}` : `HTTP ${resp.status}`);
  }
  return data;
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const child of children) {
    if (child == null) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
  return node;
}

function download(filename, content, mime) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

function midiToName(midi, keyRootPc = 0) {
  const table = keyRootPc < 0 ? NOTE_LETTERS : NOTE_LETTERS;
  const pc = ((midi % 12) + 12) % 12;
  const octave = Math.floor(midi / 12) - 1;
  return `${table[pc]}${octave}`;
}

// ---------------------------------------------------------------------------
// Demo documents
// ---------------------------------------------------------------------------

function demoDocument() {
  return {
    key_root_pc: 0,
    style: "common_practice_strict",
    beam_width: 40,
    seed: 7,
    candidate_cap: 0,
    overrides: { levels: {}, weights: {}, params: {} },
    segments: [
      { duration: 1, fixed: { soprano: 72 }, chord_candidates: [{ function: "I" }] },
      { duration: 1, fixed: { soprano: 69 }, chord_candidates: [{ function: "IV" }, { function: "ii" }] },
      { duration: 1, fixed: { soprano: 67 }, chord_candidates: [{ function: "V7" }, { function: "vii°7" }] },
      { duration: 1, fixed: { soprano: 72, bass: 48 }, chord_candidates: [{ function: "I" }] },
    ],
  };
}

function conflictDocument() {
  return {
    key_root_pc: 0,
    style: "common_practice_strict",
    beam_width: 40,
    seed: 3,
    candidate_cap: 0,
    overrides: { levels: {}, weights: {}, params: {} },
    segments: [
      { duration: 1, fixed: { soprano: 84, bass: 36 }, chord_candidates: [{ function: "I" }] },
      { duration: 1, fixed: { soprano: 83, bass: 43 }, chord_candidates: [{ function: "V7" }] },
      { duration: 1, fixed: { soprano: 84, bass: 36 }, chord_candidates: [{ function: "I" }] },
    ],
  };
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

async function init() {
  const meta = await fetch("/api/styles").then((r) => r.json());
  styles = meta.styles;
  styleMap = Object.fromEntries(styles.map((s) => [s.name, s]));
  voiceRanges = meta.voice_ranges;

  const keySel = document.getElementById("key-select");
  NOTE_LETTERS.forEach((name, pc) => keySel.appendChild(el("option", { value: pc, text: `${name} 大调` })));
  const styleSel = document.getElementById("style-select");
  styles.forEach((s) => styleSel.appendChild(el("option", { value: s.name, text: s.name })));

  bindGlobalControls();
  state = demoDocument();
  syncGlobalControls();
  renderRules();
  renderSegments();

  document.getElementById("btn-demo").addEventListener("click", () => {
    state = demoDocument();
    syncGlobalControls();
    renderRules();
    renderSegments();
    clearResult();
  });
  document.getElementById("btn-conflict").addEventListener("click", () => {
    state = conflictDocument();
    syncGlobalControls();
    renderRules();
    renderSegments();
    clearResult();
  });
  document.getElementById("btn-solve").addEventListener("click", runSolve);
  document.getElementById("btn-add-segment").addEventListener("click", () => {
    state.segments.push({ duration: 1, fixed: {}, chord_candidates: [{ function: "I" }] });
    renderSegments();
  });
  document.getElementById("btn-remove-segment").addEventListener("click", () => {
    if (state.segments.length > 1) {
      state.segments.pop();
      renderSegments();
    }
  });

  document.getElementById("btn-play-ensemble").addEventListener("click", () => playAudio("ensemble"));
  document.getElementById("btn-stop").addEventListener("click", stopAudio);
  document.querySelectorAll(".vp").forEach((btn) =>
    btn.addEventListener("click", () => playAudio(btn.dataset.voice))
  );
  document.getElementById("btn-export-events").addEventListener("click", exportEvents);
  document.getElementById("btn-export-midi").addEventListener("click", () => exportBinary("midi"));
  document.getElementById("btn-export-wav").addEventListener("click", exportWav);
  document.getElementById("btn-reimport").addEventListener("click", () => {
    if (!lastEvents) return alert("请先求解生成音符事件");
    reimport(lastEvents);
  });
  document.getElementById("reimport-file").addEventListener("change", reimportFile);
}

function bindGlobalControls() {
  const bind = (id, key, parse = (v) => v) => {
    document.getElementById(id).addEventListener("change", (e) => {
      state[key] = parse(e.target.value);
      if (key === "style") {
        state.overrides = { levels: {}, weights: {}, params: {} };
        renderRules();
        updateStyleDescription();
      }
    });
  };
  bind("key-select", "key_root_pc", Number);
  bind("style-select", "style");
  bind("beam-width", "beam_width", Number);
  bind("seed", "seed", Number);
  bind("candidate-cap", "candidate_cap", Number);
}

function syncGlobalControls() {
  document.getElementById("key-select").value = state.key_root_pc;
  document.getElementById("style-select").value = state.style;
  document.getElementById("beam-width").value = state.beam_width;
  document.getElementById("seed").value = state.seed;
  document.getElementById("candidate-cap").value = state.candidate_cap;
  updateStyleDescription();
}

function updateStyleDescription() {
  const profile = styleMap[state.style];
  document.getElementById("style-description").textContent = profile.description;
}

// ---------------------------------------------------------------------------
// Rule override table
// ---------------------------------------------------------------------------

function renderRules() {
  const profile = styleMap[state.style];
  const wrap = document.getElementById("rule-table");
  wrap.innerHTML = "";
  const table = el("table");
  table.appendChild(el("thead", {}, el("tr", {},
    el("th", { text: "规则" }), el("th", { text: "类别" }),
    el("th", { text: "级别" }), el("th", { text: "权重" }), el("th", { text: "说明" }))));
  const tbody = el("tbody");
  for (const rule of profile.rules) {
    const isPlay = rule.category === "playability";
    const levelSelect = el("select", {
      disabled: isPlay ? "disabled" : null,
      onChange: (e) => {
        state.overrides.levels[rule.key] = e.target.value;
      },
    });
    LEVELS.forEach((lv) => {
      const opt = el("option", { value: lv, text: lv });
      if (lv === rule.level) opt.selected = true;
      levelSelect.appendChild(opt);
    });
    const weightInput = el("input", {
      type: "number", step: "0.5", min: "0", value: rule.weight,
      disabled: rule.level === "hard" ? "disabled" : null,
      onChange: (e) => {
        state.overrides.weights[rule.key] = Number(e.target.value);
      },
    });
    tbody.appendChild(el("tr", {},
      el("td", { text: rule.key }),
      el("td", { class: `rule-cat-${rule.category}`, text: rule.category }),
      el("td", {}, levelSelect),
      el("td", {}, weightInput),
      el("td", {},
        document.createTextNode(rule.description),
        rule.convention_note ? el("div", { class: "convention", text: "风格惯例：" + rule.convention_note }) : null,
      ),
    ));
  }
  table.appendChild(tbody);
  wrap.appendChild(table);
  const params = el("div", { class: "seg-grid", style: "margin-top:10px" });
  for (const [key, value] of Object.entries(profile.params)) {
    params.appendChild(el("label", {},
      document.createTextNode(`参数 ${key}`),
      el("input", {
        type: "number", step: "0.5", value,
        onChange: (e) => { state.overrides.params[key] = Number(e.target.value); },
      }),
    ));
  }
  wrap.appendChild(params);
}

// ---------------------------------------------------------------------------
// Segment editor
// ---------------------------------------------------------------------------

function renderSegments() {
  const wrap = document.getElementById("segment-editor");
  wrap.innerHTML = "";
  state.segments.forEach((seg, idx) => wrap.appendChild(renderSegmentCard(seg, idx)));
}

function renderSegmentCard(seg, idx) {
  const card = el("div", { class: "seg-card" });
  card.appendChild(el("h4", { text: `段落 ${idx + 1}` }));
  const grid = el("div", { class: "seg-grid" });

  grid.appendChild(el("label", {}, "时长（拍）",
    el("input", {
      type: "number", step: "0.5", min: "0.25", value: seg.duration,
      onChange: (e) => { seg.duration = Number(e.target.value); },
    })));

  const chordBox = el("div");
  chordBox.appendChild(el("div", { class: "muted", text: "和弦功能候选（求解器可在候选间选择）" }));
  seg.chord_candidates.forEach((cand, ci) => {
    const tag = el("span", { class: "cand-tag" });
    const fnSel = el("select", {
      onChange: (e) => {
        if (e.target.value === "__explicit__") {
          delete cand.function;
          cand.root_pc = state.key_root_pc;
          cand.quality = "maj";
        } else {
          cand.function = e.target.value;
        }
        renderSegments();
      },
    });
    const placeholder = el("option", { value: "", text: "(功能)" });
    fnSel.appendChild(placeholder);
    FUNCTIONS.forEach((fn) => {
      const opt = el("option", { value: fn, text: fn });
      if (cand.function === fn) opt.selected = true;
      fnSel.appendChild(opt);
    });
    fnSel.appendChild(el("option", { value: "__explicit__", text: "显式和弦…" }));
    if (!cand.function) fnSel.value = "__explicit__";
    tag.appendChild(fnSel);

    if (!cand.function) {
      const rootSel = el("select", {
        onChange: (e) => { cand.root_pc = Number(e.target.value); },
      });
      KEY_NAMES.forEach((nm, pc) => {
        const opt = el("option", { value: pc, text: nm });
        if ((cand.root_pc ?? 0) === pc) opt.selected = true;
        rootSel.appendChild(opt);
      });
      const qSel = el("select", {
        onChange: (e) => { cand.quality = e.target.value; },
      });
      QUALITIES.forEach((q) => {
        const opt = el("option", { value: q, text: q });
        if ((cand.quality ?? "maj") === q) opt.selected = true;
        qSel.appendChild(opt);
      });
      tag.appendChild(rootSel);
      tag.appendChild(qSel);
    }
    if (seg.chord_candidates.length > 1) {
      tag.appendChild(el("button", {
        text: "×", title: "移除候选",
        onClick: () => { seg.chord_candidates.splice(ci, 1); renderSegments(); },
      }));
    }
    chordBox.appendChild(el("div", { class: "chord-cand" }, tag));
  });
  chordBox.appendChild(el("button", {
    text: "+ 候选和弦",
    onClick: () => { seg.chord_candidates.push({ function: "V" }); renderSegments(); },
  }));
  grid.appendChild(chordBox);

  const fixedBox = el("div");
  fixedBox.appendChild(el("div", { class: "muted", text: "固定声部音高（可留空；音名如 C5、Bb3、F#4）" }));
  const fixedGrid = el("div", { class: "fixed-grid" });
  VOICES.forEach((voice) => {
    const range = voiceRanges[voice];
    const input = el("input", {
      placeholder: `${VOICE_LABEL[voice]} ${midiToName(range[0])}–${midiToName(range[1])}`,
      value: seg.fixed[voice] != null ? midiToName(seg.fixed[voice]) : "",
      onChange: (e) => {
        const token = e.target.value.trim();
        if (!token) {
          delete seg.fixed[voice];
          return;
        }
        const midi = parseNameToMidi(token);
        if (midi == null) {
          e.target.style.borderColor = "#b91c1c";
          e.target.title = "无法解析音名";
          return;
        }
        e.target.style.borderColor = "";
        seg.fixed[voice] = midi;
      },
    });
    fixedGrid.appendChild(input);
  });
  fixedBox.appendChild(fixedGrid);
  grid.appendChild(fixedBox);

  card.appendChild(grid);
  return card;
}

function parseNameToMidi(token) {
  const m = /^([A-Ga-g])([#b]?)(-?\d+)$/.exec(token.trim());
  if (!m) return null;
  const base = { C: 0, D: 2, E: 4, F: 5, G: 7, A: 9, B: 11 }[m[1].toUpperCase()];
  const acc = m[2] === "#" ? 1 : m[2] === "b" ? -1 : 0;
  return (Number(m[3]) + 1) * 12 + base + acc;
}

// ---------------------------------------------------------------------------
// Solve
// ---------------------------------------------------------------------------

async function runSolve() {
  const banner = document.getElementById("status-banner");
  banner.className = "banner";
  banner.textContent = "求解中…";
  try {
    const payload = JSON.parse(JSON.stringify(state));
    lastResult = await api("/api/solve", payload);
    lastEvents = lastResult.events;
    renderResult(lastResult);
    audioCache = null;
    document.getElementById("events-preview").textContent =
      JSON.stringify(lastEvents, null, 2).slice(0, 6000);
  } catch (err) {
    banner.className = "banner error";
    banner.textContent = "请求失败：" + err.message;
  }
}

function clearResult() {
  lastResult = null;
  lastEvents = null;
  audioCache = null;
  const banner = document.getElementById("status-banner");
  banner.className = "banner empty";
  banner.textContent = "尚未求解";
  document.getElementById("recovery-box").classList.add("hidden");
  document.getElementById("cost-table").querySelector("tbody").innerHTML = "";
  document.getElementById("beam-table").querySelector("tbody").innerHTML = "";
  document.getElementById("diagnostics").innerHTML = "";
  document.getElementById("voice-table-wrap").innerHTML = "";
  const ctx = document.getElementById("score-canvas").getContext("2d");
  ctx.clearRect(0, 0, 1080, 420);
  document.getElementById("events-preview").textContent = "";
}

function renderResult(result) {
  const banner = document.getElementById("status-banner");
  banner.className = `banner ${result.status}`;
  const costText = Number.isFinite(result.total_cost) ? `总代价 ${result.total_cost}` : "";
  if (result.status === "optimal") {
    banner.textContent = `求解成功：${result.segments.length} 个和弦段，全部硬条件满足。${costText}`;
  } else if (result.status === "recovered") {
    banner.textContent =
      `严格求解失败，已用恢复策略给出可演奏路径（${result.violations.length} 处显式放宽的硬条件，未隐藏任何越界）。${costText}`;
  } else {
    banner.textContent = "无法满足约束（infeasible）：以下硬性条件互相矛盾，未生成可演奏路径。";
  }

  renderRecovery(result);
  renderCosts(result);
  renderBeamTrace(result);
  renderDiagnostics(result);
  if (result.segments.length) {
    drawScore(result);
    renderVoiceTable(result);
  } else {
    const ctx = document.getElementById("score-canvas").getContext("2d");
    ctx.clearRect(0, 0, 1080, 420);
    document.getElementById("voice-table-wrap").innerHTML = "";
  }
}

function renderRecovery(result) {
  const box = document.getElementById("recovery-box");
  if (!result.recovery) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = "";
  box.appendChild(el("strong", {
    text: `边界冲突发生在段落 ${result.recovery.failed_segment + 1}：局部换位 vs 回看前两和弦`,
  }));
  const table = el("table");
  table.appendChild(el("thead", {}, el("tr", {},
    el("th", { text: "策略" }), el("th", { text: "硬违反数" }),
    el("th", { text: "总代价" }), el("th", { text: "和弦序列" }))));
  const tbody = el("tbody");
  for (const s of result.recovery.strategies) {
    const tr = el("tr", {},
      el("td", { text: s.name + (s.name === result.recovery.chosen ? "（选定）" : "") }),
      el("td", { text: String(s.hard_violations) }),
      el("td", { text: String(s.total_cost) }),
      el("td", { text: s.chord_symbols.join(" → ") }),
    );
    tbody.appendChild(tr);
  }
  table.appendChild(tbody);
  box.appendChild(table);
  box.appendChild(el("p", { class: "muted", text: result.recovery.comparison_note }));
  const ul = el("ul");
  for (const v of result.violations) {
    ul.appendChild(el("li", {
      text: `段落 ${v.segment + 1} · 规则 ${v.rule} · ${v.voice || "声部间"} · ${v.detail}`,
    }));
  }
  box.appendChild(ul);
}

function renderCosts(result) {
  const tbody = document.getElementById("cost-table").querySelector("tbody");
  tbody.innerHTML = "";
  const profile = styleMap[state.style];
  const ruleLevel = Object.fromEntries(profile.rules.map((r) => [r.key, r.level]));
  Object.entries(result.cost_breakdown)
    .sort((a, b) => b[1] - a[1])
    .forEach(([key, value]) => {
      tbody.appendChild(el("tr", {},
        el("td", { text: key }),
        el("td", { text: ruleLevel[key] || (key === "range" ? "hard(relaxed)" : "?") }),
        el("td", { text: String(value) }),
      ));
    });
}

function renderBeamTrace(result) {
  const tbody = document.getElementById("beam-table").querySelector("tbody");
  tbody.innerHTML = "";
  result.beam_trace.forEach((row) => {
    tbody.appendChild(el("tr", {},
      el("td", { text: String(row.segment + 1) }),
      el("td", { text: String(row.candidates_enumerated) }),
      el("td", { text: String(row.distinct_states) }),
      el("td", { text: String(row.beam_kept) }),
      el("td", { text: String(row.tie_band_size || 0) }),
    ));
  });
}

function renderDiagnostics(result) {
  const wrap = document.getElementById("diagnostics");
  wrap.innerHTML = "";
  if (!result.diagnostics.length) {
    wrap.appendChild(el("p", { class: "muted", text: "无输入诊断告警。" }));
    return;
  }
  const ul = el("ul");
  result.diagnostics.forEach((d) => ul.appendChild(el("li", { text: d })));
  wrap.appendChild(ul);
}

// ---------------------------------------------------------------------------
// Canvas: voice-leading score (grand-staff-like lines per voice)
// ---------------------------------------------------------------------------

// pc -> [letter index C=0..B=6, accidental semitones]
const PC_TO_LETTER = {
  0: [0, 0], 1: [0, 1], 2: [1, 0], 3: [1, 1], 4: [2, 0], 5: [3, 0],
  6: [3, 1], 7: [4, 0], 8: [4, 1], 9: [5, 0], 10: [5, 1], 11: [6, 0],
};

function diatonicSteps(midi) {
  const octave = Math.floor(midi / 12) - 1;
  return octave * 7 + PC_TO_LETTER[midi % 12][0];
}

function noteLetter(midi) {
  return LETTER_ORDER[PC_TO_LETTER[midi % 12][0]];
}

function drawScore(result) {
  const canvas = document.getElementById("score-canvas");
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  const n = result.segments.length;
  const marginL = 90, marginR = 40, top = 40;
  const plotW = W - marginL - marginR;
  const colW = plotW / Math.max(1, n);

  // Y range across all sounding notes.
  const allMidi = result.segments.flatMap((s) => Object.values(s.voicing));
  const minMidi = Math.min(...allMidi) - 4;
  const maxMidi = Math.max(...allMidi) + 4;
  const minStep = diatonicSteps(minMidi);
  const maxStep = diatonicSteps(maxMidi);
  const plotH = H - top - 60;
  const yOf = (midi) => {
    const d = diatonicSteps(midi) - minStep;
    return top + plotH - (d / Math.max(1, maxStep - minStep)) * plotH;
  };
  const xOf = (idx) => marginL + colW * (idx + 0.5);

  // Horizontal reference lines at diatonic steps.
  ctx.strokeStyle = "#ece7dc";
  ctx.lineWidth = 1;
  for (let step = minStep; step <= maxStep; step++) {
    const y = yOf(minMidi) - ((step - minStep) / Math.max(1, maxStep - minStep)) * plotH;
    ctx.beginPath();
    ctx.moveTo(marginL - 10, y);
    ctx.lineTo(W - marginR, y);
    ctx.stroke();
  }

  // Segment separators + chord symbols.
  ctx.fillStyle = "#374151";
  ctx.font = "12px sans-serif";
  ctx.textAlign = "center";
  result.segments.forEach((seg, idx) => {
    const x = xOf(idx);
    ctx.strokeStyle = "#e5e7eb";
    ctx.beginPath();
    ctx.moveTo(x - colW / 2, top - 10);
    ctx.lineTo(x - colW / 2, top + plotH + 10);
    ctx.stroke();
    ctx.fillText(seg.chord.symbol || seg.chord.label, x, top - 16);
    ctx.fillStyle = "#9ca3af";
    ctx.fillText(String(idx + 1), x, top + plotH + 28);
    ctx.fillStyle = "#374151";
  });

  // Voice labels.
  ctx.textAlign = "right";
  VOICES.forEach((voice) => {
    ctx.fillStyle = VOICE_COLOR[voice];
    ctx.font = "bold 12px sans-serif";
    ctx.fillText(VOICE_LABEL[voice], marginL - 16, top + 24 + VOICES.indexOf(voice) * 18);
  });

  // Lines then noteheads.
  VOICES.forEach((voice, vi) => {
    ctx.strokeStyle = VOICE_COLOR[voice];
    ctx.fillStyle = VOICE_COLOR[voice];
    ctx.lineWidth = 2;
    ctx.beginPath();
    result.segments.forEach((seg, idx) => {
      const x = xOf(idx), y = yOf(seg.voicing[voice]);
      if (idx === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.stroke();

    result.segments.forEach((seg, idx) => {
      const midi = seg.voicing[voice];
      const x = xOf(idx), y = yOf(midi);
      ctx.beginPath();
      ctx.ellipse(x, y, 9, 6.5, 0, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      ctx.font = "9px sans-serif";
      ctx.textAlign = "center";
      const name = seg.note_names[voice];
      ctx.fillText(name.replace(/[0-9-]/g, ""), x, y + 3);
      ctx.fillStyle = VOICE_COLOR[voice];
      // Octave number under notehead.
      ctx.fillStyle = "#6b7280";
      ctx.font = "9px sans-serif";
      ctx.fillText(String(Math.floor(midi / 12) - 1), x, y + 18);
      ctx.fillStyle = VOICE_COLOR[voice];
    });

    // Interval labels between consecutive notes (acceptance check aid).
    ctx.fillStyle = VOICE_COLOR[voice];
    ctx.font = "10px sans-serif";
    ctx.textAlign = "center";
    for (let idx = 1; idx < result.segments.length; idx++) {
      const a = result.segments[idx - 1].voicing[voice];
      const b = result.segments[idx].voicing[voice];
      const gap = b - a;
      if (gap === 0) continue;
      const x = (xOf(idx - 1) + xOf(idx)) / 2;
      const y = (yOf(a) + yOf(b)) / 2 + (vi - 1.5) * 4;
      ctx.fillText(`${gap > 0 ? "+" : ""}${gap}`, x, y);
    }
  });

  // Fixed-note legend.
  ctx.textAlign = "left";
  ctx.fillStyle = "#7c3aed";
  ctx.font = "11px sans-serif";
  ctx.fillText("椭圆=声部音高，线上数字=相邻段半音程（正上行/负下行），固定音见下方表格紫色标记",
    marginL, H - 18);
}

function renderVoiceTable(result) {
  const wrap = document.getElementById("voice-table-wrap");
  wrap.innerHTML = "";
  const table = el("table");
  const thead = el("thead", {}, el("tr", {}, el("th", { text: "声部" })));
  result.segments.forEach((seg, idx) => {
    thead.firstChild.appendChild(el("th", { text: `${idx + 1} · ${seg.chord.symbol}` }));
  });
  table.appendChild(thead);
  const tbody = el("tbody");
  const fixedMap = result.input_snapshot.segments;
  VOICES.forEach((voice) => {
    const tr = el("tr", {}, el("td", { class: `voice-name-${voice}`, text: VOICE_LABEL[voice] }));
    result.segments.forEach((seg, idx) => {
      const cell = el("td", { text: seg.note_names[voice] });
      if (fixedMap[idx].fixed && fixedMap[idx].fixed[voice] != null) {
        cell.appendChild(el("span", { class: "fixed-pill", text: "固定" }));
      }
      tr.appendChild(cell);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  wrap.appendChild(table);
}

// ---------------------------------------------------------------------------
// Audio
// ---------------------------------------------------------------------------

async function ensureAudio() {
  if (audioCache) return audioCache;
  audioCache = await api("/api/synth", {
    events: lastEvents,
    bpm: Number(document.getElementById("bpm").value) || 90,
    sample_rate: 22050,
  });
  document.getElementById("engine-note").textContent =
    `合成引擎：${audioCache.engine}，采样率 ${audioCache.sample_rate} Hz`;
  return audioCache;
}

function decodeB64(b64) {
  const bin = atob(b64);
  const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return bytes.buffer;
}

async function playAudio(kind) {
  if (!lastEvents) return alert("请先求解");
  stopAudio();
  const cache = await ensureAudio();
  audioContext = audioContext || new (window.AudioContext || window.webkitAudioContext)();
  const b64 = kind === "ensemble" ? cache.ensemble_wav : cache.per_voice_wav[kind];
  const buffer = await audioContext.decodeAudioData(decodeB64(b64));
  activeSource = audioContext.createBufferSource();
  activeSource.buffer = buffer;
  activeSource.connect(audioContext.destination);
  activeSource.start();
  activeSource.onended = () => { activeSource = null; };
}

function stopAudio() {
  if (activeSource) {
    try { activeSource.stop(); } catch (_) { /* already stopped */ }
    activeSource = null;
  }
}

// ---------------------------------------------------------------------------
// Export / re-import
// ---------------------------------------------------------------------------

function exportEvents() {
  if (!lastEvents) return alert("请先求解");
  download("chorale_events.json", JSON.stringify(lastEvents, null, 2), "application/json");
}

async function exportBinary(kind) {
  if (!lastEvents) return alert("请先求解");
  const resp = await fetch("/api/export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind,
      events: lastEvents,
      bpm: Number(document.getElementById("bpm").value) || 90,
    }),
  });
  if (!resp.ok) return alert("导出失败");
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = el("a", { href: url, download: kind === "midi" ? "chorale.mid" : "chorale.bin" });
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

async function exportWav() {
  if (!lastEvents) return alert("请先求解");
  const cache = await ensureAudio();
  const bytes = decodeB64(cache.ensemble_wav);
  download("chorale_ensemble.wav", new Blob([bytes], { type: "audio/wav" }), "audio/wav");
}

async function reimport(events) {
  try {
    const data = await api("/api/reimport", { events });
    state.key_root_pc = data.rebuilt_input.key_root_pc;
    state.style = data.rebuilt_input.style;
    state.seed = data.rebuilt_input.seed;
    state.segments = data.rebuilt_input.segments.map((s) => ({
      duration: s.duration,
      breath_after: s.breath_after || 0,
      fixed: s.fixed || {},
      chord_candidates: s.chord_candidates.map((c) => ({
        root_pc: c.root_pc, quality: c.quality, label: c.label || "",
      })),
    }));
    syncGlobalControls();
    renderRules();
    renderSegments();
    lastResult = data.result;
    lastEvents = data.result.events;
    renderResult(data.result);
    document.getElementById("events-preview").textContent =
      "已从事件文件回读并通过内核重解，声部序列与导出结果一致。";
  } catch (err) {
    alert("回读失败：" + err.message);
  }
}

function buildEventsFromResult(result) {
  const events = [];
  let cursor = 0;
  result.segments.forEach((seg) => {
    const duration = result.input_snapshot.segments[seg.index].duration;
    VOICES.forEach((voice) => {
      const midi = seg.voicing[voice];
      events.push({
        event_id: `${seg.index}:${voice}`,
        segment: seg.index,
        voice,
        midi,
        name: seg.note_names[voice],
        start_beat: cursor,
        duration_beats: duration,
        start_time: null,
        duration_time: null,
        velocity: 78,
        chord_label: seg.chord.label || seg.chord.symbol,
        fixed: voice in (result.input_snapshot.segments[seg.index].fixed || {}),
      });
    });
    cursor += duration;
  });
  return {
    format: "chorale_arranger_events",
    version: 1,
    key_root_pc: result.key_root_pc,
    style: result.style,
    seed: result.seed,
    beam_width: result.beam_width,
    total_beats: cursor,
    voices: VOICES,
    segments: result.input_snapshot.segments,
    events,
  };
}

async function reimportFile(e) {
  const file = e.target.files[0];
  if (!file) return;
  const text = await file.text();
  try {
    const events = JSON.parse(text);
    await reimport(events);
  } catch (err) {
    alert("文件解析失败：" + err.message);
  }
  e.target.value = "";
}

window.choraleUI = {
  loadDocument(doc, options = {}) {
    state = JSON.parse(JSON.stringify(doc));
    state.candidate_cap = state.candidate_cap ?? 0;
    state.overrides = state.overrides || { levels: {}, weights: {}, params: {} };
    syncGlobalControls();
    renderRules();
    renderSegments();
    clearResult();
    if (options.solve) return runSolve();
  },
  runSolve,
  getState: () => state,
};

init().catch((err) => {
  document.body.insertAdjacentHTML("afterbegin",
    `<pre style="color:#b91c1c;padding:16px">初始化失败：${err.message}\n${err.stack || ""}</pre>`);
});
