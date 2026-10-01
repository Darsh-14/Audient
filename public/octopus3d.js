// A real-time 3D octopus (Three.js), modelled on the reference clip: one continuous body with the mantle
// leaning back, fine raised contour lines in blue and pink, and long sucker-lined arms that curl at the tips.
// It turns to look at the cursor. Each arm is a physics chain (springs + water drag) chasing a pose driven
// by random noise, so the arms never loop and they trail and swing when the body moves.
import * as THREE from "https://cdn.jsdelivr.net/npm/three@0.186.1/build/three.module.min.js";

// ---------------------------------------------------------------- smooth 1-D noise (non-repeating)
const perm = new Uint8Array(512);
{ let s = 1337; const p = [...Array(256).keys()]; for (let i = 255; i > 0; i--) { s = (s * 16807) % 2147483647; const j = s % (i + 1); [p[i], p[j]] = [p[j], p[i]]; } for (let i = 0; i < 512; i++) perm[i] = p[i & 255]; }
const grad = (h, x) => ((h & 1) ? -x : x) * (1 + (h >> 1 & 3) * 0.25);
function noise(x) {
  const i = Math.floor(x), f = x - i, u = f * f * f * (f * (f * 6 - 15) + 10);
  const a = grad(perm[i & 255], f), b = grad(perm[(i + 1) & 255], f - 1);
  return (a + (b - a) * u) * 1.6;
}
const fbm = (x) => noise(x) * 0.65 + noise(x * 2.13 + 17.3) * 0.25 + noise(x * 4.37 + 41.1) * 0.1;

