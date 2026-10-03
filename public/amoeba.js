// A ring-shaped ("zero") amoeba drawn as glowing strands on a 2D canvas.
// It is a soft body: each point of the ring has a spring, neighbours are coupled, so where the cursor
// touches the ring it dents away and the dent travels round and settles. A click sends a ripple.
// The strands drift on smooth random noise (never a loop); the agent's state sets pace, size and glow.

// ---------------------------------------------------------------- smooth 1-D noise (non-repeating)
const perm = new Uint8Array(512);
{ let s = 2027; const p = [...Array(256).keys()]; for (let i = 255; i > 0; i--) { s = (s * 16807) % 2147483647; const j = s % (i + 1); [p[i], p[j]] = [p[j], p[i]]; } for (let i = 0; i < 512; i++) perm[i] = p[i & 255]; }
const grad = (h, x) => ((h & 1) ? -x : x) * (1 + (h >> 1 & 3) * 0.25);
function noise(x) {
  const i = Math.floor(x), f = x - i, u = f * f * f * (f * (f * 6 - 15) + 10);
  const a = grad(perm[i & 255], f), b = grad(perm[(i + 1) & 255], f - 1);
  return (a + (b - a) * u) * 1.6;
}

const K = 144;          // points around the ring
const STRANDS = 30;     // glowing strands across the ring's thickness
const MOODS = {
  connecting: { speed: 0.35, wobble: 0.6, swirl: 0.15, size: 0.94, glow: 0.55 },
  idle: { speed: 0.7, wobble: 1.0, swirl: 0.25, size: 1.0, glow: 0.85 },
  listening: { speed: 0.9, wobble: 1.15, swirl: 0.3, size: 1.04, glow: 1.0 },
  thinking: { speed: 1.7, wobble: 1.1, swirl: 1.1, size: 0.98, glow: 1.0 },
  speaking: { speed: 1.2, wobble: 1.3, swirl: 0.45, size: 1.02, glow: 1.1 },
  interrupted: { speed: 2.4, wobble: 1.8, swirl: 0.2, size: 0.92, glow: 1.25 },
};

