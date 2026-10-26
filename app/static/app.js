/* FlashVSR+ Video Upscaler — frontend */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));

const state = {
  variant: "turbo",
  models: [],
  fetchJob: null,
  upJob: null,
  file: null,
  source: null,
  thumbUrl: null,
  settings: { scale_mode: "factor", scale_factor: 2, target_px: 1080, group: 24, colorfix: "wavelet", decoder: "tiny", tcomp: 4, seed: 42, audio: true, randomSeed: false, fps: "auto" },
  lastOutW: null,
  lastOutH: null,
  pollTimer: null,
};

/* ---------------- API helpers ---------------- */

async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(msg);
  }
  return res.json();
}

function fmtSize(n) {
  if (!n) return "";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n > 1024 && i < u.length - 1) { n /= 1024; i++; }
  return n.toFixed(n >= 100 || i === 0 ? 0 : 1) + " " + u[i];
}

// Middle-truncate a filename: keep the beginning + the end, ellipsize the
// middle (e.g. "my_very_long_clip_final_v2.mov" -> "my_very…inal_v2.mov").
function truncateName(name, max = 40) {
  if (!name) return "";
  if (name.length <= max) return name;
  const keep = Math.max(4, Math.floor((max - 1) / 2));
  return name.slice(0, keep) + "…" + name.slice(name.length - keep);
}
function fmtDur(s) {
  if (s == null || isNaN(s)) return "";
  s = Math.round(s);
  if (s < 60) return s + "s";
  const m = Math.floor(s / 60), sec = s % 60;
  if (m < 60) return m + "m " + String(sec).padStart(2, "0") + "s";
  const h = Math.floor(m / 60);
  return h + "h " + (m % 60) + "m";
}

function toast(msg, isErr = true) {
  const el = document.createElement("div");
  el.textContent = msg;
  el.style.cssText = `
    position: fixed; bottom: 20px; right: 20px; z-index: 99;
    background: ${isErr ? "#3a1518" : "#12291c"};
    color: ${isErr ? "#fca5a5" : "#86efac"};
    border: 1px solid ${isErr ? "#7f1d1d" : "#14532d"};
    padding: 10px 16px; border-radius: 10px; font-size: 13px;
    box-shadow: 0 6px 24px rgba(0,0,0,.5); max-width: 420px;
  `;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), isErr ? 6000 : 3000);
}

/* ---------------- System info ---------------- */

async function loadSystem() {
  try {
    const d = await api("/api/system");
    const b = $("#badge-vpipe");
    if (d.vpipe.found) { b.textContent = "vpipe ✓"; b.classList.add("ok"); }
    else { b.textContent = "vpipe missing — run Install"; b.classList.add("bad"); }
    const s = [];
    if (d.chip) s.push(d.chip.replace("Apple M", "M"));
    if (d.ram_gb) s.push(d.ram_gb + " GB");
    const el = $("#badge-sys");
    el.textContent = s.join(" · ");
  } catch {}
}

/* ---------------- Models ---------------- */

async function loadModels() {
  try {
    const d = await api("/api/models");
    state.models = d.models;
    renderModelFiles();
  } catch {}
}

function renderModelFiles() {
  const m = state.models.find((x) => x.id === state.variant);
  const box = $("#model-files");
  if (!m) { box.innerHTML = ""; return; }
  // Just a downloaded / not-downloaded tick — no per-file list.
  const status = m.ready
    ? `<span class="ok">✓ Downloaded</span>`
    : m.fetching
      ? `<span class="hint">Downloading…</span>`
      : `<span class="miss">Not downloaded</span>`;
  box.innerHTML = `<div class="mf"><span>Model files</span><span>${status}</span></div>`;
  $("#variant-desc").textContent = m.desc;
  const btn = $("#btn-fetch");
  btn.disabled = m.ready || m.fetching;
  btn.textContent = m.ready ? "Model ready ✓" : m.fetching ? "Fetching…" : "Fetch model";
  if (m.fetching && m.fetch_job) startJobPoll(m.fetch_job, "fetch");
}

async function startFetch() {
  const m = state.models.find((x) => x.id === state.variant);
  if (!m || m.ready) return;
  try {
    const d = await api("/api/models/fetch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ variant: state.variant }),
    });
    toast("Fetching " + state.variant + " model…", false);
    startJobPoll(d.job, "fetch");
  } catch (e) {
    toast("Fetch failed: " + e.message);
  }
}

async function cancelFetch() {
  if (state.fetchJob) {
    try { await api(`/api/jobs/${state.fetchJob}/cancel`, { method: "POST" }); } catch {}
  }
}

/* ---------------- Job polling ---------------- */

