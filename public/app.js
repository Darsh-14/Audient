import { AudientRuntime } from "./runtime.js";
import { BrowserPerception } from "./perception.js";
import { LiveSpeech, Voice } from "./speech.js";
import { I, toolIcon } from "./icons.js";
import { plural, taskOutcome, taskTitle } from "./tasks.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (s) => (s == null ? "–" : s < 1 ? `${(s * 1000).toFixed(1)} ms` : `${s.toFixed(2)} s`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
for (const el of document.querySelectorAll("[data-icon]")) el.outerHTML = I[el.dataset.icon];

const rt = new AudientRuntime();
const voice = new Voice();
const speech = new LiveSpeech({ lang: /^en/i.test(navigator.language) ? navigator.language : "en-US" });
const perception = new BrowserPerception({ onStatus: setHint, onResult: onPerceptionResult });
// the octopus follows your cursor (creature.js); the agent's state sets its base pace and look
const creature = $("creature");
let octo = { setState() {}, setLevel() {} };              // until the 3D octopus has loaded (or if WebGL is missing)
import("./octopus3d.js").then(({ Octopus3D }) => {
  octo = new Octopus3D(creature);
  octo.mirror($("avatar"));
  octo.setState(creature.dataset.state);
}).catch((err) => { console.warn("3D octopus unavailable:", err); creature.classList.add("no3d"); });
function setCreature(st) {
  if (creature.dataset.state === st) return;
  creature.dataset.state = st;
  octo.setState(st);
}
const setLevel = (v) => octo.setLevel(v);
const bridge = {
  analyzeFrame: async (ref) => { S.perceiving++; schedule(); try { return await perception.analyzeFrame(ref); } finally { S.perceiving--; schedule(); } },
  transcribe: async (ref) => { S.perceiving++; schedule(); try { return await perception.transcribe(ref); } finally { S.perceiving--; schedule(); } },
};

const FRAMES = [["wm3000_e42.png", "Error E-42"], ["wm3000_e20_red.png", "E-20 + red light"], ["wm3000_blue_led.png", "Blue light"], ["wm3000_dark.png", "Too dark"]];
const RUNNING = new Set(["inflight", "retry_pending"]);

let S = fresh();
function fresh() {
  return { trace: [], live: null, pendingTurns: [], responses: [], cancels: [], cancelByCall: new Map(), lastUserT: null,
           perceiving: 0, interruptedUntil: 0, userSpeaking: false, hyp: "", userLine: "", agentLine: "", lastClarify: "",
           media: new Map(), taskSig: "" };
}

// ------------------------------------------------------------------ runtime events
rt.addEventListener("input", (e) => {
  const ev = e.detail;
  S.trace.push({ dir: "in", ...ev });
  if (ev.type === "transcript" || ev.type === "audio") {
    S.lastUserT = ev.t;
    if (ev.end_of_turn !== false) S.pendingTurns.push(ev.t);
  }
  schedule();
});

rt.addEventListener("action", (e) => {
  const a = e.detail;
  S.trace.push({ dir: "out", ...a });
  if (a.type === "speak" || a.type === "clarify" || a.type === "final_response") {
    S.agentLine = a.text;
    voice.speak(a.text);
  }
  if (a.type === "clarify") S.lastClarify = a.text;
  if (a.type === "final_response") S.lastClarify = "";
  if (a.type === "cancel") {
    const lat = S.lastUserT != null ? a.t - S.lastUserT : null;
    S.cancels.push(lat);
    S.cancelByCall.set(a.call_id, lat);
    S.interruptedUntil = performance.now() + 1100;
  }
  const substantive = a.type === "clarify" || a.type === "final_response" || (a.type === "speak" && a.kind !== "filler");
  if (substantive && S.pendingTurns.length) {
    for (const t of S.pendingTurns) S.responses.push(a.t - t);
    S.pendingTurns = [];
  }
  schedule();
});
voice.addEventListener("start", schedule);
voice.addEventListener("end", schedule);

// ------------------------------------------------------------------ user input
function busy() {
  return voice.speaking || (S.live?.calls || []).some((c) => RUNNING.has(c.status));
}

