// Voice in and out for the live session.
//  LiveSpeech: the browser's streaming speech recognition (Web Speech API). Interim results become
//              revisable transcript hypotheses; final results end the utterance. (Chrome/Edge send
//              the audio to their cloud recogniser; recorded clips use on-device Whisper instead.)
//  Voice:      speaks agent replies with speechSynthesis and can be cut off instantly (barge-in).

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

export class Voice extends EventTarget {
  constructor() {
    super();
    this.enabled = "speechSynthesis" in window;
    this.current = "";
    this._voice = null;
    if (this.enabled) {
      const pick = () => {
        const vs = speechSynthesis.getVoices();
        this._voice = vs.find((v) => /en[-_](US|GB|IN)/i.test(v.lang) && /natural|online|google/i.test(v.name))
          || vs.find((v) => /^en/i.test(v.lang)) || null;
      };
      pick();
      speechSynthesis.onvoiceschanged = pick;
    }
  }

  get speaking() {
    return this.enabled && (speechSynthesis.speaking || speechSynthesis.pending);
  }

  speak(text) {
    if (!this.enabled || !this.on || !text) return;
    const u = new SpeechSynthesisUtterance(text);
    if (this._voice) u.voice = this._voice;
    u.rate = 1.05;
    u.onstart = () => { this.current = text; this._emit("start", { text }); };
    u.onend = u.onerror = () => {
      if (!speechSynthesis.speaking && !speechSynthesis.pending) { this.current = ""; this._emit("end", {}); }
    };
    speechSynthesis.speak(u);
  }

  cancel() {
    if (!this.enabled) return;
    speechSynthesis.cancel();
    this.current = "";
    this._emit("end", {});
  }

  // Is what the microphone heard just the agent's own voice coming back through the speakers?
  isEcho(heard) {
    if (!this.current) return false;
    const words = (s) => s.toLowerCase().replace(/[^a-z0-9 ]/g, " ").split(/\s+/).filter((w) => w.length > 2);
    const said = new Set(words(this.current));
    const h = words(heard);
    if (!h.length) return true;
    return h.filter((w) => said.has(w)).length / h.length >= 0.6;
  }

  _emit(type, detail) {
    this.dispatchEvent(new CustomEvent(type, { detail }));
  }
}
Voice.prototype.on = true;