function startJobPoll(jobId, kind) {
  if (state.pollTimer) clearInterval(state.pollTimer);
  if (kind === "fetch") {
    state.fetchJob = jobId;
    $("#fetch-progress").hidden = false;
  } else {
    state.upJob = jobId;
    $("#jobbar").hidden = false;
    $("#btn-upscale").disabled = true;
    $("#btn-cancel").hidden = false;
    // Clear any failure state left over from a previous run.
    $("#job-error").hidden = true;
    $("#job-error").textContent = "";
    $("#job-fill").classList.remove("failed");
  }
  const tick = async () => {
    let d;
    try {
      d = await api(`/api/jobs/${jobId}`);
    } catch {
      return;
    }
    const j = d.job;
    if (kind === "fetch") {
      $("#fetch-fill").style.width = j.pct + "%";
      $("#fetch-status").textContent = j.pct.toFixed(0) + "% · " + (j.desc || j.phase);
      if (j.state === "done") {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.fetchJob = null;
        $("#fetch-progress").hidden = true;
        toast(state.variant + " model ready ✓", false);
        loadModels();
      } else if (j.state === "failed" || j.state === "cancelled") {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.fetchJob = null;
        $("#fetch-progress").hidden = true;
        toast("Fetch " + j.state + (j.error ? ": " + j.error : ""));
        loadModels();
      }
    } else {
      $("#job-phase").textContent = j.phase + (j.desc && j.desc !== j.phase ? " — " + j.desc : "") + (j.detail ? " (" + j.detail + ")" : "");
      $("#job-pct").textContent = j.pct.toFixed(0) + "%";
      $("#job-fill").style.width = j.pct + "%";
      $("#job-log").textContent = j.log.join("\n");
      $("#job-log").scrollTop = $("#job-log").scrollHeight;
      if (j.state === "done") {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.upJob = null;
        $("#btn-upscale").disabled = false;
        $("#btn-cancel").hidden = true;
        showResult(jobId);
        loadLibrary();
        flashBolt();  // flicker once more to signal "done"
        toast("Upscale complete ✓", false);
      } else if (j.state === "failed" || j.state === "cancelled") {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.upJob = null;
        $("#btn-upscale").disabled = false;
        $("#btn-cancel").hidden = true;
        // Show a persistent, prominent failure in the job bar. A toast alone
        // vanishes, and without this the user just sees a stuck progress bar
        // with no idea why the run stopped.
        const failed = j.state === "failed";
        $("#jobbar").hidden = false;
        $("#job-phase").textContent = failed ? "Failed" : "Cancelled";
        $("#job-pct").textContent = "";
        $("#job-fill").classList.toggle("failed", failed);
        $("#job-fill").style.width = failed ? "100%" : "0%";
        const err = j.error || (failed ? "vpipe stopped unexpectedly" : "Run cancelled.");
        $("#job-error").textContent = err;
        $("#job-error").hidden = false;
        toast("Job " + j.state + (j.error ? ": " + j.error : ""));
      }
    }
  };
  tick();
  state.pollTimer = setInterval(tick, 1000);
}

/* ---------------- File selection ---------------- */

const dz = $("#dz-video");
const fileInput = $("#file-video");

dz.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => pickFile(fileInput.files[0]));
["dragenter", "dragover"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); })
);
["dragleave", "drop"].forEach((ev) =>
  dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); })
);
dz.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) pickFile(f);
});

function pickFile(f) {
  if (!f) return;
  if (!/^(video\/|.*mkv)/.test(f.type) && !/\.(mp4|mov|m4v|mkv|webm|avi)$/i.test(f.name)) {
    toast("Please choose a video file");
    return;
  }
  state.file = f;
  state.source = null;
  $("#res-estimate").hidden = true;
  // Recreate the thumbnail object URL (revoke the previous one to avoid leaks).
  if (state.thumbUrl) { try { URL.revokeObjectURL(state.thumbUrl); } catch {} }
  state.thumbUrl = URL.createObjectURL(f);
  const mb = (f.size / 1048576).toFixed(1);
  const name = truncateName(f.name, 46);
  $("#video-meta").hidden = false;
  $("#video-meta").innerHTML =
    `<b>${escapeHtml(name)}</b> · ${mb} MB<br>` +
    `<span class="hint">Reading resolution…</span>`;
  dz.innerHTML = `
    <video class="dz-thumb" src="${state.thumbUrl}" muted preload="metadata" playsinline></video>
    <p><b>${escapeHtml(name)}</b></p>
    <p class="hint">Click to replace</p>`;
  fileInput.hidden = true;
  dz.appendChild(fileInput);
  $("#upscale-note").textContent = "Ready to upscale.";
  probeSource(f);
}