function bargeIn() {
  const wasBusy = busy();
  if (voice.speaking) voice.cancel();
  if (wasBusy && rt.session) {
    rt.push({ type: "interruption" });
    S.interruptedUntil = performance.now() + 900;
  }
}

function say(text) {
  text = text.trim();
  if (!text || !rt.session) return;
  bargeIn();
  rt.push({ type: "transcript", text, end_of_turn: true, hypothesis: true });
  S.userSpeaking = false;
  S.hyp = "";
  S.userLine = text;
  schedule();
}

function partial(text) {
  if (!rt.session) return;
  if (!S.userSpeaking && text) bargeIn();
  S.userSpeaking = !!text;
  S.hyp = text;
  rt.push({ type: "transcript", text, end_of_turn: false, hypothesis: true });
  schedule();
}

$("composer").addEventListener("submit", (e) => {
  e.preventDefault();
  const v = $("sayInput").value;
  $("sayInput").value = "";
  say(v);
});

// ---- microphone: the browser's live recognition where available, otherwise an on-device Whisper clip
let meter = null;
async function startMeter(stream) {
  try {
    const ctx = new AudioContext();
    const an = ctx.createAnalyser();
    an.fftSize = 512;
    ctx.createMediaStreamSource(stream).connect(an);
    const buf = new Float32Array(an.fftSize);
    let on = true;
    const tick = () => {
      if (!on) return;
      an.getFloatTimeDomainData(buf);
      let s = 0;
      for (const v of buf) s += v * v;
      setLevel(Math.sqrt(s / buf.length) * 7);
      requestAnimationFrame(tick);
    };
    tick();
    meter = { stop() { on = false; ctx.close(); setLevel(0); stream.getTracks().forEach((t) => t.stop()); } };
  } catch { stream.getTracks().forEach((t) => t.stop()); }
}
function stopMeter() { if (meter) { meter.stop(); meter = null; } }

$("micBtn").addEventListener("click", async () => {
  if (LiveSpeech.supported()) {
    if (speech.active) { speech.stop(); return; }
    speech.start();
    navigator.mediaDevices?.getUserMedia({ audio: true }).then(startMeter).catch(() => {});
  } else {
    toggleRecording();
  }
});
speech.addEventListener("started", () => { $("micBtn").setAttribute("aria-pressed", "true"); setHint("mic", "Listening — talk over the agent any time to interrupt it."); schedule(); });
speech.addEventListener("stopped", () => { $("micBtn").setAttribute("aria-pressed", "false"); stopMeter(); S.userSpeaking = false; S.hyp = ""; setHint("mic", ""); schedule(); });
speech.addEventListener("error", (e) => setHint("mic", `Microphone: ${e.detail.error.replaceAll("-", " ")}`));
speech.addEventListener("hypothesis", (e) => {
  const { text, final } = e.detail;
  if (voice.speaking && voice.isEcho(text)) return; // the agent hearing itself through the speakers
  if (final) say(text);
  else partial(text);
});

let recorder = null;
async function toggleRecording() {
  if (recorder) { recorder.stop(); return; }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch {
    setHint("mic", "Microphone permission was denied.");
    return;
  }
  perception.preloadAsr();
  bargeIn();
  const chunks = [];
  recorder = new MediaRecorder(stream);
  recorder.ondataavailable = (ev) => chunks.push(ev.data);
  recorder.onstop = () => {
    const blob = new Blob(chunks, { type: recorder.mimeType });
    recorder = null;
    stopMeter();
    $("micBtn").classList.remove("recording");
    $("micBtn").innerHTML = I.mic;
    $("micBtn").setAttribute("aria-pressed", "false");
    sendAudio(blob, "a voice clip");
  };
  recorder.start();
  startMeter(stream.clone());
  $("micBtn").classList.add("recording");
  $("micBtn").innerHTML = I.stop;
  $("micBtn").setAttribute("aria-pressed", "true");
  setHint("mic", "Recording — tap again to send. Transcribed on this device.");
  schedule();
}

