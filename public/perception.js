// Browser perception for the live agent: camera frames (OCR + LED + quality) and audio (Whisper).
// Mirrors audient/perception.py so the agent receives the same fields it gets in the benchmark.
const TESSERACT_URL = "https://cdn.jsdelivr.net/npm/tesseract.js@7.0.0/dist/tesseract.esm.min.js";

const CODE_RE = /\b(?:ERR(?:OR)?\s*)?([EF])\s*-?\s*(\d{1,3})\b/g;
const MODEL_RE = /\b([A-Z]{2,}-?\d{3,}[A-Z]?)\b/g;
const HUES = [["red", 0, 15], ["amber", 15, 45], ["yellow", 45, 70], ["green", 70, 170], ["blue", 170, 265], ["red", 330, 361]];

export class BrowserPerception {
  constructor({ onStatus = () => {}, onResult = () => {} } = {}) {
    this.blobs = new Map(); // ref -> Blob (frames and audio clips the user provided)
    this.onStatus = onStatus;
    this.onResult = onResult;
    this._ocr = null;
    this._asrWorker = null;
    this._asrJobs = new Map();
    this._n = 0;
  }

  register(blob, kind) {
    const ref = `${kind}:${++this._n}`;
    this.blobs.set(ref, blob);
    return ref;
  }

  // ------------------------------------------------------------------ vision
  async _ocrWorker() {
    if (!this._ocr) {
      this.onStatus("vision", "Loading OCR model…");
      const Tesseract = (await import(TESSERACT_URL)).default;
      this._ocr = await Tesseract.createWorker("eng");
      // an explicit resolution stops Tesseract estimating one (and logging it as an error) on every frame
      await this._ocr.setParameters({ user_defined_dpi: "150" });
      this.onStatus("vision", "OCR ready");
    }
    return this._ocr;
  }

  async analyzeFrame(ref) {
    const blob = this.blobs.get(ref);
    if (!blob) return JSON.stringify({ ambiguous: "frame_missing" });
    const bitmap = await createImageBitmap(blob);
    const scale = Math.min(1, 800 / Math.max(bitmap.width, bitmap.height));
    const canvas = new OffscreenCanvas(Math.round(bitmap.width * scale), Math.round(bitmap.height * scale));
    const ctx = canvas.getContext("2d", { willReadFrequently: true });
    ctx.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    const img = ctx.getImageData(0, 0, canvas.width, canvas.height);
    const out = { brightness: 0, sharpness: 0 };
    const { gray, brightness } = grayscale(img);
    out.brightness = +brightness.toFixed(1);
    out.sharpness = +sharpness(gray, img.width, img.height).toFixed(2);
    if (brightness < 35) return JSON.stringify({ ...out, ambiguous: "image_too_dark" });

    const worker = await this._ocrWorker();
    this.onStatus("vision", "Reading the frame…");
    // Two passes, merged: sparse-text mode finds isolated labels such as a code on a display panel
    // (default layout analysis tends to skip those as "image"), the default mode reads regular text.
    const frameBlob = await canvas.convertToBlob();
    const reads = [];
    for (const psm of ["12", "3"]) {
      await worker.setParameters({ tessedit_pageseg_mode: psm });
      reads.push((await worker.recognize(frameBlob)).data);
    }
    const text = reads.map((d) => d.text || "").join("\n").toUpperCase();
    out.ocr_text = text.replace(/\s+/g, " ").trim();
    out.ocr_confidence = Math.round(Math.max(...reads.map((d) => d.confidence || 0)));
    const codes = [...new Set([...text.matchAll(CODE_RE)].map((m) => `${m[1]}${parseInt(m[2], 10)}`))];
    const models = [...text.matchAll(MODEL_RE)].map((m) => m[1]).filter((m) => !/^[EF]-?\d{1,3}$/.test(m));
    if (codes.length === 1) out.code = codes[0];
    else if (codes.length > 1) out.ambiguous = "multiple_error_codes_visible";
    if (models.length) out.model = models[0];
    const led = ledColour(img);
    if (led) out.indicator = `${led}_solid`;
    if (!out.code && !out.indicator && !out.ambiguous) {
      out.ambiguous = out.sharpness > 4 ? "no_code_or_indicator_visible" : "image_blurry";
    }
    this.onStatus("vision", "Frame analysed");
    this.onResult("frame", ref, out);
    return JSON.stringify(out);
  }

  // ------------------------------------------------------------------ audio
  _worker() {
    if (!this._asrWorker) {
      this._asrWorker = new Worker(new URL("./asr-worker.js", import.meta.url), { type: "module" });
      this._asrWorker.onmessage = (e) => {
        const m = e.data;
        if (m.type === "status") return this.onStatus("asr", m.text);
        const job = this._asrJobs.get(m.id);
        if (!job) return;
        this._asrJobs.delete(m.id);
        m.error ? job.reject(new Error(m.error)) : job.resolve(m.text);
      };
    }
    return this._asrWorker;
  }