// ---------------------------------------------------------------- skin: raised wavy contour lines, iridescent
const VERT = `
attribute float stripe;
attribute vec3 pat;
varying vec3 vN; varying vec3 vV; varying vec3 vPat; varying float vStripe; varying float vUp;
void main() {
  vec4 local = vec4(position, 1.0);
  vec3 nrm = normal;
#ifdef USE_INSTANCING
  local = instanceMatrix * local;
  nrm = mat3(instanceMatrix) * nrm;
#endif
  vec4 mv = modelViewMatrix * local;
  vN = normalize(normalMatrix * nrm);
  vV = -mv.xyz;
  vPat = pat;
  vStripe = stripe;
  vUp = normalize(mat3(modelMatrix) * nrm).y;
  gl_Position = projectionMatrix * mv;
}`;
const FRAG = `
uniform float uTime, uRed, uGlow, uFlow, uTint, uBump, uWarp, uHue, uRimHue;
varying vec3 vN; varying vec3 vV; varying vec3 vPat; varying float vStripe; varying float vUp;
vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 mod289(vec4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 permute(vec4 x) { return mod289(((x * 34.0) + 1.0) * x); }
vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }
float snoise(vec3 v) {
  const vec2 C = vec2(1.0 / 6.0, 1.0 / 3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;
  i = mod289(i);
  vec4 p = permute(permute(permute(i.z + vec4(0.0, i1.z, i2.z, 1.0)) + i.y + vec4(0.0, i1.y, i2.y, 1.0)) + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  vec3 ns = 0.142857142857 * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0) * 2.0 + 1.0;
  vec4 s1 = floor(b1) * 2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;
  vec3 p0 = vec3(a0.xy, h.x), p1 = vec3(a0.zw, h.y), p2 = vec3(a1.xy, h.z), p3 = vec3(a1.zw, h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.6 - vec4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m * m, vec4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}
void main() {
  vec3 N = normalize(vN), V = normalize(vV);
  float face = gl_FrontFacing ? 1.0 : -1.0;
  N *= face;
  // fingerprint-like contour lines: a base coordinate bent by two octaves of noise
  float w = vStripe + (snoise(vPat * 1.1) * 1.5 + snoise(vPat * 2.9 + 7.1) * 0.45) * uWarp + uTime * uFlow;
  float fw = fwidth(w);
  float aa = 1.0 - smoothstep(0.3, 0.7, fw);                     // fade lines that get finer than a pixel
  float tri = abs(fract(w) - 0.5) * 2.0;
  float h = mix(0.5, smoothstep(0.2, 0.9, tri), aa);              // 1 on a ridge, 0 in a groove
  // raise the ridges: bump the normal along the screen-space slope of h
  vec3 sp = -vV;
  vec3 sx = dFdx(sp), sy = dFdy(sp);
  vec3 r1 = cross(sy, N), r2 = cross(N, sx);
  float det = dot(sx, r1) * face;
  vec2 dh = vec2(dFdx(h), dFdy(h)) * uBump * aa;
  N = normalize(abs(det) * N - sign(det) * (dh.x * r1 + dh.y * r2));

  vec3 N0 = normalize(vN) * face;                                 // smooth normal, for hue and rim
  vec3 L = normalize(vec3(-0.45, 0.75, 0.6));
  float wrap = clamp((dot(N, L) + 0.5) / 1.5, 0.0, 1.0);
  float fres = pow(1.0 - max(dot(N0, V), 0.0), 2.6);
  vec3 blue = vec3(0.36, 0.58, 1.0), pink = vec3(1.0, 0.5, 0.84), cyan = vec3(0.62, 0.9, 1.0), violet = vec3(0.5, 0.36, 0.98);
  // iridescent: pink faces, blue towards the edges and in shade, in slowly drifting patches
  float m = 0.62 + uHue + 0.55 * snoise(vPat * 0.33 + 3.0) + 0.2 * vUp - uRimHue * fres + 0.3 * (clamp(dot(N0, L), -1.0, 1.0));
  vec3 base = mix(blue, pink, smoothstep(0.2, 0.75, m));
  base = mix(base, violet, smoothstep(0.3, 0.0, wrap) * 0.4);
  vec3 col = base * (0.34 + 0.72 * wrap);
  col *= 0.74 + 0.4 * h;                                           // light ridges, darker grooves
  vec3 H = normalize(L + V);
  col += vec3(1.0, 0.9, 1.0) * pow(max(dot(N, H), 0.0), 48.0) * 0.18 * h;
  col += cyan * fres * 0.42 * uGlow;                                // luminous blue-white edge
  col = mix(col, vec3(1.0, 0.72, 0.9), uTint * 0.3);
  col = mix(col, vec3(1.0, 0.28, 0.38), uRed * 0.55);
  gl_FragColor = vec4(col, 1.0);
}`;

const MOODS = {
  connecting: { speed: 0.5, amp: 0.6, curl: 1.0, lift: 0.0, glow: 0.7, flow: 0.0 },
  idle: { speed: 0.85, amp: 1.0, curl: 1.0, lift: 0.0, glow: 1.0, flow: 0.0 },
  listening: { speed: 1.0, amp: 1.1, curl: 0.9, lift: 0.2, glow: 1.15, flow: 0.04 },
  thinking: { speed: 1.9, amp: 1.25, curl: 1.05, lift: 0.1, glow: 1.2, flow: 0.3 },
  speaking: { speed: 1.3, amp: 1.15, curl: 1.0, lift: 0.15, glow: 1.3, flow: 0.08 },
  interrupted: { speed: 2.6, amp: 0.7, curl: 1.5, lift: -0.25, glow: 1.3, flow: 0.0 },
};

const ARMS = 8, NODES = 22, SEG = 64, RAD = 20, SUCK = 28;
const BASE_YAW = -0.3;