function sendAudio(blob, label) {
  if (!rt.session) return;
  const ref = perception.register(blob, "audio");
  bargeIn();
  S.media.set(ref, label);
  S.userLine = `Sent ${label} — transcribing on this device…`;
  rt.push({ type: "audio", path: ref, end_of_turn: true });
  schedule();
}

// ---- attachments: camera frames and voice clips
for (const [file, label] of FRAMES) {
  const b = document.createElement("button");
  b.type = "button";
  b.innerHTML = `<img src="frames/${file}" alt=""><small>${esc(label)}</small>`;
  b.addEventListener("click", () => sendSampleFrame(file, label));
  $("frameGrid").appendChild(b);
}
function toggleMenu(open = $("attachMenu").hidden) {
  $("attachMenu").hidden = !open;
  $("attachBtn").setAttribute("aria-expanded", String(open));
}
$("attachBtn").addEventListener("click", () => toggleMenu());
document.addEventListener("click", (e) => {
  if (!$("attachMenu").hidden && !e.target.closest("#attachMenu, #attachBtn")) toggleMenu(false);
});
$("imageFile").addEventListener("change", (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (f) sendFrame(f, "a photo");
});
$("audioFile").addEventListener("change", (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  toggleMenu(false);
  if (f) { perception.preloadAsr(); sendAudio(f, "a voice clip"); }
});

async function sendSampleFrame(file, label) {
  const blob = await (await fetch(`frames/${file}`)).blob();
  sendFrame(blob, `a camera frame (${label})`);
}

function sendFrame(blob, label) {
  if (!rt.session) return;
  const ref = perception.register(blob, "frame");
  S.media.set(ref, label);
  S.userLine = `Showed ${label}`;
  rt.push({ type: "frame", path: ref });
  toggleMenu(false);
  schedule();
}

function onPerceptionResult(kind, ref, res) {
  if (!S.media.has(ref)) return;
  if (kind === "frame") {
    const parts = [res.model && `model ${res.model}`, res.code && `error ${res.code}`, res.indicator && `${res.indicator.replace("_", " ")} light`].filter(Boolean);
    setHint("vision", res.ambiguous && !parts.length ? `Couldn't read the frame — ${res.ambiguous.replaceAll("_", " ")}` : `Seen in the frame: ${parts.join(" · ") || "nothing readable"}`);
  } else {
    S.userLine = res.text || "(couldn't make out the clip)";
    schedule();
  }
}

let hintTimer = 0;
function setHint(kind, text) {
  clearTimeout(hintTimer);
  $("hint").textContent = /ready|analysed/i.test(text) ? "" : text;
  if (text && !/^(Listening|Recording)/.test(text)) hintTimer = setTimeout(() => ($("hint").textContent = ""), 6000);
}

// ------------------------------------------------------------------ rendering
let raf = 0;
function schedule() {
  if (!raf) raf = requestAnimationFrame(() => { raf = 0; render(); });
}

function agentState() {
  if (!rt.session) return "connecting";
  if (performance.now() < S.interruptedUntil) return "interrupted";
  if (voice.speaking) return "speaking";
  if (S.userSpeaking || S.live?.holding_floor || recorder) return "listening";
  if ((S.live?.calls || []).some((c) => RUNNING.has(c.status)) || S.perceiving > 0) return "thinking";
  if (speech.active) return "listening";
  return "idle";
}
const TITLES = { connecting: "Waking up", idle: "Ready when you are", listening: "Listening", thinking: "Working on it", speaking: "Speaking", interrupted: "Changing course" };
const IDLE_LINE = "Ask for something, then change your mind while it's working.";

function render() {
  if (rt.session) S.live = rt.liveState();
  const st = agentState();
  setCreature(st);
  $("stateLabel").textContent = TITLES[st];
  const userText = S.hyp || S.userLine;
  $("lineUser").hidden = !userText;
  $("lineUser").classList.toggle("live", !!S.hyp);
  $("lineUserText").textContent = S.hyp ? `“${S.hyp}”` : userText;
  $("lineAgentText").textContent = st === "connecting" ? "Loading the agent into your browser…" : S.agentLine || IDLE_LINE;
  renderTasks();
  renderTools();
  renderStats();
  renderSnapshot();
}

