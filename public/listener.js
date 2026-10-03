// The listener: a 3D glass creature (Three.js). A transparent body with arms that drift and two ear-lobes on
// top. Its colour shows what it is doing, blending smoothly from one state to the next:
//   idle         calm ice-blue
//   listening    cyan and aqua, lit brighter the louder you talk
//   thinking     violet, pulsing
//   speaking     pink and coral, breathing as it talks
//   interrupted  a quick golden flash
// It also reacts physically: it turns to the cursor and reaches for it, a fast sweep or a click sends ripples
// through the glass, and the ears perk up while you talk.
import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.186.1/build/three.module.min.js";

// smooth 1-D noise, for drift that never repeats
const perm = new Uint8Array(512);
{ let s = 4111; const p = [...Array(256).keys()]; for (let i = 255; i > 0; i--) { s = (s * 16807) % 2147483647; const j = s % (i + 1); [p[i], p[j]] = [p[j], p[i]]; } for (let i = 0; i < 512; i++) perm[i] = p[i & 255]; }
const grad = (h, x) => ((h & 1) ? -x : x) * (1 + (h >> 1 & 3) * 0.25);
function noise(x) {
  const i = Math.floor(x), f = x - i, u = f * f * f * (f * (f * 6 - 15) + 10);
  const a = grad(perm[i & 255], f), b = grad(perm[(i + 1) & 255], f - 1);
  return (a + (b - a) * u) * 1.6;
}

// a, b: the two colours flowing through the glass; energy: how lit it is from within; ears: how perked;
// pulse: thinking pulse; breathe: speaking breath
const MOODS = {
  connecting: { a: 0x5c79a8, b: 0x8197c0, energy: 0.08, ears: 0.25, pulse: 0, breathe: 0, drift: 0.4 },
  idle: { a: 0x7fb4ff, b: 0xc8e0ff, energy: 0.16, ears: 0.35, pulse: 0, breathe: 0, drift: 1.0 },
  listening: { a: 0x1fe0ff, b: 0x2fffc8, energy: 0.45, ears: 0.8, pulse: 0, breathe: 0, drift: 0.8 },
  thinking: { a: 0x8a5cff, b: 0x4f6bff, energy: 0.45, ears: 0.45, pulse: 1, breathe: 0, drift: 0.6 },
  speaking: { a: 0xff4fd8, b: 0xff8a5c, energy: 0.55, ears: 0.4, pulse: 0, breathe: 1, drift: 0.9 },
  interrupted: { a: 0xffd27a, b: 0xff7a9a, energy: 0.8, ears: 1.0, pulse: 0, breathe: 0, drift: 0.3 },
};
const ARMS = [0.35, 1.55, 2.75, 3.85, 5.1];   // azimuths of the arms