// Read the source clip's dimensions client-side (no upload) so the output
// resolution estimate can be shown for the current scale × / shortest side.
function probeSource(f) {
  const url = URL.createObjectURL(f);
  const v = document.createElement("video");
  v.preload = "metadata";
  v.muted = true;
  v.onloadedmetadata = () => {
    if (v.videoWidth && v.videoHeight) {
      state.source = { width: v.videoWidth, height: v.videoHeight, duration: v.duration || null };
      const mb = (f.size / 1048576).toFixed(1);
      const aspect = (v.videoWidth / v.videoHeight).toFixed(3);
      $("#video-meta").innerHTML =
        `<b>${escapeHtml(truncateName(f.name, 46))}</b> · ${mb} MB<br>` +
        [`${v.videoWidth}×${v.videoHeight}`, v.duration && Math.round(v.duration) + " s", "aspect " + aspect]
          .filter(Boolean).join(" · ") +
        `<br><span class="hint">Aspect is preserved automatically — set a scale × or shortest side below</span>`;
      updateResEstimate();
    }
    URL.revokeObjectURL(url);
  };
  v.onerror = () => URL.revokeObjectURL(url);
  v.src = url;
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ---------------- Settings chips ---------------- */

function bindChips(sel, key, transform) {
  $(sel).addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    $$(sel + " button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    state.settings[key] = transform(b);
  });
}

bindChips("#colorfix-chips", "colorfix", (b) => b.dataset.cf);
bindChips("#decoder-chips", "decoder", (b) => b.dataset.dec);
bindChips("#tcomp-chips", "tcomp", (b) => +b.dataset.tc);
bindChips("#fps-chips", "fps", (b) => b.dataset.fps);

// --- Output resolution: scale factor (1x–4x) OR shortest-side px. Both
// preserve the source aspect by construction — the backend scales both sides
// by the same factor and snaps to the 128-px grid keeping the ratio — so there
// is no "match source" step and no stretching. ---
// Snap an aspect-preserving target to the vpipe 128-px grid. Mirrors the
// backend's _snap_to_grid_aspect so the live estimate matches the real output.
function snapGrid(w, h) {
  const ratio = w / h;
  const idealN = Math.sqrt((w * h) / ratio) / 128;
  const idealUnits = (w * h) / (128 * 128);
  let best = [128, 128], bestScore = Infinity;
  for (let n = Math.max(1, Math.round(idealN) - 4); n <= Math.round(idealN) + 4; n++) {
    const m = Math.max(1, Math.round(n * ratio));
    const score = Math.abs(m / n - ratio) / ratio + 0.15 * (Math.abs(m * n - idealUnits) / idealUnits);
    if (score < bestScore) { bestScore = score; best = [m * 128, n * 128]; }
  }
  return best;
}
// Raw (pre-grid) output size for the current mode + probed source.
function rawOutSize() {
  const s = state.settings, src = state.source;
  if (!src) return null;
  const scale = s.scale_mode === "target"
    ? s.target_px / Math.min(src.width, src.height)
    : s.scale_factor;
  return [src.width * scale, src.height * scale];
}
// Live "≈ W×H → on-grid W×H" preview under the controls.
function updateResEstimate() {
  const el = $("#res-estimate");
  const raw = rawOutSize();
  if (!raw) { el.hidden = true; return; }
  const [tw, th] = snapGrid(raw[0], raw[1]);
  const s = state.settings;
  const label = s.scale_mode === "target"
    ? `shortest ${s.target_px}px`
    : `${s.scale_factor}× of ${state.source.width}×${state.source.height}`;
  el.hidden = false;
  el.textContent = `Output ${Math.round(raw[0])}×${Math.round(raw[1])} (${label}) → ${tw}×${th}`;
}
$("#res-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  $$("#res-mode button").forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  state.settings.scale_mode = b.dataset.mode;
  const isFactor = b.dataset.mode === "factor";
  $("#res-factor-row").style.display = isFactor ? "" : "none";
  $("#res-target-row").style.display = isFactor ? "none" : "";
  updateResEstimate();
});
$("#res-scale").addEventListener("input", (e) => {
  state.settings.scale_factor = +e.target.value;
  $("#res-scale-val").textContent = (+e.target.value).toFixed(1) + "×";
  updateResEstimate();
});
const targetSlider = $("#res-target"), targetNum = $("#res-target-num");
targetSlider.addEventListener("input", (e) => {
  state.settings.target_px = +e.target.value;
  targetNum.value = e.target.value;
  updateResEstimate();
});
targetNum.addEventListener("input", (e) => {
  const v = Math.max(64, Math.min(16384, parseInt(e.target.value) || 480));
  state.settings.target_px = v;
  targetSlider.value = Math.max(480, Math.min(4320, v));
  updateResEstimate();
});
$("#in-seed").addEventListener("change", (e) => (state.settings.seed = +e.target.value || 0));
$("#in-random-seed").addEventListener("change", (e) => {
  state.settings.randomSeed = e.target.checked;
  $("#in-seed").disabled = e.target.checked;
});
$("#in-audio").addEventListener("change", (e) => (state.settings.audio = e.target.checked));

