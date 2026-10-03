// On-device speech recognition (Whisper tiny.en via Transformers.js) in a Web Worker, so the
// page (and the agent's fast path) never blocks while a clip is transcribed.
const TRANSFORMERS_URL = "https://cdn.jsdelivr.net/npm/@huggingface/transformers@4.3.0/dist/transformers.min.js";
const MODEL = "Xenova/whisper-tiny.en";

let transcriber = null;

async function load() {
  if (transcriber) return transcriber;
  const { pipeline } = await import(TRANSFORMERS_URL);
  self.postMessage({ type: "status", text: "Downloading Whisper tiny.en (first use only)…" });
  let lastPct = -1;
  transcriber = await pipeline("automatic-speech-recognition", MODEL, {
    progress_callback: (p) => {
      if (p.status === "progress" && p.total > 1e6) {
        const pct = Math.floor(p.progress / 10) * 10;
        if (pct !== lastPct) { lastPct = pct; self.postMessage({ type: "status", text: `Downloading Whisper… ${pct}%` }); }
      }
    },
  });
  self.postMessage({ type: "status", text: "Whisper ready (runs on this device)" });
  return transcriber;
}

self.onmessage = async (e) => {
  const { id, audio, preload } = e.data;
  try {
    const asr = await load();
    if (preload) return;
    self.postMessage({ type: "status", text: "Transcribing…" });
    const out = await asr(audio);
    self.postMessage({ type: "result", id, text: out.text || "" });
    self.postMessage({ type: "status", text: "Whisper ready (runs on this device)" });
  } catch (err) {
    self.postMessage({ type: "result", id, error: String(err && err.message ? err.message : err) });
  }
};