const GLASS_VERT = `
varying vec3 vN; varying vec3 vV; varying vec3 vP;
void main() {
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  vN = normalize(normalMatrix * normal);
  vV = -mv.xyz;
  vP = position;
  gl_Position = projectionMatrix * mv;
}`;
const GLASS_FRAG = `
uniform float uTime, uEnergy, uFlash, uBack;
uniform vec3 uColA, uColB;
varying vec3 vN; varying vec3 vV; varying vec3 vP;
void main() {
  vec3 N = normalize(vN), V = normalize(vV);
  if (uBack > 0.5) N = -N;                                  // the inside of the far wall
  float ndv = clamp(abs(dot(N, V)), 0.0, 1.0);
  float fres = pow(1.0 - ndv, 2.3);
  // the state's two colours flow through the body in soft bands
  float band = 0.5 + 0.5 * sin(dot(vP, vec3(2.1, 1.4, 1.7)) * 1.7 - uTime * 1.6);
  float band2 = 0.5 + 0.5 * sin(dot(vP, vec3(-1.3, 2.2, 0.9)) * 2.3 + uTime * 1.1);
  vec3 tint = mix(uColA, uColB, band * 0.7 + band2 * 0.3);
  // lit from within, a bright tinted rim, and glassy highlights
  vec3 col = tint * (0.08 + 0.6 * uEnergy) * (1.0 - fres * 0.5);
  col += mix(tint, vec3(1.0), 0.25) * fres * (0.8 + 0.6 * uEnergy);
  float alpha = 0.05 + 0.3 * uEnergy + 0.72 * fres;
  vec3 L1 = normalize(vec3(-0.45, 0.85, 0.55)), L2 = normalize(vec3(0.7, 0.25, 0.45));
  float spec = pow(max(dot(N, normalize(L1 + V)), 0.0), 90.0) * 1.2 + pow(max(dot(N, normalize(L2 + V)), 0.0), 60.0) * 0.45;
  vec3 R = reflect(-V, N);
  float strip = smoothstep(0.86, 0.97, R.y) * (1.0 - smoothstep(0.97, 1.0, R.y)) * 0.5;
  col += vec3(1.0) * (spec + strip);
  alpha += spec * 0.9 + strip * 0.6;
  col = mix(col, vec3(1.0, 0.95, 0.85), uFlash * 0.35);
  gl_FragColor = vec4(col, clamp(alpha * (uBack > 0.5 ? 0.45 : 1.0), 0.0, 1.0));
  #include <colorspace_fragment>
}`;