$("#variant-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  $$("#variant-seg button").forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  state.variant = b.dataset.v;
  loadModels();
});

/* ---------------- Upscale ---------------- */

$("#btn-fetch").addEventListener("click", startFetch);
$("#btn-fetch-cancel").addEventListener("click", cancelFetch);

$("#btn-cancel").addEventListener("click", async () => {
  if (state.upJob) {
    try { await api(`/api/jobs/${state.upJob}/cancel`, { method: "POST" }); } catch {}
  }
});

// Lightning flicker on the header logo. Runs once (~6s) then stops.
// Re-triggerable: strip the class, force a reflow, re-add it.
function flashBolt() {
  const logo = document.querySelector(".hero-logo");
  if (!logo) return;
  logo.classList.remove("flashing");
  void logo.offsetWidth; // reflow so the animation can restart
  logo.classList.add("flashing");
}

$("#btn-upscale").addEventListener("click", async () => {
  if (!state.file) { toast("Choose an input video first"); return; }
  const m = state.models.find((x) => x.id === state.variant);
  if (!m || !m.ready) { toast("Fetch the " + state.variant + " model first"); return; }
  const variant = m.id;  // guaranteed a valid VARIANTS key (comes from /api/models)
  flashBolt();  // lightning flicker while the upscale runs
  const s = state.settings;
  const usedSeed = s.randomSeed ? Math.floor(Math.random() * 2147483647) : s.seed;
  const params = {
    variant: variant,
    scale_mode: s.scale_mode,
    scale_factor: s.scale_factor,
    target_px: s.target_px,
    fps: s.fps === "auto" ? null : Number(s.fps),
    frames: 49,
    seed: usedSeed,
    group_size: s.group,
    color_fix: s.colorfix,
    decoder: s.decoder,
    temporal_compression: s.tcomp,
    keep_audio: s.audio,
  };
  const fd = new FormData();
  fd.append("file", state.file);
  fd.append("params", JSON.stringify(params));
  $("#btn-upscale").disabled = true;
  $("#btn-upscale").textContent = "Uploading…";
  try {
    const d = await api("/api/video/upscale", { method: "POST", body: fd });
    const meta = d.meta || {};
    state.lastOutW = meta.out_width || null;
    state.lastOutH = meta.out_height || null;
    $("#video-meta").hidden = false;
    $("#video-meta").innerHTML =
      `<b>${escapeHtml(truncateName(state.file.name, 46))}</b><br>` +
      [
        meta.width && `${meta.width}×${meta.height}` + (meta.out_width ? ` → ${meta.out_width}×${meta.out_height}` : ""),
        meta.fps && meta.fps + " fps",
        meta.frames && meta.frames + " frames",
        meta.duration && (Math.round(meta.duration) + " s"),
      ].filter(Boolean).join(" · ") +
      `<br><span class="hint">${state.variant} · seed ${usedSeed}${s.randomSeed ? " (random)" : ""}</span>`;
    startJobPoll(d.job, "upscale");
  } catch (e) {
    toast("Upscale failed: " + e.message);
  } finally {
    $("#btn-upscale").textContent = "⚡ Upscale Video";
    if (!state.upJob) $("#btn-upscale").disabled = false;
  }
});

/* ---------------- Result ---------------- */

function showResult(jobId) {
  switchTab("result");
  const v = $("#result-video");
  v.src = `/api/outputs/${jobId}/output.mp4`;
  v.load();
  if (resZoom) resZoom.reset();
  $("#result-placeholder").style.display = "none";
  $("#result-actions").hidden = false;
  $("#result-download").href = `/api/download/${jobId}`;
  const s = state.settings;
  $("#result-info").textContent = `${state.lastOutW ? state.lastOutW + "×" + state.lastOutH : "—"} · ${state.variant}`;
  // Size the result viewport to the video's aspect ratio so it fills the width
  // without cropping (mirrors how the compare stage is sized).
  const fitClip = () => {
    const w = v.videoWidth, h = v.videoHeight;
    if (w && h) $("#result-zoom-clip").style.aspectRatio = `${w} / ${h}`;
  };
  v.addEventListener("loadedmetadata", fitClip, { once: true });
  fitClip();
  // Pre-load the compare view with the just-finished job's input + output.
  const inName = state.file ? `input${extOf(state.file.name)}` : null;
  setCompare(jobId, inName);
}