// ---- tasks: every tool call, in plain language
const EMPTY_TASKS = $("tasks").innerHTML;
const PILL = {
  inflight: ["blue", I.ring, "In progress"], retry_pending: ["amber", I.clock, "Retrying"],
  done: ["green", I.check, "Completed"], cancelled: ["red", I.x, "Cancelled"], failed: ["red", I.alert, "Failed"],
};
function renderTasks() {
  const calls = (S.live?.calls || []).slice().reverse();
  const waiting = S.live?.snapshot?.status === "clarifying" && S.lastClarify;
  const now = rt.session ? rt.now() : 0;
  const sig = JSON.stringify([calls.map((c) => [c.call_id, c.status, RUNNING.has(c.status) ? Math.floor((now - c.t0) * 2) : 0]), waiting]);
  if (sig === S.taskSig) return;
  S.taskSig = sig;
  const box = $("tasks");
  if (!calls.length && !waiting) {
    box.innerHTML = EMPTY_TASKS;
    return;
  }
  box.querySelector(".tasks-empty")?.remove();
  // rows are keyed and updated in place, so only genuinely new tasks animate in
  const rows = [];
  if (waiting) {
    rows.push(["clarify", "task", `<span class="task-ic">${I.alert}</span><div><b>${esc(S.lastClarify)}</b><small>Answer by typing or talking</small></div><span class="pill amber">${I.alert}Needs you</span>`, ""]);
  }
  for (const c of calls) {
    const [name, bits] = taskTitle(c);
    const [col, icon, label] = PILL[c.status] || ["", I.clock, c.status];
    let sub = bits.filter(Boolean).join(" · ");
    if (c.speculative) sub = `Started early · ${sub}`;
    if (RUNNING.has(c.status)) sub += ` · ${fmt(now - c.t0)}`;
    if (c.status === "done") sub = taskOutcome(c.result) || sub;
    if (c.status === "cancelled") {
      const lat = S.cancelByCall.get(c.call_id);
      sub = `You changed it${lat != null ? ` · stopped in ${fmt(lat)}` : ""}`;
    }
    rows.push([c.call_id, `task ${c.status}`, `<span class="task-ic">${toolIcon(c.tool)}</span><div><b>${esc(name)}</b><small>${esc(sub)}</small></div>` +
      `<span class="pill ${col}">${icon}${label}</span>`, `${c.call_id} ${c.tool}(${JSON.stringify(c.args)})`]);
  }
  const keep = new Set(rows.map((r) => r[0]));
  for (const li of [...box.children]) if (!keep.has(li.dataset.key)) li.remove();
  rows.forEach(([key, cls, html, tip], i) => {
    let li = box.querySelector(`li[data-key="${CSS.escape(key)}"]`);
    if (!li) { li = document.createElement("li"); li.dataset.key = key; li.classList.add("fresh"); }
    if (li.innerHTML !== html) li.innerHTML = html;
    li.className = cls + (li.classList.contains("fresh") ? " fresh" : "");
    li.title = tip;
    if (box.children[i] !== li) box.insertBefore(li, box.children[i] || null);
    setTimeout(() => li.classList.remove("fresh"), 300);
  });
}

let toolsDrawn = false;
function renderTools() {
  const box = $("toolIcons");
  if (!toolsDrawn && rt.manifest.length) {
    box.innerHTML = rt.manifest.map((t) => {
      const writes = /state_modifying|write|mutating/.test(t.side_effect || "");
      return `<span class="tool-icon${writes ? " writes" : ""}" data-tool="${esc(t.name)}" title="${esc(t.name)} — ${esc(t.description || "")}${writes ? " (changes something; done exactly once)" : ""}">${toolIcon(t.name)}</span>`;
    }).join("");
    toolsDrawn = true;
  }
  const running = new Set((S.live?.calls || []).filter((c) => RUNNING.has(c.status)).map((c) => c.tool));
  for (const el of box.children) el.classList.toggle("active", running.has(el.dataset.tool));
}

