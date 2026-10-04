import { AudientRuntime } from "./runtime.js";
import { BrowserPerception } from "./perception.js";
import { BargeIn, LiveSpeech, Voice, speakable } from "./speech.js";
import { LIVE_TOOLS } from "./livetools.js";
import { I, toolIcon } from "./icons.js";
import { taskOutcome, taskTitle } from "./tasks.js";
import { Amoeba } from "./amoeba.js";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (s) => (s == null ? "–" : s < 1 ? `${(s * 1000).toFixed(1)} ms` : `${s.toFixed(2)} s`);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
for (const el of document.querySelectorAll("[data-icon]")) el.outerHTML = I[el.dataset.icon];

const rt = new AudientRuntime();
const voice = new Voice();
const speech = new LiveSpeech({ lang: /^en/i.test(navigator.language) ? navigator.language : "en-US" });
const perception = new BrowserPerception({ onStatus: setHint, onResult: onPerceptionResult });
// the puffer fish (3D) reacts to your voice, your cursor and the agent's state; without WebGL the
// 2D amoeba stands in
const creature = $("creature");
let blob = { setState() {}, setLevel() {} };
import("./listener.js").then(({ Listener }) => {
  blob = new Listener(creature);
  blob.setState(creature.dataset.state);
}).catch((err) => {
  console.warn("3D listener unavailable, using the 2D amoeba:", err);
  blob = new Amoeba(creature);
  blob.setState(creature.dataset.state);
});
function setCreature(st) {
  if (creature.dataset.state === st) return;
  creature.dataset.state = st;
  blob.setState(st);
}
const setLevel = (v) => blob.setLevel(v);
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

// Speak the gist and leave long lists on screen: a 15-second spoken list leaves no room to reply.
function forSpeech(text) {
  const i = text.indexOf(" Others:");
  if (i > 0) text = text.slice(0, i);
  const m = text.match(/^(I found \d+ [^:]+):/);
  return m ? `${m[1]}. The options are on screen.` : text;
}

rt.addEventListener("action", (e) => {
  const a = e.detail;
  S.trace.push({ dir: "out", ...a });
  if (a.type === "speak" || a.type === "clarify" || a.type === "final_response") {
    S.agentLine = a.text;
    voice.speak(forSpeech(a.text));
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

// the big button switches hands-free listening on and off; talking over the agent interrupts it
$("interruptBtn").addEventListener("click", () => toggleMic());
function tick() {
  const d = new Date();
  $("clock").textContent = `${d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })} | ${d.toLocaleDateString([], { month: "short", day: "numeric" })}`;
}
tick();
setInterval(tick, 15000);

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
      const lvl = Math.min(1, Math.sqrt(s / buf.length) * 7);
      setLevel(lvl);
      S.level = lvl;
      $("interruptBtn").style.setProperty("--lvl", lvl.toFixed(3));
      onVoiceLevel(lvl);
      requestAnimationFrame(tick);
    };
    tick();
    meter = { stop() { on = false; ctx.close(); setLevel(0); S.level = 0; stream.getTracks().forEach((t) => t.stop()); } };
  } catch { stream.getTracks().forEach((t) => t.stop()); }
}
function stopMeter() { if (meter) { meter.stop(); meter = null; } }