function extOf(name) {
  const m = /\.([a-z0-9]+)$/i.exec(name || "");
  return m ? "." + m[1].toLowerCase() : ".mp4";
}

/* ---------------- Compare ---------------- */

const cmpBefore = $("#cmp-before");
const cmpAfter = $("#cmp-after");
const cmpSlider = $("#cmp-slider");
const cmpDivider = $("#cmp-divider");

function setCompare(jobId, inName) {
  cmpAfter.src = `/api/outputs/${jobId}/output.mp4`;
  cmpAfter.load();
  if (inName) {
    cmpBefore.src = `/api/uploads/${jobId}/${inName}`;
    cmpBefore.load();
  } else {
    // No stored original (e.g. the upload was cleared) — show a clean black
    // left side instead of a stale frame from a previous comparison.
    cmpBefore.removeAttribute("src");
    cmpBefore.load();
  }
  if (cmpZoom) cmpZoom.reset();
  $("#compare-placeholder").style.display = "none";
  $("#cmp-actions").hidden = false;
  $("#cmp-seekrow").hidden = false;
  setComparePos(cmpSlider.value);
  if (cmpSeek) updateCmpSeek();
  // Size the stage to the output's aspect ratio so the video fills the panel
  // width (matching the Result tab) instead of letterboxing in a fixed box.
  const fitStage = () => {
    const w = cmpAfter.videoWidth, h = cmpAfter.videoHeight;
    if (w && h) {
      // Size the CLIP to the video ratio (full-width, like the Result clip) so
      // the stage stays panel-width and the video centers via object-fit:contain.
      const clip = $("#cmp-zoom-clip");
      if (clip) clip.style.aspectRatio = `${w} / ${h}`;
    }
  };
  cmpAfter.addEventListener("loadedmetadata", fitStage, { once: true });
  fitStage();
}

function setComparePos(x) {
  cmpBefore.style.clipPath = `inset(0 ${100 - x}% 0 0)`;
  cmpDivider.style.left = x + "%";
}

cmpSlider.addEventListener("input", () => setComparePos(+cmpSlider.value));

/* ---------- Zoom / pan controller (mirrors the SeedVR2 app) ---------- */
// `wrapper` is the element that gets scaled/translated; `rectEl` is the fixed
// visible viewport used to convert pointer positions and clamp the pan.
function makeZoom(wrapper, rectEl, onApply) {
  const st = { level: 1, x: 0, y: 0 };
  function clamp() {
    const rect = rectEl.getBoundingClientRect();
    if (!rect || rect.width === 0) return;
    const mx = ((1 - 1 / st.level) * rect.width) / 2;
    const my = ((1 - 1 / st.level) * rect.height) / 2;
    st.x = Math.max(-mx, Math.min(mx, st.x));
    st.y = Math.max(-my, Math.min(my, st.y));
  }
  function apply() {
    const s = st.level;
    wrapper.style.transform = s === 1 ? "" : `scale(${s}) translate(${st.x}px, ${st.y}px)`;
    if (onApply) onApply(s);
  }
  return {
    get level() { return st.level; },
    get panX() { return st.x; },
    get panY() { return st.y; },
    reset() { st.level = 1; st.x = 0; st.y = 0; apply(); },
    apply,
    zoomAt(clientX, clientY, delta) {
      const rect = rectEl.getBoundingClientRect();
      if (!rect || rect.width === 0) return;
      const s = st.level;
      const s2 = Math.max(1, Math.min(8, s + delta));
      if (s2 === s) return;
      const cx = clientX - rect.left, cy = clientY - rect.top;
      const ox = rect.width / 2, oy = rect.height / 2;
      const ux = (cx + (s - 1) * ox) / s;
      const uy = (cy + (s - 1) * oy) / s;
      st.x = (cx + (s2 - 1) * ox) / s2 - (ux - st.x);
      st.y = (cy + (s2 - 1) * oy) / s2 - (uy - st.y);
      st.level = s2;
      if (st.level === 1) { st.x = 0; st.y = 0; }
      clamp();
      apply();
    },
    panBy(dx, dy) {
      st.x += dx / st.level;
      st.y += dy / st.level;
      clamp();
      apply();
    },
  };
}

let cmpZoom = null;
let resZoom = null;
let sliderDragging = false;
let panDragging = false;
let panLastX = 0, panLastY = 0, panStartX = 0, panStartY = 0;