function median(xs) {
  const v = xs.filter((x) => x != null).sort((a, b) => a - b);
  return v.length ? v[v.length >> 1] : null;
}
function renderStats() {
  const r = S.responses.filter((x) => x != null);
  $("sResp").textContent = r.length ? fmt(r[r.length - 1]) : "–";
  $("sRespSub").textContent = r.length > 1 ? `median ${fmt(median(r))}` : "first spoken reply";
  const c = S.cancels.filter((x) => x != null);
  $("sCancel").textContent = c.length ? fmt(c[c.length - 1]) : "–";
  $("sCancelSub").textContent = c.length ? `${plural(c.length, "interruption")} handled` : "to drop stale work";
  const calls = S.live?.calls || [];
  const done = calls.filter((x) => x.status === "done").length;
  const cancelled = calls.filter((x) => x.status === "cancelled").length;
  $("sTasks").textContent = done;
  $("sTasksSub").textContent = calls.length ? `of ${calls.length}${cancelled ? ` · ${cancelled} cancelled` : ""}` : "completed";
  const commits = S.live?.commits || [];
  const keys = commits.map((x) => x.tool + JSON.stringify(x.args));
  const dups = keys.length - new Set(keys).size;
  $("sWrites").textContent = plural(dups, "duplicate");
  $("sWritesSub").textContent = commits.length ? `${plural(commits.length, "change")} made, each once` : "nothing changed yet";
  const pts = r.slice(-16).map((x) => x * 1000);
  const svg = $("spark");
  if (pts.length < 2) {
    // placeholder until there are two replies to plot
    svg.innerHTML = `<path d="M0,50 C20,50 25,38 45,40 S75,52 90,44 S110,36 120,38" fill="none" stroke="rgba(255,255,255,.14)" stroke-width="1.2" stroke-dasharray="3 4" vector-effect="non-scaling-stroke"/>`;
    return;
  }
  const max = Math.max(...pts) * 1.15 || 1;
  const xy = pts.map((v, i) => [(i / (pts.length - 1)) * 120, 60 - (v / max) * 52]);
  const line = xy.map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  svg.innerHTML = `<defs><linearGradient id="sg" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ff5fb4" stop-opacity=".55"/><stop offset="1" stop-color="#ff5fb4" stop-opacity="0"/></linearGradient></defs>` +
    `<path d="${line} L120,64 L0,64 Z" fill="url(#sg)"/><path d="${line}" fill="none" stroke="#ff7cc0" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
}

let prevSlots = {};
function renderSnapshot() {
  const snap = S.live?.snapshot;
  const box = $("snapshot");
  if (!snap || (!snap.intent && !Object.keys(snap.slots || {}).length)) {
    if (!box.querySelector(".snap-empty")) box.innerHTML = `<span class="snap-empty">Intent and slot values appear here as you talk.</span>`;
    $("snapMeta").textContent = "";
    return;
  }
  const chips = [`<span class="chip intent"><b>intent </b>${esc(snap.intent || "—")}</span>`, `<span class="chip"><b>status </b>${esc(snap.status)}</span>`];
  for (const [k, v] of Object.entries(snap.slots || {})) {
    const fresh = prevSlots[k] !== undefined && prevSlots[k] !== v;
    chips.push(`<span class="chip${fresh ? " fresh" : ""}"><b>${esc(k)}=</b>${esc(v)}</span>`);
  }
  for (const [k, v] of Object.entries(snap.tentative_slots || {})) chips.push(`<span class="chip tentative" title="heard but not final yet"><b>${esc(k)}≈</b>${esc(v)}</span>`);
  const html = chips.join("");
  if (box.dataset.html !== html) { box.innerHTML = html; box.dataset.html = html; prevSlots = { ...(snap.slots || {}) }; }
  $("snapMeta").textContent = `version ${snap.version}`;
}

// ------------------------------------------------------------------ session lifecycle
async function newSession() {
  voice.cancel();
  await rt.start(bridge);
  S = fresh();
  schedule();
}
setInterval(() => { if (rt.session && document.visibilityState === "visible") schedule(); }, 200);

// ------------------------------------------------------------------ guided demos (the first four get chips)
const callRunning = (tool) => () => (S.live?.calls || []).some((c) => c.tool === tool && RUNNING.has(c.status));
const DEMOS = [
  { title: "Change a booking mid-way", desc: "Book for 2, then say “make it 3” while it's booking", steps: [
    { say: "Book the cheapest flight from Pune to Chennai tomorrow for 2 passengers" },
    { until: callRunning("book_flight"), timeout: 9000 }, { wait: 500 },
    { say: "wait, make it 3 passengers" }] },
  { title: "Camera + spoken correction", desc: "Error E-20 on camera, then “it's blinking blue now”", steps: [
    { frame: "wm3000_e20_red.png", label: "E-20 + red light" }, { wait: 500 },
    { say: "My washer shows error E-20, what should I do?" },
    { until: callRunning("lookup_manual"), timeout: 20000 }, { wait: 250 },
    { frame: "wm3000_blue_led.png", label: "Blue light" },
    { say: "wait, no, the red light stopped, now it's blinking blue twice" }] },
  { title: "Pause mid-sentence", desc: "“…to, um…” — it waits instead of cutting in", steps: [
    { say: "I want to fly from Mumbai to, um..." }, { wait: 600 }, { say: "Chennai on Sunday" }] },
  { title: "Change destination while driving", desc: "“Navigate to the office… no wait, the airport”", steps: [
    { say: "Navigate to the office" }, { until: callRunning("get_route"), timeout: 5000 }, { wait: 300 },
    { say: "no wait, take me to the airport instead" }] },
  { title: "Start before I finish", steps: [
    { partial: "find flights" }, { wait: 300 }, { partial: "find flights from Mumbai to Delhi" }, { wait: 300 },
    { partial: "find flights from Mumbai to Delhi tomorrow" }, { wait: 900 },
    { say: "find flights from Mumbai to Delhi tomorrow in the evening" }] },
  { title: "A tool it's never seen", steps: [
    { say: "Reserve a table for four at Olive Garden at 8 pm tonight" }] },
];

let demoRunning = false;
const chips = [];
async function until(pred, timeout) {
  const t0 = performance.now();
  while (!pred() && performance.now() - t0 < timeout) { await sleep(40); S.live = rt.liveState(); }
}
async function runDemo(i) {
  const demo = DEMOS[i], chip = chips[i];
  if (demoRunning || !rt.session) return;
  demoRunning = true;
  for (const b of chips) b.disabled = true;
  if (chip) { chip.classList.add("running"); chip.querySelector(".i").outerHTML = I.ring; }
  try {
    await newSession();
    for (const st of demo.steps) {
      if (st.wait) await sleep(st.wait);
      else if (st.until) await until(st.until, st.timeout);
      else if (st.say) say(st.say);
      else if (st.partial) partial(st.partial);
      else if (st.frame) await sendSampleFrame(st.frame, st.label);
    }
  } finally {
    demoRunning = false;
    if (chip) { chip.classList.remove("running"); chip.querySelector(".i").outerHTML = I.play; }
    for (const b of chips) b.disabled = false;
  }
}
DEMOS.slice(0, 4).forEach((d, i) => {
  const b = document.createElement("button");
  b.className = "demo";
  b.innerHTML = `<span class="play">${I.play}</span><span><b>${esc(d.title)}</b><small>${esc(d.desc)}</small></span>`;
  b.addEventListener("click", () => runDemo(i));
  $("demos").appendChild(b);
  chips.push(b);
});

// ------------------------------------------------------------------ boot
function status(text, cls) {
  $("status").className = `status ${cls}`;
  $("status").querySelector("span").textContent = text;
}
(async () => {
  try {
    await rt.load((t) => { $("loaderText").textContent = t; status(t, "wait"); });
    await newSession();
    $("loader").hidden = true;
    status("Agent online", "ok");
    setTimeout(() => perception.preloadOcr(), 2500); // fetch the OCR model in the background so the first frame is fast
    window.audient = { rt, S: () => S, say, partial, sendSampleFrame, runDemo, newSession, voice, octo: () => octo };
  } catch (err) {
    console.error(err);
    $("loaderText").textContent = `Couldn't start the agent: ${err.message}. Check your connection and reload.`;
    status("Failed to start", "bad");
  }
})();