  async transcribe(ref) {
    const blob = this.blobs.get(ref);
    if (!blob) return JSON.stringify({ text: "", ambiguous: "audio_missing" });
    let samples;
    try {
      samples = await decodeTo16kMono(await blob.arrayBuffer());
    } catch (err) {
      return JSON.stringify({ text: "", ambiguous: "unreadable_audio" });
    }
    let sum = 0;
    for (const s of samples) sum += s * s;
    const rms = Math.sqrt(sum / Math.max(1, samples.length));
    if (rms < 1e-3) return JSON.stringify({ text: "", ambiguous: "silence" });
    const id = ++this._n;
    const text = await new Promise((resolve, reject) => {
      this._asrJobs.set(id, { resolve, reject });
      this._worker().postMessage({ id, audio: samples }, [samples.buffer]);
    }).catch((err) => { this.onStatus("asr", "Speech model failed: " + err.message); return ""; });
    const clean = (text || "").trim();
    const res = { text: clean, rms: +rms.toFixed(4), ambiguous: clean ? null : "no_speech_recognised" };
    this.onResult("audio", ref, res);
    return JSON.stringify(res);
  }

  preloadOcr() {
    this._ocrWorker().catch(() => {});
  }

  preloadAsr() {
    this._worker().postMessage({ id: 0, preload: true });
  }
}

// ---------------------------------------------------------------------- helpers
function grayscale(img) {
  const { data, width, height } = img;
  const gray = new Float32Array(width * height);
  let total = 0;
  for (let i = 0, p = 0; i < data.length; i += 4, p++) {
    const g = (data[i] + data[i + 1] + data[i + 2]) / 3;
    gray[p] = g;
    total += g;
  }
  return { gray, brightness: total / gray.length };
}

// mean |second difference| along rows and columns (same measure as the Python version)
function sharpness(gray, w, h) {
  let a = 0, na = 0, b = 0, nb = 0;
  for (let y = 0; y + 2 < h; y++) for (let x = 0; x < w; x++) {
    a += Math.abs(gray[(y + 2) * w + x] - 2 * gray[(y + 1) * w + x] + gray[y * w + x]); na++;
  }
  for (let y = 0; y < h; y++) for (let x = 0; x + 2 < w; x++) {
    b += Math.abs(gray[y * w + x + 2] - 2 * gray[y * w + x + 1] + gray[y * w + x]); nb++;
  }
  return a / Math.max(1, na) + b / Math.max(1, nb);
}

function hsv(r, g, b) {
  const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
  let h = 0;
  if (d) {
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
    if (h < 0) h += 360;
  }
  return [h, max ? d / max : 0, max / 255];
}

// Largest bright, saturated, roughly round blob -> colour name (port of Perception._led).
function ledColour(img) {
  const { data, width: w, height: h } = img;
  const mask = new Uint8Array(w * h);
  const hue = new Float32Array(w * h);
  for (let p = 0, i = 0; p < w * h; p++, i += 4) {
    const [hh, s, v] = hsv(data[i], data[i + 1], data[i + 2]);
    if (s > 0.55 && v > 0.75) { mask[p] = 1; hue[p] = hh; }
  }
  const seen = new Uint8Array(w * h);
  let best = null, bestArea = 0;
  const stack = [];
  for (let start = 0; start < w * h; start++) {
    if (!mask[start] || seen[start]) continue;
    let area = 0, minX = w, maxX = 0, minY = h, maxY = 0;
    const hues = [];
    stack.push(start);
    seen[start] = 1;
    while (stack.length) {
      const p = stack.pop();
      const x = p % w, y = (p - x) / w;
      area++; hues.push(hue[p]);
      minX = Math.min(minX, x); maxX = Math.max(maxX, x); minY = Math.min(minY, y); maxY = Math.max(maxY, y);
      for (const q of [p - 1, p + 1, p - w, p + w, p - w - 1, p - w + 1, p + w - 1, p + w + 1]) {
        if (q >= 0 && q < w * h && mask[q] && !seen[q] && Math.abs((q % w) - x) <= 1) { seen[q] = 1; stack.push(q); }
      }
    }
    const bw = maxX - minX + 1, bh = maxY - minY + 1;
    if (area < 40 || area > 0.05 * w * h || !(bw / bh > 0.6 && bw / bh < 1.6) || area / (bw * bh) < 0.55) continue;
    if (area > bestArea) {
      hues.sort((a, b) => a - b);
      const med = hues[hues.length >> 1];
      best = HUES.find(([, lo, hi]) => med >= lo && med < hi)?.[0] || null;
      bestArea = area;
    }
  }
  return best;
}

async function decodeTo16kMono(arrayBuffer) {
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const decoded = await ctx.decodeAudioData(arrayBuffer);
  ctx.close();
  const length = Math.ceil(decoded.duration * 16000);
  const off = new OfflineAudioContext(1, length, 16000);
  const src = off.createBufferSource();
  src.buffer = decoded; // multi-channel input is down-mixed to the single output channel
  src.connect(off.destination);
  src.start();
  const rendered = await off.startRendering();
  return new Float32Array(rendered.getChannelData(0));
}