// Hands-free: once listening is on it stays on, and simply talking interrupts the agent. The mic level
// drives a barge-in detector: when the user starts speaking over the agent, its voice pauses at once;
// recognised words then cut it off (and cancel stale work), while noise or silence lets it carry on.
const barge = new BargeIn();
const RESUME_AFTER_MS = 1300;
let resumeTimer = 0;
function onVoiceLevel(lvl) {
  if (barge.feed(lvl, voice.speaking && !voice.paused, performance.now()) !== "pause") return;
  voice.pause();
  S.heldAt = performance.now();
  clearTimeout(resumeTimer);
  resumeTimer = setTimeout(() => { if (voice.paused) voice.resume(); }, RESUME_AFTER_MS);
  schedule();
}
function listening() { return LiveSpeech.supported() ? speech.active : !!recorder; }
function toggleMic(on = !listening()) {
  if (!on) {
    if (LiveSpeech.supported()) speech.stop(); else if (recorder) toggleRecording();
    try { localStorage.removeItem("audient.handsfree"); } catch { /* storage blocked */ }
    return;
  }
  if (LiveSpeech.supported()) {
    if (!speech.active) {
      speech.start();
      navigator.mediaDevices?.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }).then(startMeter).catch(() => {});
    }
    try { localStorage.setItem("audient.handsfree", "1"); } catch { /* storage blocked */ }
  } else if (!recorder) {
    toggleRecording();
  }
}
speech.addEventListener("started", () => { setHint("mic", "Listening"); schedule(); });
speech.addEventListener("stopped", () => { stopMeter(); S.userSpeaking = false; S.hyp = ""; setHint("mic", ""); schedule(); });
speech.addEventListener("error", (e) => setHint("mic", `Microphone: ${e.detail.error.replaceAll("-", " ")}`));
// Turn-taking on the mic. The browser marks a phrase "final" after a short silence, which is often just
// a pause for thought, so a final phrase is held briefly and merged with whatever the user says next;
// the request goes to the agent only once they have really stopped. Noise is dropped.
const END_OF_TURN_MS = 800;
const STOP_WORDS = new Set(["stop", "wait", "no", "hold", "cancel", "pause", "hey", "sorry", "actually", "enough"]);
const FILLERS = new Set(["uh", "um", "umm", "hmm", "mm", "mhm", "ah", "oh", "er", "erm", "eh", "huh"]);
let heard = "", turnTimer = 0;
function isNoise(text, final, confidence) {
  const words = text.toLowerCase().match(/[a-z0-9']+/g) || [];
  if (!words.length || words.every((w) => FILLERS.has(w))) return true;
  if (final && confidence > 0 && confidence < 0.4) return true;     // the recogniser itself isn't sure
  return voice.speaking && words.length < 2 && !STOP_WORDS.has(words[0]); // a stray word over the agent's voice
}
function endTurn() {
  clearTimeout(turnTimer);
  const t = heard.trim();
  heard = "";
  if (t) say(t);
}
speech.addEventListener("hypothesis", (e) => {
  const { text, final, confidence } = e.detail;
  if (voice.isEcho(text) || isNoise(text, final, confidence)) return; // its own voice, or not speech at all
  clearTimeout(turnTimer);
  if (final) {
    heard = `${heard} ${text}`.trim();
    partial(heard);
    turnTimer = setTimeout(endTurn, END_OF_TURN_MS);
  } else {
    partial(`${heard} ${text}`.trim());
  }
});
speech.addEventListener("stopped", endTurn);

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
    $("interruptBtn").classList.remove("recording");
    sendAudio(blob, "a voice clip");
  };
  recorder.start();
  startMeter(stream.clone());
  $("interruptBtn").classList.add("recording");
  setHint("mic", "Recording. Tap again to send.");
  schedule();
}