export class Amoeba {
  constructor(container) {
    this.el = container;
    this.state = "connecting";
    this.mood = { ...MOODS.connecting };
    this.level = 0;
    this.t = Math.random() * 100;
    this.spin = 0;
    this.flash = 0;
    this.disp = new Float32Array(K);   // radial dent / bulge per point (px), the soft body
    this.vel = new Float32Array(K);
    this.ptr = null;                   // cursor in canvas px, or null
    this.ptrV = 0;                     // cursor speed (px/s)
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
    const c = document.createElement("canvas");
    c.className = "amoeba";
    container.appendChild(c);
    this.canvas = c;
    this.ctx = c.getContext("2d");
    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
    addEventListener("pointermove", (e) => this.pointer(e), { passive: true });
    container.addEventListener("pointerleave", () => { this.ptr = null; });
    container.addEventListener("pointerdown", (e) => this.poke(e));
    this.last = performance.now();
    const loop = (now) => { this.frame(now); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  resize() {
    const dpr = Math.min(2, devicePixelRatio || 1);
    const w = this.el.clientWidth || 1, h = this.el.clientHeight || 1;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.dpr = dpr;
    this.w = w; this.h = h;
    this.R0 = Math.min(w, h) * 0.28;   // mean radius of the ring
    this.T = Math.min(w, h) * 0.16;    // thickness of the ring
  }

  pointer(e) {
    const r = this.el.getBoundingClientRect();
    const x = e.clientX - r.left, y = e.clientY - r.top;
    const now = performance.now();
    if (this.ptr) {
      const dt = Math.max(1, now - this.ptr.at) / 1000;
      this.ptrV = Math.min(3000, Math.hypot(x - this.ptr.x, y - this.ptr.y) / dt);
    }
    this.ptr = { x, y, at: now };
  }

  // a click: a ripple from where it landed (or from a random side if it came from code)
  poke(e) {
    let ang = Math.random() * Math.PI * 2;
    if (e && e.clientX != null) {
      const r = this.el.getBoundingClientRect();
      ang = Math.atan2(e.clientY - r.top - this.h / 2, e.clientX - r.left - this.w / 2);
    }
    for (let k = 0; k < K; k++) {
      const d = angDist((k / K) * Math.PI * 2, ang);
      this.vel[k] -= 520 * Math.exp(-(d * d) / 0.18);
    }
  }

  setState(s) {
    if (!MOODS[s] || s === this.state) return;
    if (s === "interrupted") { this.flash = 1; this.poke(); }
    this.state = s;
  }
  setLevel(v) { this.level = Math.max(0, Math.min(1, v)); }

  // radius of the ring's centre line at point k, before strand offsets (used by the tests too)
  radiusAt(k) { return this.R0 * this.mood.size + this.disp[k]; }

  frame(now) {
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;
    if (document.hidden || this.el.offsetParent === null) return;
    const tgt = MOODS[this.state];
    const km = 1 - Math.exp(-dt * 3);
    for (const key of Object.keys(tgt)) this.mood[key] += (tgt[key] - this.mood[key]) * km;
    const speed = this.reduced ? 0.15 : this.mood.speed * (1 + this.level * 0.9);
    this.t += dt * speed;
    this.spin += dt * this.mood.swirl * (this.reduced ? 0.1 : 1);
    this.flash = Math.max(0, this.flash - dt * 1.6);
    this.ptrV *= Math.exp(-dt * 6);
    this.simulate(dt);
    this.draw();
  }

  // springs back to round, neighbours pull each other (dents travel), the cursor pushes the ring away
  simulate(dt) {
    const { disp, vel } = this;
    const cx = this.w / 2, cy = this.h / 2, R = this.R0 * this.mood.size;
    let fAng = 0, fAmt = 0, sign = 1;
    if (this.ptr) {
      const dx = this.ptr.x - cx, dy = this.ptr.y - cy, d = Math.hypot(dx, dy);
      fAng = Math.atan2(dy, dx);
      const near = Math.exp(-(((d - R) / (R * 0.55)) ** 2));          // how close the cursor is to the ring band
      sign = d < R ? 1 : -1;                                          // inside the hole: bulge out; outside: dent in
      fAmt = near * (2600 + Math.min(this.ptrV, 1500) * 1.6);
    }
    const steps = 2, h = dt / steps;
    for (let s = 0; s < steps; s++) {
      for (let k = 0; k < K; k++) {
        const a = (k / K) * Math.PI * 2;
        const lap = disp[(k + 1) % K] + disp[(k - 1 + K) % K] - 2 * disp[k];
        let f = -38 * disp[k] - 5.5 * vel[k] + 340 * lap;
        if (fAmt) {
          const da = angDist(a, fAng);
          f += sign * fAmt * Math.exp(-(da * da) / 0.16);
        }
        vel[k] += f * h;
      }
      for (let k = 0; k < K; k++) disp[k] = Math.max(-this.R0 * 0.55, Math.min(this.R0 * 0.55, disp[k] + vel[k] * h));
    }
  }

  draw() {
    const { ctx, dpr, w, h, t, mood } = this;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h / 2, R = this.R0 * mood.size, T = this.T * (1 + this.level * 0.35);
    // the HUD circle around it, with ticks and slow dashes
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(90,160,255,.22)";
    ctx.beginPath(); ctx.arc(cx, cy, this.R0 * 1.62, 0, Math.PI * 2); ctx.stroke();
    ctx.strokeStyle = "rgba(120,180,255,.55)";
    ctx.setLineDash([2, 10]);
    ctx.lineDashOffset = -this.spin * 40;
    ctx.beginPath(); ctx.arc(cx, cy, this.R0 * 1.5, 0, Math.PI * 2); ctx.stroke();
    ctx.setLineDash([]);
    for (const a of [-Math.PI / 2, Math.PI / 2]) {      // chevrons top and bottom
      const r = this.R0 * 1.74, x = cx + Math.cos(a) * r, y = cy + Math.sin(a) * r, s = a < 0 ? 1 : -1;
      ctx.beginPath(); ctx.moveTo(x - 5, y - 3 * s); ctx.lineTo(x, y + 2 * s); ctx.lineTo(x + 5, y - 3 * s); ctx.stroke();
    }

    // strands: closed curves across the ring's thickness, each bent by its own noise and the soft body
    ctx.globalCompositeOperation = "lighter";
    const P = this._pts || (this._pts = new Float32Array(K * 2));
    for (let j = 0; j < STRANDS; j++) {
      const u = j / (STRANDS - 1) - 0.5;                 // -0.5 inner edge ... 0.5 outer edge
      const seed = j * 7.31;
      const twist = 2 + (j % 3);
      for (let k = 0; k < K; k++) {
        const a = (k / K) * Math.PI * 2;
        const n = noise(seed + Math.cos(a) * 1.3 + t * 0.35) * 0.6 + noise(seed * 1.7 + Math.sin(a) * 1.9 - t * 0.5) * 0.4;
        const weave = Math.sin(a * twist + this.spin * (1 + u) + seed) * T * 0.22;
        const r = R + this.disp[k] * (1.15 - Math.abs(u) * 0.5) + u * T * (1 + 0.35 * n) + n * T * 0.28 * mood.wobble + weave;
        const aa = a + this.spin * 0.25 * (0.5 + u);
        P[k * 2] = cx + Math.cos(aa) * r;
        P[k * 2 + 1] = cy + Math.sin(aa) * r;
      }
      const edge = Math.abs(u) * 2;                      // 0 centre, 1 edge
      const white = (1 - edge) ** 2;
      const alpha = (0.16 + 0.42 * (1 - edge)) * mood.glow + this.flash * 0.25;
      ctx.strokeStyle = `rgba(${Math.round(60 + 195 * white)},${Math.round(130 + 120 * white)},255,${alpha.toFixed(3)})`;
      ctx.lineWidth = 0.9 + (1 - edge) * 1.5;
      ctx.beginPath();
      // smooth closed curve through the points (midpoint quadratic curves)
      ctx.moveTo((P[0] + P[(K - 1) * 2]) / 2, (P[1] + P[(K - 1) * 2 + 1]) / 2);
      for (let k = 0; k < K; k++) {
        const nx = (k + 1) % K;
        ctx.quadraticCurveTo(P[k * 2], P[k * 2 + 1], (P[k * 2] + P[nx * 2]) / 2, (P[k * 2 + 1] + P[nx * 2 + 1]) / 2);
      }
      ctx.closePath();
      ctx.stroke();
      // a fine dotted texture on some strands
      if (j % 3 === 1) {
        ctx.fillStyle = `rgba(200,225,255,${(0.35 * mood.glow).toFixed(3)})`;
        for (let k = (j * 5) % 6; k < K; k += 6) ctx.fillRect(P[k * 2] - 0.6, P[k * 2 + 1] - 0.6, 1.2, 1.2);
      }
    }
    // soft core light, dark hole in the middle
    const g = ctx.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 1.25);
    g.addColorStop(0, "rgba(0,0,0,0)");
    g.addColorStop(0.55, `rgba(40,110,255,${(0.16 * mood.glow + this.flash * 0.15).toFixed(3)})`);
    g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g;
    ctx.beginPath(); ctx.arc(cx, cy, R * 1.6, 0, Math.PI * 2); ctx.fill();
    ctx.globalCompositeOperation = "source-over";
  }
}

function angDist(a, b) {
  let d = (a - b) % (Math.PI * 2);
  if (d > Math.PI) d -= Math.PI * 2;
  if (d < -Math.PI) d += Math.PI * 2;
  return d;
}