export class Listener {
  constructor(container) {
    this.el = container;
    this.state = "connecting";
    const m0 = MOODS.connecting;
    this.mood = { energy: m0.energy, ears: m0.ears, pulse: 0, breathe: 0, drift: m0.drift };
    this.colA = new THREE.Color(m0.a);   // the colours on screen now; they blend toward the state's pair
    this.colB = new THREE.Color(m0.b);
    this.level = 0;          // the user's voice level (0..1), from the mic
    this.ear = 0.25;         // current ear perk (0..1)
    this.reach = 0;          // how much it reaches toward the cursor (0..1)
    this.ripple = 0;         // ripple energy (cursor sweeps, pokes, voice)
    this.flinch = 0;
    this.t = Math.random() * 50;
    this.look = { yaw: 0, pitch: 0, vy: 0, vp: 0, ty: 0, tp: 0 };
    this.ptr = null;
    this.frames = 0;
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

    const r = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    r.setPixelRatio(Math.min(1.75, devicePixelRatio || 1));
    r.setClearColor(0x000000, 0);
    r.domElement.className = "listener";
    container.appendChild(r.domElement);
    this.renderer = r;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(34, 1, 0.1, 50);
    this.camera.position.set(0, 1.35, 4.7);
    this.camera.lookAt(0, -0.05, 0);
    this.build();
    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
    addEventListener("pointermove", (e) => this.pointer(e), { passive: true });
    container.addEventListener("pointerleave", () => { this.ptr = null; });
    container.addEventListener("pointerdown", () => this.poke());
    this.last = performance.now();
    const loop = (now) => { this.frame(now); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  build() {
    this.root = new THREE.Group();
    this.scene.add(this.root);
    // the glass body: a sphere reshaped every frame (arms, ears, drift, ripples, reach)
    const g = new THREE.SphereGeometry(1, 144, 108);
    this.base = Float32Array.from(g.attributes.position.array);   // unit directions
    const glass = (back) => new THREE.ShaderMaterial({
      vertexShader: GLASS_VERT, fragmentShader: GLASS_FRAG, transparent: true, depthWrite: false,
      side: back ? THREE.BackSide : THREE.FrontSide,
      uniforms: { uTime: { value: 0 }, uEnergy: { value: 0.1 }, uFlash: { value: 0 }, uBack: { value: back ? 1 : 0 },
                  uColA: { value: this.colA }, uColB: { value: this.colB } },
    });
    this.glassBack = new THREE.Mesh(g, glass(true));    // far wall first, then the near wall: a solid-looking volume
    this.body = new THREE.Mesh(g, glass(false));
    this.glassBack.renderOrder = 1;
    this.body.renderOrder = 2;
    this.root.add(this.glassBack, this.body);
    const lobeDirs = ARMS.map((a) => new THREE.Vector3(Math.cos(a), -0.28, Math.sin(a)).normalize());
    lobeDirs.push(new THREE.Vector3(-0.6, 0.76, 0.24).normalize(), new THREE.Vector3(0.6, 0.76, 0.24).normalize());
    this.lobes = lobeDirs.map((d, i) => ({ dir0: d.clone(), dir: d.clone(), ear: i >= ARMS.length, ext: 1,
                                           len: i >= ARMS.length ? 0.62 : 0.9 + 0.12 * Math.sin(i * 2.1) }));
    this.tmp = { v: new THREE.Vector3(), c: new THREE.Vector3(), inv: new THREE.Quaternion(), up: new THREE.Vector3(0, 1, 0),
                 a: new THREE.Color(), b: new THREE.Color() };
  }

  resize() {
    const w = this.el.clientWidth || 1, h = this.el.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  pointer(e) {
    const r = this.el.getBoundingClientRect();
    const x = (e.clientX - (r.left + r.width / 2)) / (r.width / 2), y = (e.clientY - (r.top + r.height / 2)) / (r.height / 2);
    const now = performance.now();
    if (this.ptr) {  // a fast sweep near it sends ripples through the glass
      const v = Math.hypot(x - this.ptr.x, y - this.ptr.y) / Math.max(0.008, (now - this.ptr.at) / 1000);
      if (Math.hypot(x, y) < 1.1) this.ripple = Math.min(1, this.ripple + Math.min(v, 6) * 0.02);
    }
    this.ptr = { x, y, at: now };
  }

  poke() { this.ripple = 1; this.flinch = Math.max(this.flinch, 0.6); }
  setState(s) {
    if (!MOODS[s] || s === this.state) return;
    if (s === "interrupted") { this.flinch = 1; this.ripple = 1; }
    this.state = s;
  }
  setLevel(v) { this.level = Math.max(0, Math.min(1, v)); }

  frame(now) {
    if (now - this.last < 30) return;                     // ~30 fps keeps the agent snappy
    const dt = Math.min(0.06, (now - this.last) / 1000);
    this.last = now;
    if (document.hidden || this.el.offsetParent === null) return;
    const tg = MOODS[this.state], k = 1 - Math.exp(-dt * 4);
    for (const key of ["energy", "ears", "pulse", "breathe", "drift"]) this.mood[key] += (tg[key] - this.mood[key]) * k;
    // colours blend toward the state's pair; while you talk, the listening cyan comes through whatever the state
    const kc = 1 - Math.exp(-dt * 3.2);
    this.colA.lerp(this.tmp.a.set(tg.a).lerp(this.tmp.b.set(MOODS.listening.a), this.level * 0.6), kc);
    this.colB.lerp(this.tmp.a.set(tg.b), kc);
    const t = (this.t += dt * (this.reduced ? 0.15 : 0.6 + this.mood.drift * 0.6));

    // the cursor: how close it is, and where
    const p = this.ptr, dist = p ? Math.hypot(p.x, p.y) : 9;
    const near = p ? Math.max(0, Math.min(1, (1.25 - dist) / 0.75)) : 0;
    this.reach += (near - this.reach) * (1 - Math.exp(-dt * 5));
    const L = this.look;
    if (p) { L.ty = Math.max(-1, Math.min(1, p.x)) * 0.55; L.tp = Math.max(-1, Math.min(1, p.y)) * 0.28; }
    else { L.ty = noise(t * 0.2 + 3) * 0.25; L.tp = noise(t * 0.17 + 7) * 0.08; }
    L.vy += (24 * (L.ty - L.yaw) - 7 * L.vy) * dt; L.yaw += L.vy * dt;
    L.vp += (24 * (L.tp - L.pitch) - 7 * L.vp) * dt; L.pitch += L.vp * dt;
    this.flinch = Math.max(0, this.flinch - dt * 2.2);
    this.root.rotation.set(L.pitch * 0.5 + this.flinch * 0.15, L.yaw + noise(t * 0.11) * 0.12, -L.vy * 0.03);
    const sc = 1 - this.flinch * 0.08;
    this.root.scale.set(sc, sc * 0.8, sc);   // a little flattened

    // ears perk with the agent's mood, the user's voice and a nearby cursor
    const earTarget = Math.min(1, Math.max(this.mood.ears, this.level * 1.2, near * 0.75) + this.flinch * 0.3);
    this.ear += (earTarget - this.ear) * (1 - Math.exp(-dt * 7));
    this.ripple = Math.max(this.level * 0.7, this.ripple - dt * 0.9);

    // cursor direction in the body's own frame
    const c = this.tmp.c.set(p ? p.x : 0, p ? -p.y : 0, 0.55).normalize();
    this.tmp.inv.copy(this.root.quaternion).invert();
    c.applyQuaternion(this.tmp.inv);

    // lobe directions: arms drift and sway, ears lean toward the cursor
    for (const [i, lb] of this.lobes.entries()) {
      const v = this.tmp.v.copy(lb.dir0);
      if (lb.ear) {
        v.x += (p ? p.x : 0) * 0.18 * this.ear; v.z += 0.12 * this.ear; v.y += 0.1 * this.ear;
      } else {
        v.applyAxisAngle(this.tmp.up, noise(t * 0.35 + i * 7.3) * 0.28);
        v.y += noise(t * 0.27 + i * 3.1) * 0.12;
      }
      lb.dir.copy(v.normalize());
      lb.ext = lb.ear ? 0.25 + this.ear * 0.75 : 0.75 + 0.25 * noise(t * 0.3 + i * 5.1) + this.reach * 0.12 * Math.max(0, lb.dir.dot(c));
    }

    // reshape the glass
    const pos = this.body.geometry.attributes.position.array, base = this.base;
    const breathe = this.mood.breathe * Math.sin(t * 7) * 0.035;
    for (let n = 0; n < pos.length; n += 3) {
      const x = base[n], y = base[n + 1], z = base[n + 2];
      let r = 0.72 + breathe;
      for (const lb of this.lobes) {
        const d = lb.dir, cc = x * d.x + y * d.y + z * d.z;
        if (cc > 0.2) r += lb.len * lb.ext * Math.pow(cc, lb.ear ? 34 : 14);
      }
      const cr = x * c.x + y * c.y + z * c.z;                        // reach toward the cursor
      if (cr > 0) r += this.reach * 0.22 * Math.pow(cr, 9);
      r += 0.03 * (Math.sin(3.1 * x + 1.7 * t) + Math.sin(2.3 * y - 1.3 * t + 1) + Math.sin(2.9 * z + 0.9 * t + 2)) / 3;
      r += this.ripple * 0.035 * Math.sin(16 * (y * 0.6 + x * 0.3 + z * 0.4) - t * 14);  // ripples run through it
      pos[n] = x * r; pos[n + 1] = y * r; pos[n + 2] = z * r;
    }
    this.body.geometry.attributes.position.needsUpdate = true;
    this.body.geometry.computeVertexNormals();

    // how lit it is from within: the state, your voice, the cursor, a thinking pulse, a speaking breath
    const energy = Math.max(0, Math.min(1, this.mood.energy + this.level * 0.45 + this.reach * 0.12 + this.flinch * 0.3
      + this.mood.pulse * 0.18 * Math.sin(t * 8) + this.mood.breathe * 0.1 * Math.sin(t * 7)));
    for (const m of [this.body.material, this.glassBack.material]) {
      m.uniforms.uTime.value = t;
      m.uniforms.uEnergy.value = energy;
      m.uniforms.uFlash.value = this.flinch;
    }
    this.renderer.render(this.scene, this.camera);
    this.frames++;
  }
}