// body: a tube of varying radius along a spine that rises from the arm crown and leans back into the mantle
const spine = (s) => [0.85 * s * s, 2.45 * s, -1.5 * s * s];
const PROFILE = [[0, 0], [0.025, 0.42], [0.09, 0.58], [0.2, 0.6], [0.32, 0.55], [0.42, 0.56], [0.58, 0.86], [0.75, 0.98], [0.9, 0.78], [0.965, 0.5], [1, 0]];
function radius(s) {
  let k = 1; while (k < PROFILE.length - 1 && PROFILE[k][0] < s) k++;
  const r = (i) => PROFILE[Math.max(0, Math.min(PROFILE.length - 1, i))][1];
  const [s0] = PROFILE[k - 1], [s1] = PROFILE[k], u = (s - s0) / (s1 - s0 || 1);
  if (k === PROFILE.length - 1) return r(k - 1) * Math.sqrt(Math.max(0, 1 - u * u));   // round cap on the mantle
  const a = r(k - 2), b = r(k - 1), c = r(k), d = r(k + 1), u2 = u * u;
  return 0.5 * (2 * b + (c - a) * u + (2 * a - 5 * b + 4 * c - d) * u2 + (3 * b - a - 3 * c + d) * u2 * u);
}
function spineFrame(s) {
  const p = spine(s), a = spine(Math.max(0, s - 0.01)), b = spine(Math.min(1, s + 0.01));
  const t = new THREE.Vector3(b[0] - a[0], b[1] - a[1], b[2] - a[2]).normalize();
  const side = new THREE.Vector3(1, 0, 0).addScaledVector(t, -t.x).normalize();
  const front = new THREE.Vector3().crossVectors(side, t).normalize();
  return { p: new THREE.Vector3(...p), t, side, front };
}