// Map a clientX to the divider position, accounting for the current zoom/pan.
function setSliderFromClientX(clientX) {
  if (!cmpZoom) return;
  const rect = $("#compare-stage").getBoundingClientRect();
  if (!rect || rect.width === 0) return;
  const s = cmpZoom.level;
  const px = (clientX - rect.left + (s - 1) * (rect.width / 2)) / s - cmpZoom.panX;
  const v = Math.max(0, Math.min(100, (px / rect.width) * 100));
  cmpSlider.value = v;
  setComparePos(v);
}
// Screen X of the divider line for a given divider position (accounts for zoom
// + pan). The divider has pointer-events: none, so a grab is detected by
// proximity to this X — works at any zoom level.
function dividerScreenX(xPct) {
  const rect = $("#compare-stage").getBoundingClientRect();
  const s = cmpZoom ? cmpZoom.level : 1;
  const panX = cmpZoom ? cmpZoom.panX : 0;
  const W = rect.width;
  const divX = (xPct / 100) * W;
  return rect.left + s * (divX + panX) - (s - 1) * W / 2;
}
function panTo(clientX, clientY) {
  if (!cmpZoom) return;
  cmpZoom.panBy(clientX - panLastX, clientY - panLastY);
  panLastX = clientX; panLastY = clientY;
}
function beginCmpDrag(clientX, clientY, target) {
  const onSlider = target === cmpSlider;
  // Grabbing the divider line (anywhere along it) moves the divider — even
  // zoomed in. Everything else in a zoomed view pans.
  const nearDivider = cmpZoom && cmpZoom.level > 1 &&
    Math.abs(clientX - dividerScreenX(+cmpSlider.value)) <= 14;
  if (onSlider || nearDivider || !cmpZoom || cmpZoom.level <= 1) {
    sliderDragging = true;
    setSliderFromClientX(clientX);
  } else {
    panDragging = true;
    panLastX = panStartX = clientX;
    panLastY = panStartY = clientY;
  }
}
function endCmpDrag(clientX, clientY) {
  // A "pan" that barely moved was really a click → snap the divider to it.
  if (panDragging && cmpZoom && cmpZoom.level > 1) {
    if (Math.hypot(clientX - panStartX, clientY - panStartY) < 5) setSliderFromClientX(clientX);
  }
  sliderDragging = false;
  panDragging = false;
}

function setupComparisonSlider() {
  const cmp = $("#compare-stage");
  if (!cmp) return;
  cmp.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    beginCmpDrag(e.clientX, e.clientY, e.target);
  });
  window.addEventListener("pointermove", (e) => {
    if (sliderDragging) setSliderFromClientX(e.clientX);
    else if (panDragging) panTo(e.clientX, e.clientY);
  });
  window.addEventListener("pointerup", (e) => endCmpDrag(e.clientX, e.clientY));
}

function setupCompareZoom() {
  const cmp = $("#compare-stage");
  if (!cmp) return;
  cmpZoom = makeZoom($("#cmp-zoom"), cmp, (s) => {
    const knob = $("#cmp-knob");
    if (knob) knob.style.transform = `translate(-50%, -50%) scale(${1 / s})`;
    $("#cmp-zoom-level").textContent = `${Math.round(s * 100)}%`;
    $("#cmp-zoom-clip").classList.toggle("zoomed", s > 1);
  });
  cmp.addEventListener("wheel", (e) => {
    e.preventDefault();
    cmpZoom.zoomAt(e.clientX, e.clientY, e.deltaY > 0 ? -0.25 : 0.25);
  }, { passive: false });
  cmp.addEventListener("dblclick", () => cmpZoom.reset());
  $("#cmp-zoom-reset").addEventListener("click", () => cmpZoom.reset());
}

function setupResultZoom() {
  const clip = $("#result-zoom-clip");
  if (!clip) return;
  resZoom = makeZoom($("#result-zoom"), clip, (s) => {
    $("#res-zoom-level").textContent = `${Math.round(s * 100)}%`;
    clip.classList.toggle("zoomed", s > 1);
  });
  clip.addEventListener("wheel", (e) => {
    e.preventDefault();
    resZoom.zoomAt(e.clientX, e.clientY, e.deltaY > 0 ? -0.25 : 0.25);
  }, { passive: false });
  clip.addEventListener("dblclick", () => resZoom.reset());
  $("#res-zoom-reset").addEventListener("click", () => resZoom.reset());
  let dragging = false, lastX = 0, lastY = 0;
  clip.addEventListener("pointerdown", (e) => {
    if (resZoom.level <= 1) return;
    e.preventDefault();
    dragging = true; lastX = e.clientX; lastY = e.clientY;
  });
  window.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    resZoom.panBy(e.clientX - lastX, e.clientY - lastY);
    lastX = e.clientX; lastY = e.clientY;
  });
  window.addEventListener("pointerup", () => { dragging = false; });
}

