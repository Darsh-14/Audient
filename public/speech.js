// Voice in and out for the live session.
//  LiveSpeech: the browser's streaming speech recognition (Web Speech API). Interim results become
//              revisable transcript hypotheses; final results end the utterance. (Chrome/Edge send
//              the audio to their cloud recogniser; recorded clips use on-device Whisper instead.)
//  Voice:      speaks agent replies with the most natural voice the browser has, sentence by sentence,
//              and can be paused (the user started talking) or cut off (barge-in).
//  BargeIn:    watches the mic level while the agent talks and flags when the user starts speaking.

export class LiveSpeech extends EventTarget {
  static supported() {
    return !!(window.SpeechRecognition || window.webkitSpeechRecognition);
  }

  constructor({ lang = "en-US" } = {}) {
    super();
    this.lang = lang;
    this.active = false;
    this.rec = null;
    this.lastInterim = "";
  }

  start() {
    if (this.active) return;
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const rec = new SR();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = this.lang;
    rec.onspeechstart = () => this._emit("speechstart", {});
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i];
        const text = r[0].transcript.trim();
        if (!text) continue;
        if (r.isFinal) {
          this.lastInterim = "";
          this._emit("hypothesis", { text, final: true, confidence: r[0].confidence });
        } else {
          interim += (interim ? " " : "") + text;
        }
      }
      if (interim && interim !== this.lastInterim) {
        this.lastInterim = interim;
        this._emit("hypothesis", { text: interim, final: false });
      }
    };
    rec.onerror = (e) => {
      this._emit("error", { error: e.error });
      if (e.error === "not-allowed" || e.error === "service-not-allowed" || e.error === "audio-capture") this.stop();
    };
    rec.onend = () => {
      // Chrome ends recognition after a silence; keep the session open until the user stops it.
      if (this.active) {
        try { rec.start(); } catch { /* already restarting */ }
      } else {
        this._emit("stopped", {});
      }
    };
    this.rec = rec;
    this.active = true;
    rec.start();
    this._emit("started", {});
  }

  stop() {
    this.active = false;
    if (this.rec) {
      try { this.rec.stop(); } catch { /* not running */ }
    }
  }

  _emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}

// ---------------------------------------------------------------- speaking
// Neural voices first (Edge's "Natural" ones, then Google's), English, Indian English slightly preferred.
function voiceScore(v) {
  if (!/^en/i.test(v.lang)) return -1;
  return (/natural/i.test(v.name) ? 40 : 0) + (/google/i.test(v.name) ? 20 : 0) + (/online/i.test(v.name) ? 10 : 0)
    + (/^en[-_]IN/i.test(v.lang) ? 6 : /^en[-_](US|GB)/i.test(v.lang) ? 4 : 1)
    + (/neerja|aria|jenny|ava|emma|andrew|brian|guy|prabhat/i.test(v.name) ? 3 : 0);
}

const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

// Text written for the screen -> text that sounds right aloud.
export function speakable(text) {
  return String(text)
    .replace(/₹\s?([\d,]+(?:\.\d+)?)/g, "$1 rupees")
    .replace(/\b(20\d\d)-(\d\d)-(\d\d)\b/g, (_, y, m, d) => `${+d} ${MONTHS[+m - 1]}`)
    .replace(/\b([01]?\d|2[0-3]):([0-5]\d)\b/g, (_, h, m) => {
      const hh = +h % 12 || 12;
      return `${hh}${m === "00" ? "" : `:${m}`} ${+h < 12 ? "AM" : "PM"}`;
    })
    .replace(/(\d)\s?°C\b/g, "$1 degrees")
    .replace(/(\d)\s?%/g, "$1 percent")
    .replace(/(\d)\s?km\b/g, "$1 kilometres")
    .replace(/(\d)\s?min\b/g, "$1 minutes")
    .replace(/\s*\(\s*/g, ", ").replace(/\s*\)\s*/g, " ")
    .replace(/:\s/g, ", ")
    .replace(/\s+([,.])/g, "$1").replace(/,\s*,/g, ",").replace(/\s{2,}/g, " ")
    .trim();
}

export class Voice extends EventTarget {
  constructor() {
    super();
    this.enabled = "speechSynthesis" in window;
    this.current = "";
    this.paused = false;
    this._voice = null;
    this._queue = 0;
    if (this.enabled) {
      const pick = () => {
        const vs = speechSynthesis.getVoices().filter((v) => voiceScore(v) >= 0).sort((a, b) => voiceScore(b) - voiceScore(a));
        this._voice = vs[0] || null;
      };
      pick();
      speechSynthesis.onvoiceschanged = pick;
    }
  }