export class Octopus3D {
  constructor(container) {
    this.el = container;
    this.state = "connecting";
    this.mood = { ...MOODS.connecting };
    this.level = 0; this.red = 0;
    this.look = { yaw: 0, pitch: 0, tyaw: 0, tpitch: 0, vyaw: 0, vpitch: 0 };
    this.squash = 0; this.squashV = 0;
    this.bob = { y: 0, v: 0, next: performance.now() + 3000 };
    this.t = Math.random() * 100;
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

    const r = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    r.setPixelRatio(Math.min(1.75, devicePixelRatio || 1));
    r.setClearColor(0x000000, 0);
    this.renderer = r;
    r.domElement.className = "octo3d";
    container.appendChild(r.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(32, 1, 0.1, 60);
    this.camera.position.set(0, 1.1, 10.6);
    this.camera.lookAt(0, 0.35, 0);
    const skin = (extra = {}) => new THREE.ShaderMaterial({
      vertexShader: VERT, fragmentShader: FRAG, side: THREE.DoubleSide,
      uniforms: { uTime: { value: 0 }, uRed: { value: 0 }, uGlow: { value: 1 }, uFlow: { value: 0 }, uTint: { value: 0 }, uBump: { value: 0.007 }, uWarp: { value: 1 }, uHue: { value: 0 }, uRimHue: { value: 0.9 }, ...extra },
    });
    this.mat = skin();
    this.armMat = skin({ uWarp: { value: 0.4 }, uHue: { value: 0.12 }, uRimHue: { value: 0.45 } });
    this.suckerMat = skin({ uTint: { value: 1 }, uBump: { value: 0.006 } });
    this.build();
    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
    addEventListener("pointermove", (e) => this.pointer(e), { passive: true });
    container.addEventListener("pointerdown", () => this.poke());
    this.last = performance.now();
    const loop = (now) => { this.frame(now); requestAnimationFrame(loop); };
    requestAnimationFrame(loop);
  }

  // ------------------------------------------------------------ model
  build() {
    this.root = new THREE.Group();          // turns to look at the cursor, bobs
    this.body = new THREE.Group();          // breathing / squash
    this.root.add(this.body);
    this.scene.add(this.root);

    // one continuous head + mantle, with contour rings that loop around the mantle's tip
    const RINGS = 96, AROUND = 72;
    const n = (RINGS + 1) * AROUND;
    const pos = new Float32Array(n * 3), pat = new Float32Array(n * 3), st = new Float32Array(n);
    for (let i = 0; i <= RINGS; i++) {
      const s = i / RINGS, f = spineFrame(s), rad = radius(s);
      for (let j = 0; j < AROUND; j++) {
        const th = (j / AROUND) * Math.PI * 2, c = Math.cos(th), sn = Math.sin(th);
        const bulge = 1 + 0.08 * Math.exp(-((s - 0.24) ** 2) / 0.004) * Math.abs(c);   // brow ridges by the eyes
        const k = i * AROUND + j, rr = rad * bulge;
        const x = f.p.x + (c * f.side.x + sn * f.front.x) * rr * 1.04;
        const y = f.p.y + (c * f.side.y + sn * f.front.y) * rr;
        const z = f.p.z + (c * f.side.z + sn * f.front.z) * rr;
        pos.set([x, y, z], k * 3); pat.set([x, y, z], k * 3);
        st[k] = (1 - s) * 62;
      }
    }
    const idx = [];
    for (let i = 0; i < RINGS; i++) for (let j = 0; j < AROUND; j++) {
      const a = i * AROUND + j, b = i * AROUND + ((j + 1) % AROUND), c = (i + 1) * AROUND + j, d = (i + 1) * AROUND + ((j + 1) % AROUND);
      idx.push(a, b, c, b, d, c);
    }
    const bg = new THREE.BufferGeometry();
    bg.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    bg.setAttribute("pat", new THREE.BufferAttribute(pat, 3));
    bg.setAttribute("stripe", new THREE.BufferAttribute(st, 1));
    bg.setIndex(idx);
    bg.computeVertexNormals();
    this.mantle = new THREE.Mesh(bg, this.mat);
    this.body.add(this.mantle);

    // eyes: a raised socket on each side of the head, a pale eyeball and a dark slit pupil that tracks the cursor
    this.eyes = [];
    const ef = spineFrame(0.25), er = radius(0.25);
    const eyeWhite = new THREE.MeshBasicMaterial({ color: 0x2c2346 });
    const pupilMat = new THREE.MeshBasicMaterial({ color: 0x050309 });
    const shine = new THREE.MeshBasicMaterial({ color: 0xffffff });
    for (const side of [-1, 1]) {
      const out = ef.side.clone().multiplyScalar(side).multiplyScalar(0.8).addScaledVector(ef.front, 0.6).normalize();
      const g = new THREE.Group();
      g.position.copy(ef.p).addScaledVector(out, er * 0.98).addScaledVector(ef.t, 0.02);
      g.lookAt(g.position.clone().add(out));
      const sg = new THREE.SphereGeometry(0.2, 40, 28);
      const sp = sg.attributes.position;
      sg.setAttribute("pat", new THREE.BufferAttribute(Float32Array.from(sp.array, (v) => v * 3), 3));
      sg.setAttribute("stripe", new THREE.BufferAttribute(Float32Array.from({ length: sp.count }, (_, i) => Math.hypot(sp.getX(i), sp.getY(i)) * 40), 1));
      const socket = new THREE.Mesh(sg, this.mat);
      socket.scale.set(1.05, 0.95, 0.62);
      g.add(socket);
      const ball = new THREE.Mesh(new THREE.SphereGeometry(0.125, 32, 24), eyeWhite);
      ball.position.z = 0.06;
      g.add(ball);
      const pupilPivot = new THREE.Group();
      pupilPivot.position.z = 0.06;
      const pupil = new THREE.Mesh(new THREE.SphereGeometry(0.07, 24, 16), pupilMat);
      pupil.scale.set(1.35, 0.8, 0.6);
      pupil.position.z = 0.09;
      pupilPivot.add(pupil);
      const glint = new THREE.Mesh(new THREE.SphereGeometry(0.018, 10, 8), shine);
      glint.position.set(-0.035, 0.04, 0.125);
      pupilPivot.add(glint);
      g.add(pupilPivot);
      this.body.add(g);
      this.eyes.push({ g, pupilPivot, side });
    }

    // arms: a physics chain each, skinned with a tube every frame
    const tidx = [];
    for (let i = 0; i < SEG; i++) for (let j = 0; j < RAD; j++) {
      const a = i * RAD + j, b = i * RAD + ((j + 1) % RAD), c = (i + 1) * RAD + j, d = (i + 1) * RAD + ((j + 1) % RAD);
      tidx.push(a, c, b, b, c, d);
    }
    this.arms = [];
    for (let k = 0; k < ARMS; k++) {
      const g = new THREE.BufferGeometry();
      const nv = (SEG + 1) * RAD;
      g.setAttribute("position", new THREE.BufferAttribute(new Float32Array(nv * 3), 3));
      g.setAttribute("normal", new THREE.BufferAttribute(new Float32Array(nv * 3), 3));
      const len = 3.05 + 0.35 * Math.sin(k * 2.3 + 0.7);
      const ast = new Float32Array(nv), apat = new Float32Array(nv * 3);
      for (let i = 0; i <= SEG; i++) for (let j = 0; j < RAD; j++) {
        const th = (j / RAD) * Math.PI * 2, s = i / SEG, v = i * RAD + j;
        ast[v] = (j / RAD) * 18;                                              // lines run along the arm
        apat.set([Math.cos(th) * 0.4 + k * 7.13, Math.sin(th) * 0.4 + k * 3.31, s * len * 1.3], v * 3);
      }
      g.setAttribute("stripe", new THREE.BufferAttribute(ast, 1));
      g.setAttribute("pat", new THREE.BufferAttribute(apat, 3));
      g.setIndex(tidx);
      const mesh = new THREE.Mesh(g, this.armMat);
      mesh.frustumCulled = false;
      this.scene.add(mesh);
      const phi = (k / ARMS) * Math.PI * 2 + Math.PI / ARMS;
      this.arms.push({
        mesh, phi, len, seed: k * 17.31 + 3.7,
        p: new Float32Array(NODES * 3), q: new Float32Array(NODES * 3), tg: new Float32Array(NODES * 3), or: new Float32Array(NODES * 3), ready: false,
      });
    }

    // suckers: two staggered rows of cups on the underside of each arm
    const cup = new THREE.LatheGeometry([[0, 0.15], [0.42, 0.2], [0.78, 0.44], [1, 0.36], [1.05, 0.08], [0.95, -0.3]].map(([x, y]) => new THREE.Vector2(x, y)), 14);
    const cp = cup.attributes.position;
    cup.setAttribute("pat", new THREE.BufferAttribute(Float32Array.from(cp.array, (v) => v * 2), 3));
    cup.setAttribute("stripe", new THREE.BufferAttribute(Float32Array.from({ length: cp.count }, (_, i) => Math.hypot(cp.getX(i), cp.getZ(i)) * 2.2), 1));
    this.suckers = new THREE.InstancedMesh(cup, this.suckerMat, ARMS * SUCK);
    this.suckers.frustumCulled = false;
    this.scene.add(this.suckers);
    this.tmp = { m: new THREE.Matrix4(), q: new THREE.Quaternion(), s: new THREE.Vector3(), y: new THREE.Vector3(0, 1, 0), d: new THREE.Vector3(), p: new THREE.Vector3() };
    this.P = Array.from({ length: SEG + 1 }, () => new Float32Array(3));
    this.O = Array.from({ length: SEG + 1 }, () => new Float32Array(3));
  }

  // where each arm wants to be: arch out from the crown, then curl at the tip, all bent by slow random noise
  armTargets(t) {
    const { amp, curl, lift } = this.mood;
    const M = this.root.matrixWorld.elements;
    const step = (len) => len / (NODES - 1);
    for (const arm of this.arms) {
      const { phi, seed, len, tg, or } = arm;
      const raise = fbm(seed + t * 0.11) * 0.5 * amp + lift + this.flare * 0.5;
      const elev0 = 0.12 + raise;
      const arch = 1.05 + 0.35 * fbm(seed * 1.3 + t * 0.17);
      const curlAmt = curl * (3.6 + 1.6 * fbm(seed * 2.1 + t * 0.13));
      const twist = (arm.phi > Math.PI ? 1 : -1) * (0.9 + 0.6 * fbm(seed * 2.9 + t * 0.09));
      const sway = fbm(seed * 0.7 + t * 0.2) * 0.45 * amp;
      let x = Math.cos(phi) * 0.32, y = 0.14, z = Math.sin(phi) * 0.32;
      for (let i = 0; i < NODES; i++) {
        const s = i / (NODES - 1);
        const wave = noise(seed * 1.7 + t * 0.8 - s * 2.6) * 0.55 * amp;         // travels from base to tip
        const s4 = s * s * s * s;
        const elev = elev0 - arch * s - curlAmt * s4 + wave * s;
        const az = phi + sway * s + 0.45 * noise(seed * 3.3 + t * 0.6 - s * 2) * s * amp + twist * curl * s4 * 1.6;
        const ce = Math.cos(elev), co = Math.cos(elev - Math.PI / 2);
        const dx = ce * Math.cos(az), dy = Math.sin(elev), dz = ce * Math.sin(az);
        const ox = co * Math.cos(az), oy = Math.sin(elev - Math.PI / 2), oz = co * Math.sin(az);
        tg[i * 3] = M[0] * x + M[4] * y + M[8] * z + M[12];
        tg[i * 3 + 1] = M[1] * x + M[5] * y + M[9] * z + M[13];
        tg[i * 3 + 2] = M[2] * x + M[6] * y + M[10] * z + M[14];
        or[i * 3] = M[0] * ox + M[4] * oy + M[8] * oz;
        or[i * 3 + 1] = M[1] * ox + M[5] * oy + M[9] * oz;
        or[i * 3 + 2] = M[2] * ox + M[6] * oy + M[10] * oz;
        const st = step(len);
        x += dx * st; y += dy * st; z += dz * st;
      }
    }
  }

  // springs pull each node towards its target (stiff at the base, loose at the tip), water drag damps it,
  // and fixed segment lengths keep the arm from stretching
  simulate(h) {
    const drag = Math.exp(-h * 2.4);
    for (const arm of this.arms) {
      const { p, q, tg, len } = arm;
      if (!arm.ready) { p.set(tg); q.set(tg); arm.ready = true; }
      for (let i = 1; i < NODES; i++) {
        const s = i / (NODES - 1), k = 140 * (1 - s) ** 2 + 7;
        for (let a = 0; a < 3; a++) {
          const o = i * 3 + a, v = (p[o] - q[o]) * drag, acc = (tg[o] - p[o]) * k;
          q[o] = p[o];
          p[o] += v + acc * h * h;
        }
      }
      p[0] = tg[0]; p[1] = tg[1]; p[2] = tg[2];
      const L = len / (NODES - 1);
      for (let i = 1; i < NODES; i++) {
        const o = i * 3, dx = p[o] - p[o - 3], dy = p[o + 1] - p[o - 2], dz = p[o + 2] - p[o - 1];
        const d = Math.hypot(dx, dy, dz) || 1, f = L / d;
        p[o] = p[o - 3] + dx * f; p[o + 1] = p[o - 2] + dy * f; p[o + 2] = p[o - 1] + dz * f;
      }
    }
  }

  skinArms() {
    const P = this.P, O = this.O, tm = this.tmp;
    let sIdx = 0;
    const cr = (a, b, c, d, u) => {
      const u2 = u * u, u3 = u2 * u;
      return 0.5 * (2 * b + (c - a) * u + (2 * a - 5 * b + 4 * c - d) * u2 + (3 * b - a - 3 * c + d) * u3);
    };
    for (const arm of this.arms) {
      const { p, or } = arm;
      for (let i = 0; i <= SEG; i++) {                                 // smooth curve through the nodes
        const f = (i / SEG) * (NODES - 1), n = Math.min(NODES - 2, Math.floor(f)), u = f - n;
        const n0 = Math.max(0, n - 1), n3 = Math.min(NODES - 1, n + 2);
        for (let a = 0; a < 3; a++) {
          P[i][a] = cr(p[n0 * 3 + a], p[n * 3 + a], p[(n + 1) * 3 + a], p[n3 * 3 + a], u);
          O[i][a] = or[n * 3 + a] * (1 - u) + or[(n + 1) * 3 + a] * u;
        }
      }
      const pos = arm.mesh.geometry.attributes.position.array, nor = arm.mesh.geometry.attributes.normal.array;
      for (let i = 0; i <= SEG; i++) {
        const A = P[Math.max(0, i - 1)], B = P[Math.min(SEG, i + 1)];
        let tx = B[0] - A[0], ty = B[1] - A[1], tz = B[2] - A[2];
        const tl = Math.hypot(tx, ty, tz) || 1; tx /= tl; ty /= tl; tz /= tl;
        let [ox, oy, oz] = O[i];
        const d = ox * tx + oy * ty + oz * tz;
        ox -= d * tx; oy -= d * ty; oz -= d * tz;
        const ol = Math.hypot(ox, oy, oz) || 1; ox /= ol; oy /= ol; oz /= ol;
        const bx = ty * oz - tz * oy, by = tz * ox - tx * oz, bz = tx * oy - ty * ox;
        const s = i / SEG, rad = 0.3 * Math.pow(1 - s, 0.95) + 0.014 + 0.09 * Math.max(0, 1 - s * 8) ** 2;
        for (let j = 0; j < RAD; j++) {
          const th = (j / RAD) * Math.PI * 2, c = Math.cos(th), sn = Math.sin(th);
          const nx = c * ox + sn * bx, ny = c * oy + sn * by, nz = c * oz + sn * bz;
          const o = (i * RAD + j) * 3;
          pos[o] = P[i][0] + nx * rad; pos[o + 1] = P[i][1] + ny * rad; pos[o + 2] = P[i][2] + nz * rad;
          nor[o] = nx; nor[o + 1] = ny; nor[o + 2] = nz;
        }
        // two staggered rows of suckers on the underside
        if (i >= 6 && i < SEG - 3 && i % 2 === 0 && sIdx < this.suckers.count) {
          const a = (i / 2) % 2 ? 0.42 : -0.42, c = Math.cos(a), sn = Math.sin(a);
          tm.d.set(c * ox + sn * bx, c * oy + sn * by, c * oz + sn * bz);
          const r = rad * 0.4;
          tm.q.setFromUnitVectors(tm.y, tm.d);
          tm.s.set(r, r * 0.9, r);
          tm.p.set(P[i][0] + tm.d.x * rad * 0.88, P[i][1] + tm.d.y * rad * 0.88, P[i][2] + tm.d.z * rad * 0.88);
          tm.m.compose(tm.p, tm.q, tm.s);
          this.suckers.setMatrixAt(sIdx++, tm.m);
        }
      }
      arm.mesh.geometry.attributes.position.needsUpdate = true;
      arm.mesh.geometry.attributes.normal.needsUpdate = true;
    }
    this.suckers.count = sIdx;
    this.suckers.instanceMatrix.needsUpdate = true;
  }

  // ------------------------------------------------------------ interaction + state
  pointer(e) {
    const r = this.el.getBoundingClientRect();
    const mx = (e.clientX - (r.left + r.width / 2)) / (r.width * 0.6);
    const my = (e.clientY - (r.top + r.height * 0.42)) / (r.height * 0.6);
    this.look.tyaw = Math.max(-1, Math.min(1, mx)) * 0.8;            // turn towards the cursor
    this.look.tpitch = Math.max(-1, Math.min(1, my)) * 0.35;
    this.lastMove = performance.now();
  }

  poke() { this.squashV -= 3.2; this.bob.v += 0.9; }
  setState(s) { if (MOODS[s]) this.state = s; }
  setLevel(v) { this.level = Math.max(0, Math.min(1, v)); }

  resize() {
    const w = this.el.clientWidth || 1, h = this.el.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  frame(now) {
    if (now - this.last < 30) return;                                // ~30 fps keeps the agent snappy
    const dt = Math.min(0.06, (now - this.last) / 1000);
    this.last = now;
    if (document.hidden || this.el.offsetParent === null) return;
    const tgt = MOODS[this.state];
    const k = 1 - Math.exp(-dt * 3);
    for (const key of Object.keys(tgt)) this.mood[key] += (tgt[key] - this.mood[key]) * k;
    this.red += ((this.state === "interrupted" ? 1 : 0) - this.red) * (1 - Math.exp(-dt * 6));
    const speed = this.reduced ? 0.1 : this.mood.speed * (1 + this.level * 0.8);
    this.t += dt * speed;
    const t = this.t;

    // looking around: a damped spring towards the cursor; when the cursor rests, it glances about on its own
    const L = this.look;
    if (!this.lastMove || now - this.lastMove > 4000) {
      L.tyaw = fbm(t * 0.15 + 5) * 0.45; L.tpitch = fbm(t * 0.12 + 9) * 0.15;
    }
    // an occasional swim stroke: the mantle pumps, the body lifts and the arms trail behind it
    const B = this.bob;
    if (!this.reduced && now > B.next && (this.state === "idle" || this.state === "listening")) {
      B.v += 0.75; this.squashV -= 1.6; B.next = now + 4500 + Math.random() * 6000;
    }
    this.flare = Math.max(0, (this.flare || 0) - dt * 0.8);
    const H = 2, h = dt / H;
    for (let n = 0; n < H; n++) {
      L.vyaw += (26 * (L.tyaw - L.yaw) - 7.4 * L.vyaw) * h; L.yaw += L.vyaw * h;
      L.vpitch += (26 * (L.tpitch - L.pitch) - 7.4 * L.vpitch) * h; L.pitch += L.vpitch * h;
      B.v += (-14 * (B.y - Math.sin(t * 0.7) * 0.08) - 3.2 * B.v) * h; B.y += B.v * h;
    }
    this.root.position.y = B.y;
    this.root.rotation.set(L.pitch * 0.55, BASE_YAW + L.yaw, -L.vyaw * 0.06);
    for (const e of this.eyes) {                                     // eyes lead the turn
      e.pupilPivot.rotation.set(L.pitch * 0.7 + (L.tpitch - L.pitch) * 0.8, (L.tyaw - L.yaw) * 1.3 + e.side * L.yaw * 0.3, 0);
    }

    // breathing, speaking pulse, poke squash
    this.squashV += (-55 * this.squash - 6 * this.squashV) * dt;
    this.squash += this.squashV * dt;
    const talk = this.state === "speaking" ? Math.sin(t * 9) * 0.03 : 0;
    const breath = Math.sin(t * 1.7) * 0.018 + talk + this.level * 0.045;
    this.body.scale.set(1 + breath - this.squash * 0.25, 1 + breath + this.squash * 0.6, 1 + breath - this.squash * 0.25);

    this.root.updateMatrixWorld();
    this.armTargets(t);
    for (let n = 0; n < H; n++) this.simulate(h);
    this.skinArms();
    for (const m of [this.mat, this.armMat, this.suckerMat]) {
      const u = m.uniforms;
      u.uTime.value = t; u.uRed.value = this.red; u.uGlow.value = this.mood.glow; u.uFlow.value = this.mood.flow;
    }
    this.renderer.render(this.scene, this.camera);
    if (this.mirrorCtx && (this.frames = (this.frames || 0) + 1) % 3 === 0) {   // live thumbnail (avatar)
      const src = this.renderer.domElement, m = this.mirrorCtx;
      m.clearRect(0, 0, m.canvas.width, m.canvas.height);
      m.drawImage(src, src.width * 0.22, src.height * 0.04, src.width * 0.56, src.height * 0.56, 0, 0, m.canvas.width, m.canvas.height);
    }
  }

  mirror(canvas) { this.mirrorCtx = canvas.getContext("2d"); }
}