// Keep the two compare videos in lockstep. One master (the upscaled "after")
// drives a single slave via a requestAnimationFrame loop. `timeupdate` fires
// only ~4×/sec, so drift accumulates between events and the clips visibly slip
// apart (even at identical fps). Polling every frame keeps them locked.
//
// The two clips can also differ in length — vpipe sometimes drops a few video
// frames, so the "after" is a touch shorter than the "before". We cap playback
// at the shorter one so they start AND end together, instead of the longer
// clip playing on past the end of the shorter (which reads as "out of sync").
function linkVideos(master, slave, onCapped) {
  let rafId = null;
  const cap = () => {
    const a = (master.duration && isFinite(master.duration)) ? master.duration : Infinity;
    const b = (slave.duration && isFinite(slave.duration)) ? slave.duration : Infinity;
    return Math.min(a, b);
  };
  const sync = () => {
    if (master.paused || master.ended) { rafId = null; return; }
    const c = cap();
    if (isFinite(c) && master.currentTime >= c - 0.03) {
      try { master.currentTime = c; slave.currentTime = c; } catch {}
      master.pause(); slave.pause();
      rafId = null;
      if (onCapped) onCapped();
      return;
    }
    if (Math.abs(slave.currentTime - master.currentTime) > 0.05) slave.currentTime = master.currentTime;
    rafId = requestAnimationFrame(sync);
  };
  const start = () => { if (rafId == null) rafId = requestAnimationFrame(sync); };
  const stop = () => { if (rafId != null) { cancelAnimationFrame(rafId); rafId = null; } };
  master.addEventListener("play", () => {
    slave.currentTime = master.currentTime;
    slave.play().catch(() => {});
    start();
  });
  master.addEventListener("pause", stop);
  master.addEventListener("ended", () => { stop(); try { slave.pause(); } catch {} });
  master.addEventListener("seeked", () => { try { slave.currentTime = master.currentTime; } catch {} });
  // If the slave starts playing on its own (stray play()), pull it back in line.
  slave.addEventListener("play", () => slave.pause());
}
linkVideos(cmpAfter, cmpBefore, () => setCmpPlay(false));

let cmpPlaying = false;
function setCmpPlay(playing) {
  cmpPlaying = playing;
  $("#cmp-play").textContent = playing ? "⏸ Pause" : "▶ Play";
}
async function playCompare() {
  // If any clip is at/has passed its end (including a cap set by a shorter
  // clip), rewind ALL of them to 0 so they start together. Otherwise resume
  // from the current position.
  const restart = [cmpBefore, cmpAfter].some(
    (v) => v.src && (v.ended || (v.duration && v.currentTime >= v.duration - 0.15)));
  for (const v of [cmpBefore, cmpAfter]) {
    if (!v.src) continue;
    if (restart) {
      try { v.currentTime = 0; } catch {}
    }
    if (v.readyState < 3) {
      await new Promise((res) => {
        v.addEventListener("canplay", res, { once: true });
        v.load();
      });
    }
    v.play().catch(() => {});
  }
  setCmpPlay(true);
}
$("#cmp-play").addEventListener("click", () => {
  if (cmpPlaying) {
    cmpBefore.pause();
    cmpAfter.pause();
    setCmpPlay(false);
  } else {
    playCompare();
  }
});
// A finished video must flip the button back to "Play" (was stuck on "Pause").
cmpAfter.addEventListener("ended", () => setCmpPlay(false));
cmpBefore.addEventListener("ended", () => setCmpPlay(false));
$("#cmp-reset").addEventListener("click", () => {
  cmpBefore.pause();
  cmpAfter.pause();
  cmpBefore.currentTime = 0;
  cmpAfter.currentTime = 0;
  setCmpPlay(false);
  if (cmpSeek) updateCmpSeek();
});

/* ---------------- Compare seek bar ---------------- */
const cmpSeek = $("#cmp-seek");
const cmpTime = $("#cmp-time");
function fmtCmpT(s) {
  if (!isFinite(s) || s < 0) s = 0;
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return m + ":" + String(sec).padStart(2, "0");
}
function updateCmpSeek() {
  if (!cmpSeek) return;
  const v = cmpAfter;
  if (!v.duration || !isFinite(v.duration) || v.duration <= 0) {
    cmpSeek.value = 0;
    if (cmpTime) cmpTime.textContent = "0:00 / 0:00";
    return;
  }
  cmpSeek.value = Math.round((v.currentTime / v.duration) * 1000);
  if (cmpTime) cmpTime.textContent = fmtCmpT(v.currentTime) + " / " + fmtCmpT(v.duration);
}
// Smooth rAF-driven scrubber while playing; timeupdate covers paused seeks.
function cmpSeekTick() {
  updateCmpSeek();
  if (cmpAfter.src && !cmpAfter.paused && !cmpAfter.ended) requestAnimationFrame(cmpSeekTick);
}
cmpAfter.addEventListener("timeupdate", updateCmpSeek);
cmpAfter.addEventListener("loadedmetadata", updateCmpSeek);
cmpAfter.addEventListener("play", cmpSeekTick);
if (cmpSeek) {
  cmpSeek.addEventListener("input", () => {
    const v = cmpAfter;
    if (!v.duration || !isFinite(v.duration) || v.duration <= 0) return;
    try { v.currentTime = (cmpSeek.value / 1000) * v.duration; } catch {}
    updateCmpSeek();
  });
}