  get voiceName() { return this._voice ? this._voice.name : null; }

  get speaking() {
    return this.enabled && (speechSynthesis.speaking || speechSynthesis.pending || this._queue > 0);
  }

  // one utterance per sentence: shorter utterances start sooner, pause and stop cleanly, and avoid the
  // cut-off some browsers apply to long ones
  speak(text) {
    if (!this.enabled || !this.on || !text) return;
    const said = speakable(text);
    const parts = said.match(/[^.!?]+[.!?]*/g) || [said];
    for (const part of parts.map((p) => p.trim()).filter(Boolean)) {
      const u = new SpeechSynthesisUtterance(part);
      if (this._voice) u.voice = this._voice;
      u.rate = 1;
      u.pitch = 1;
      this._queue++;
      u.onstart = () => { this.current = part; this._emit("start", { text: part }); };
      u.onend = u.onerror = () => {
        this._queue = Math.max(0, this._queue - 1);
        this._remember(part);
        if (!this._queue) { this.current = ""; this.paused = false; this._emit("end", {}); }
      };
      speechSynthesis.speak(u);
    }
  }

  // the user may be starting to talk: hold the voice, and either resume or cut it off
  pause() {
    if (!this.enabled || this.paused || !this.speaking) return;
    speechSynthesis.pause();
    this.paused = true;
    this._emit("pause", {});
  }

  resume() {
    if (!this.enabled || !this.paused) return;
    speechSynthesis.resume();
    this.paused = false;
    this._emit("resume", {});
  }

  cancel() {
    if (!this.enabled) return;
    if (this.current) this._remember(this.current);
    this._queue = 0;
    this.paused = false;
    speechSynthesis.cancel();
    this.current = "";
    this._emit("end", {});
  }

  // The recogniser often delivers its final transcript a moment after the speakers go quiet, so what
  // the agent said stays on the "that was me" list for a few seconds after it finishes.
  _remember(text) {
    const now = performance.now();
    this.recent = (this.recent || []).filter((r) => r.until > now).concat({ text, until: now + 4000 });
  }

  // Is what the microphone heard just the agent's own voice coming back through the speakers?
  isEcho(heard) {
    const now = performance.now();
    const spoken = [this.current, ...(this.recent || []).filter((r) => r.until > now).map((r) => r.text)].filter(Boolean);
    if (!spoken.length) return false;
    const words = (s) => s.toLowerCase().replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter((w) => w.length > 2);
    const h = words(heard);
    if (!h.length) return !!this.current;
    if (h.length === 1 && !this.current) return false; // one word after it finished: the user ("Delhi", "yes")
    // an echo repeats the agent's sentence in order; a reply that reuses some of its words does not
    // ("departing from Chennai" after "Where will you be departing from?")
    const lcs = (a, b) => {
      const dp = Array(b.length + 1).fill(0);
      for (const x of a) { let prev = 0; for (let j = 1; j <= b.length; j++) { const t = dp[j]; dp[j] = x === b[j - 1] ? prev + 1 : Math.max(dp[j], dp[j - 1]); prev = t; } }
      return dp[b.length];
    };
    return spoken.some((s) => lcs(h, words(s)) / h.length >= 0.75);
  }

  _emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
Voice.prototype.on = true;

// ---------------------------------------------------------------- barge-in
// While the agent talks, the mic also hears the agent (echo). The detector learns that echo level and
// reports "pause" when the level jumps well above it for a moment: the user has started speaking.
// The app then holds the voice; recognised words cut it off, silence resumes it.
export class BargeIn {
  constructor({ minLevel = 0.3, ratio = 2.2, holdMs = 180, warmMs = 450 } = {}) {
    Object.assign(this, { minLevel, ratio, holdMs, warmMs });
    this.reset();
  }

  reset() { this.floor = null; this.since = 0; this.start = 0; }

  feed(level, agentSpeaking, now) {
    if (!agentSpeaking) { this.reset(); return null; }
    if (!this.start) this.start = now;
    const loud = this.floor !== null && level > Math.max(this.minLevel, this.floor * this.ratio);
    if (!loud) this.floor = this.floor === null ? level : this.floor + (level - this.floor) * 0.08; // learn the echo
    if (now - this.start < this.warmMs) return null;
    if (!loud) { this.since = 0; return null; }
    if (!this.since) this.since = now;
    if (now - this.since < this.holdMs) return null;
    this.since = 0;
    return "pause";
  }
}