function sendAudio(blob, label) {
  if (!rt.session) return;
  const ref = perception.register(blob, "audio");
  bargeIn();
  S.media.set(ref, label);
  S.userLine = `Sent ${label}, transcribing…`;
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
    setHint("vision", res.ambiguous && !parts.length ? `Couldn't read the frame: ${res.ambiguous.replaceAll("_", " ")}` : `Seen in the frame: ${parts.join(" · ") || "nothing readable"}`);
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
const IDLE_LINE = "Hello, I'm Audient. How may I assist you today?";

function render() {
  if (rt.session) S.live = rt.liveState();
  const st = agentState();
  setCreature(st);
  const userText = S.hyp || S.userLine;
  $("lineUser").hidden = !userText;
  $("lineUser").classList.toggle("live", !!S.hyp);
  $("lineUserText").textContent = S.hyp ? `“${S.hyp}”` : userText;
  $("lineAgentText").textContent = st === "connecting" ? "Loading…" : S.agentLine || IDLE_LINE;
  // the big button interrupts while the agent talks or works; otherwise it starts listening
  const on = listening();
  $("interruptBtn").classList.toggle("live", on);
  $("interruptBtn").setAttribute("aria-pressed", String(on));
  $("interruptLabel").textContent = recorder ? "Tap to send" : !on ? "Tap to talk" : voice.paused ? "Go ahead" : st === "speaking" ? "Talk to interrupt" : "Listening";
  renderTasks();
}

// ---- tasks: every tool call, in plain language, in a panel opened from the sidebar
const EMPTY_TASKS = $("tasks").innerHTML;
function showTasks(open) {
  $("tasksPanel").classList.toggle("open", open);
  $("tasksToggle").setAttribute("aria-expanded", String(open));
  try { localStorage.setItem("audient.tasks", open ? "1" : ""); } catch { /* storage blocked */ }
}
$("tasksToggle").addEventListener("click", () => showTasks(!$("tasksPanel").classList.contains("open")));
$("tasksClose").addEventListener("click", () => { showTasks(false); $("tasksToggle").focus(); });
addEventListener("keydown", (e) => { if (e.key === "Escape" && $("tasksPanel").classList.contains("open")) showTasks(false); });
try { if (localStorage.getItem("audient.tasks")) showTasks(true); } catch { /* storage blocked */ }
const PILL = {
  inflight: ["blue", I.ring, "In progress"], retry_pending: ["amber", I.clock, "Retrying"],
  done: ["green", I.check, "Completed"], cancelled: ["red", I.x, "Cancelled"], failed: ["red", I.alert, "Failed"],
};
function renderTasks() {
  // a search started early from half a sentence is only shown once the sentence confirms it
  const calls = (S.live?.calls || []).filter((c) => !(c.speculative && (c.status === "cancelled" || S.userSpeaking))).reverse();
  const waiting = S.live?.snapshot?.status === "clarifying" && S.lastClarify;
  const now = rt.session ? rt.now() : 0;
  const sig = JSON.stringify([calls.map((c) => [c.call_id, c.status, RUNNING.has(c.status) ? Math.floor((now - c.t0) * 2) : 0]), waiting]);
  if (sig === S.taskSig) return;
  S.taskSig = sig;
  // the sidebar badge: how many tasks, beating while one runs, amber when the agent needs an answer
  const badge = $("tasksBadge");
  badge.hidden = !calls.length && !waiting;
  badge.textContent = String(calls.length + (waiting ? 1 : 0));
  badge.classList.toggle("busy", calls.some((c) => RUNNING.has(c.status)));
  badge.classList.toggle("needs", !!waiting);
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

// ------------------------------------------------------------------ session lifecycle
// The language model (if the server has one configured) understands the user; otherwise the rule parser does.
const model = { on: false, name: null };
async function understand(ctx) {
  const r = await fetch("api/understand", { method: "POST", headers: { "Content-Type": "application/json" }, body: ctx });
  const j = await r.json();
  if (!r.ok || !j.result) throw new Error(j.error || `HTTP ${r.status}`);
  return JSON.stringify(j.result);
}
async function detectModel() {
  try {
    const r = await fetch("api/understand", { cache: "no-store" });
    const j = r.ok ? await r.json() : null;
    model.on = !!j?.configured;
    model.name = j?.model || null;
  } catch { model.on = false; }
}

async function newSession() {
  voice.cancel();
  await rt.start(bridge, model.on ? understand : null, LIVE_TOOLS);
  S = fresh();
  schedule();
}
setInterval(() => { if (rt.session && document.visibilityState === "visible") schedule(); }, 200);

// ------------------------------------------------------------------ guided demos (the first four get chips)
const callRunning = (tool) => () => (S.live?.calls || []).some((c) => c.tool === tool && RUNNING.has(c.status));
const DEMOS = [
  { title: "Change a booking mid-way", desc: "Book for 2, then “make it 3”", steps: [
    { say: "Book the cheapest flight from Pune to Chennai tomorrow for 2 passengers" },
    { until: callRunning("book_flight"), timeout: 9000 }, { wait: 500 },
    { say: "wait, make it 3 passengers" }] },
  { title: "Camera + spoken correction", desc: "E-20 on camera, then “blinking blue”", steps: [
    { frame: "wm3000_e20_red.png", label: "E-20 + red light" }, { wait: 500 },
    { say: "My washer shows error E-20, what should I do?" },
    { until: callRunning("lookup_manual"), timeout: 20000 }, { wait: 250 },
    { frame: "wm3000_blue_led.png", label: "Blue light" },
    { say: "wait, no, the red light stopped, now it's blinking blue twice" }] },
  { title: "Pause mid-sentence", desc: "“…to, um…” and it waits", steps: [
    { say: "I want to fly from Mumbai to, um..." }, { wait: 600 }, { say: "Chennai on Sunday" }] },
  { title: "Change destination while driving", desc: "Office… no wait, the airport", steps: [
    { say: "Navigate to Phoenix Marketcity" }, { until: callRunning("get_route"), timeout: 5000 }, { wait: 150 },
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
    if (chip) { chip.classList.remove("running"); chip.querySelector(".i").outerHTML = I.send; }
    for (const b of chips) b.disabled = false;
  }
}
// The examples only start you off: a click puts the first sentence in the box (and shows the camera
// frame it needs). You send it and do the interrupting yourself; nothing is said on your behalf.
// The scripted runs (DEMOS / runDemo) are kept for the automated browser tests only.
const EXAMPLES = [
  { title: "Change an alarm mid-way", desc: "Then say “make it 6:30”", say: "Set an alarm for 6 am" },
  { title: "Live weather", desc: "Real forecast, then “actually Delhi”", say: "What's the weather in Pune tomorrow?" },
  { title: "Smart home", desc: "Then “actually make it 24”", say: "Set the bedroom AC to 22 degrees" },
  { title: "Change a booking mid-way", desc: "Book for 2, then “make it 3”", say: "Book the cheapest flight from Pune to Chennai tomorrow for 2 passengers" },
  { title: "Camera + spoken correction", desc: "E-20 on camera, then “blinking blue”", say: "My washer shows error E-20, what should I do?",
    frame: ["wm3000_e20_red.png", "E-20 + red light"] },
];
EXAMPLES.forEach((d) => {
  const b = document.createElement("button");
  b.className = "demo";
  b.innerHTML = `<span class="play">${I.send}</span><span><b>${esc(d.title)}</b><small>${esc(d.desc)}</small></span>`;
  b.addEventListener("click", async () => {
    if (d.frame && rt.session) await sendSampleFrame(d.frame[0], d.frame[1]);
    $("sayInput").value = d.say;
    $("sayInput").focus();
  });
  $("demos").appendChild(b);
});

// ------------------------------------------------------------------ boot
(async () => {
  try {
    await Promise.all([rt.load((t) => { $("loaderText").textContent = t; }), detectModel()]);
    await newSession();
    $("loader").hidden = true;
    try { if (localStorage.getItem("audient.handsfree")) toggleMic(true); } catch { /* storage blocked */ }
    setTimeout(() => perception.preloadOcr(), 2500); // fetch the OCR model in the background so the first frame is fast
    window.audient = { rt, S: () => S, say, partial, sendSampleFrame, runDemo, newSession, voice, speech, forSpeech, model, speakable, barge, toggleMic, listener: () => blob, amoeba: () => blob };
  } catch (err) {
    console.error(err);
    $("loaderText").textContent = `Couldn't start the agent: ${err.message}. Check your connection and reload.`;
  }
})();