/* ---------------- Tabs ---------------- */

function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("on", t.dataset.tab === name));
  $$(".tabpane").forEach((p) => p.classList.toggle("on", p.id === "pane-" + name));
}
$$(".tab").forEach((t) => t.addEventListener("click", () => switchTab(t.dataset.tab)));

/* ---------------- Library ---------------- */

async function loadLibrary() {
  let d;
  try {
    d = await api("/api/outputs");
  } catch {
    return;
  }
  const grid = $("#lib-grid");
  grid.innerHTML = "";
  $("#lib-empty").hidden = d.outputs.length > 0;
  const totalEl = $("#lib-total-size");
  if (totalEl) {
    totalEl.textContent = d.total_size
      ? `Total size: approx. ${fmtSize(d.total_size)} (outputs + inputs)`
      : "";
  }
  // Latest first (the backend orders by folder name, not time).
  const outputs = [...d.outputs].sort((a, b) => (b.created || 0) - (a.created || 0));
  for (const o of outputs) {
    const card = document.createElement("div");
    card.className = "lib-card";
    const p = o.meta && o.meta.params ? o.meta.params : {};
    const src = o.meta && o.meta.src ? o.meta.src : {};
    const badges = [
      p.width && `${p.width}×${p.height}`,
      p.variant && `<span class="v">${escapeHtml(p.variant)}</span>`,
      src.width && `in ${src.width}×${src.height}`,
      src.duration ? fmtDur(src.duration) : null,
      src.fps ? src.fps + " fps" : null,
    ].filter(Boolean);
    const full = o.meta && o.meta.filename || "output " + o.id;
    card.innerHTML = `
      <video src="/api/outputs/${o.id}/output.mp4" preload="metadata" muted playsinline></video>
      <div class="lib-body">
        <div class="lib-name" title="${escapeHtml(full)}">${escapeHtml(truncateName(full, 40))}</div>
        <div class="lib-badges">${badges.map((b) => `<span>${b}</span>`).join("")}</div>
        <div class="lib-foot">
          <span class="lib-size">${fmtSize(o.size)}${o.meta && o.meta.generation_time ? " · " + escapeHtml(o.meta.generation_time) : ""} · ${new Date(o.created * 1000).toLocaleDateString()}</span>
          <span class="lib-actions">
            <a class="btn sm" href="/api/download/${o.id}" download>⬇</a>
            <button class="btn ghost sm act-compare">⇄</button>
            <button class="btn danger sm act-delete" title="Delete">🗑</button>
          </span>
        </div>
      </div>`;
    card.querySelector(".act-compare").addEventListener("click", (e) => {
      e.stopPropagation();
      switchTab("compare");
      setCompare(o.id, o.input);
    });
    card.querySelector(".act-delete").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this generation? This cannot be undone.")) return;
      try {
        await api(`/api/outputs/${o.id}/delete`, { method: "POST" });
        toast("Deleted", false);
        loadLibrary();
      } catch (err) {
        toast("Delete failed: " + err.message);
      }
    });
    // Clicking the card / thumbnail opens it on the Result page (the ⬇ / ⇄
    // buttons in .lib-actions stopPropagation, so they keep their own action).
    card.addEventListener("click", (e) => {
      if (e.target.closest(".lib-actions")) return;
      showResult(o.id);
    });
    grid.appendChild(card);
  }
}

$("#btn-refresh-lib").addEventListener("click", loadLibrary);
$("#btn-clear-lib").addEventListener("click", async () => {
  if (!confirm("Delete ALL outputs? This cannot be undone.")) return;
  try {
    const d = await api("/api/outputs/clear", { method: "POST" });
    toast(`Removed ${d.removed} output(s)`, false);
    loadLibrary();
  } catch (e) {
    toast("Clear failed: " + e.message);
  }
});

/* ---------------- Init ---------------- */

setupCompareZoom();
setupResultZoom();
setupComparisonSlider();
loadSystem();
loadModels();
loadLibrary();
setInterval(() => {
  // Keep model file status fresh while idle (cheap).
  if (!state.fetchJob) loadModels();
}, 5000);
