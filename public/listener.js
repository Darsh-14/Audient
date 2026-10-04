// The listener: a 3D puffer fish (Three.js). It swims gently in place and turns to look at your cursor.
// While it listens it puffs up into a spiky ball, and puffs fuller the louder you talk; it deflates when you
// stop. Its colour shows what the agent is doing, blending smoothly between states:
//   idle         calm blue, deflated, drifting
//   listening    cyan and aqua, half puffed, fully puffed while you talk
//   thinking     violet, pulsing
//   speaking     pink and coral, mouth moving as it talks
//   interrupted  a quick golden flash and a startled puff
// A click is a poke: it puffs up in surprise.
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
const clamp = (v, a = -1, b = 1) => Math.max(a, Math.min(b, v));
const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };

// back: body colour (the belly is a pale version of it); fin: fins and tail; puff: how inflated at rest;
// energy: glow; pulse: thinking pulse; talk: mouth movement; swim: how lively
const MOODS = {
  connecting: { back: 0x5d7194, fin: 0x8ea3c3, puff: 0, energy: 0.1, pulse: 0, talk: 0, swim: 0.4 },
  idle: { back: 0x4f9bff, fin: 0x9fd0ff, puff: 0, energy: 0.25, pulse: 0, talk: 0, swim: 1.0 },
  listening: { back: 0x14d2ee, fin: 0x7dffe9, puff: 0.5, energy: 0.5, pulse: 0, talk: 0, swim: 0.7 },
  thinking: { back: 0x8a5cff, fin: 0xc3a8ff, puff: 0.3, energy: 0.5, pulse: 1, talk: 0, swim: 0.5 },
  speaking: { back: 0xff5fb8, fin: 0xffa48a, puff: 0.15, energy: 0.6, pulse: 0, talk: 1, swim: 0.9 },
  interrupted: { back: 0xffbf47, fin: 0xffe08a, puff: 1, energy: 0.8, pulse: 0, talk: 0, swim: 0.3 },
};

const BODY_VERT = `
varying vec3 vN; varying vec3 vV; varying vec3 vO;
void main() {
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  vN = normalize(normalMatrix * normal);
  vV = -mv.xyz;
  vO = position;
  gl_Position = projectionMatrix * mv;
}`;
const BODY_FRAG = `
uniform vec3 uBack;
uniform float uEnergy, uFlash;
varying vec3 vN; varying vec3 vV; varying vec3 vO;
float hash(vec3 p) { return fract(sin(dot(p, vec3(127.1, 311.7, 74.7))) * 43758.5453); }
void main() {
  vec3 N = normalize(vN), V = normalize(vV), o = normalize(vO);
  // pale belly, coloured back
  vec3 belly = mix(uBack, vec3(1.0), 0.8);
  vec3 base = mix(belly, uBack, smoothstep(-0.35, 0.25, o.y));
  // dark spots on the back (they stretch with the skin as it puffs up)
  vec3 q = o * 5.5, c = floor(q), f = fract(q) - 0.5;
  vec3 off = (vec3(hash(c + 1.3), hash(c + 2.7), hash(c + 4.1)) - 0.5) * 0.5;
  float spot = (1.0 - smoothstep(0.11, 0.16, length(f - off))) * step(0.35, hash(c)) * smoothstep(-0.1, 0.3, o.y);
  base = mix(base, uBack * 0.35, spot * 0.75);
  // soft light, a glossy highlight and a glowing rim in the state's colour
  vec3 L = normalize(vec3(-0.4, 0.8, 0.6));
  float diff = 0.5 + 0.5 * max(dot(N, L), 0.0);
  float spec = pow(max(dot(N, normalize(L + V)), 0.0), 40.0) * 0.45;
  float fres = pow(1.0 - clamp(dot(N, V), 0.0, 1.0), 2.5);
  vec3 col = base * diff + spec + mix(uBack, vec3(1.0), 0.4) * fres * (0.35 + 0.6 * uEnergy) + uBack * uEnergy * 0.12;
  col = mix(col, vec3(1.0, 0.95, 0.8), uFlash * 0.3);
  gl_FragColor = vec4(col, 1.0);
  #include <colorspace_fragment>
}`;

// a scalloped fan in the xy plane, pointing +x (tail, dorsal and side fins)
function fan(R, spread, lobes) {
  const s = new THREE.Shape();
  s.moveTo(0, 0);
  const n = 40;
  for (let i = 0; i <= n; i++) {
    const a = -spread + (2 * spread * i) / n, r = R * (0.82 + 0.18 * Math.abs(Math.sin((i / n) * Math.PI * lobes)));
    s.lineTo(Math.cos(a) * r, Math.sin(a) * r);
  }
  s.lineTo(0, 0);
  return new THREE.ShapeGeometry(s);
}

const UP = new THREE.Vector3(0, 1, 0), Z = new THREE.Vector3(0, 0, 1);

export class Listener {
  constructor(container) {
    this.el = container;
    this.state = "connecting";
    const m0 = MOODS.connecting;
    this.mood = { puff: m0.puff, energy: m0.energy, pulse: 0, talk: 0, swim: m0.swim };
    this.colBack = new THREE.Color(m0.back);   // the colours on screen now; they blend toward the state's
    this.colFin = new THREE.Color(m0.fin);
    this.colA = this.colBack;
    this.level = 0;          // the user's voice level (0..1), from the mic
    this.puff = 0;           // how inflated (0 deflated, 1 a full spiky ball)
    this.puffV = 0;
    this.reach = 0;          // how close the cursor is (0..1); it swims toward it
    this.flinch = 0;
    this.t = Math.random() * 50;
    this.clock = 0;
    this.look = { yaw: -Math.PI / 2, pitch: 0, vy: 0, vp: 0, ty: -Math.PI / 2, tp: 0 };
    this.ptr = null;
    this.frames = 0;
    this.blinkAt = performance.now() + 2500;
    this.blinkUntil = 0;
    this.reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;

    const r = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    r.setPixelRatio(Math.min(1.75, devicePixelRatio || 1));
    r.setClearColor(0x000000, 0);
    r.domElement.className = "listener";
    container.appendChild(r.domElement);
    this.renderer = r;
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(34, 1, 0.1, 50);
    this.camera.position.set(0, 0.5, 5.2);
    this.camera.lookAt(0, 0, 0);
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
    const scene = this.scene;
    scene.add(new THREE.HemisphereLight(0xeaf4ff, 0x1a2a4a, 1.4));
    const key = new THREE.DirectionalLight(0xffffff, 1.6);
    key.position.set(-2, 3, 4);
    scene.add(key);
    this.root = new THREE.Group();
    this.root.rotation.order = "YZX";   // turn, then nose up/down, then roll
    scene.add(this.root);

    // the body: a sphere stretched into a fish shape, round when puffed
    this.body = new THREE.Mesh(new THREE.SphereGeometry(1, 64, 48), new THREE.ShaderMaterial({
      vertexShader: BODY_VERT, fragmentShader: BODY_FRAG,
      uniforms: { uBack: { value: this.colBack }, uEnergy: { value: 0.2 }, uFlash: { value: 0 } },
    }));
    this.root.add(this.body);

    // spines all over (not on the face or the tail tip): flat when deflated, standing up when puffed
    const cone = new THREE.ConeGeometry(0.028, 1, 6);
    cone.translate(0, 0.5, 0);
    this.spineMat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.45 });
    this.spineDirs = [];
    const N = 220, ga = Math.PI * (3 - Math.sqrt(5));
    for (let i = 0; i < N; i++) {
      const y = 1 - ((i + 0.5) / N) * 2, rr = Math.sqrt(1 - y * y), a = i * ga;
      const d = new THREE.Vector3(Math.cos(a) * rr, y, Math.sin(a) * rr);
      if (d.x < 0.45 && d.x > -0.88) this.spineDirs.push(d);
    }
    this.spines = new THREE.InstancedMesh(cone, this.spineMat, this.spineDirs.length);
    this.root.add(this.spines);

    // fins: tail, a small dorsal fin and two side fins
    this.finMat = new THREE.MeshStandardMaterial({ color: 0xffffff, transparent: true, opacity: 0.88, side: THREE.DoubleSide, roughness: 0.4 });
    this.tail = new THREE.Group();
    this.tail.add(new THREE.Mesh(fan(0.5, 0.62, 3), this.finMat));
    this.dorsal = new THREE.Mesh(fan(0.24, 0.5, 2), this.finMat);
    this.root.add(this.tail, this.dorsal);
    this.pecs = [1, -1].map((side) => {
      const g = new THREE.Group(), m = new THREE.Mesh(fan(0.22, 0.55, 2), this.finMat);
      g.add(m);
      g.userData = { side, m };
      this.root.add(g);
      return g;
    });

    // big eyes: white, an iris in the state's colour, a pupil that follows the cursor, and a highlight
    const white = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.25 });
    this.irisMat = new THREE.MeshStandardMaterial({ color: 0x3399ff, roughness: 0.3 });
    const dark = new THREE.MeshStandardMaterial({ color: 0x05070d, roughness: 0.2 });
    const shine = new THREE.MeshBasicMaterial({ color: 0xffffff });
    const cap = (r, ang) => new THREE.SphereGeometry(r, 28, 8, 0, Math.PI * 2, 0, ang).rotateX(Math.PI / 2);
    this.eyes = [1, -1].map((side) => {
      const g = new THREE.Group(), look = new THREE.Group();
      g.add(new THREE.Mesh(new THREE.SphereGeometry(0.16, 28, 20), white));
      look.add(new THREE.Mesh(cap(0.162, 0.62), this.irisMat), new THREE.Mesh(cap(0.164, 0.36), dark));
      g.add(look);
      const hl = new THREE.Mesh(new THREE.SphereGeometry(0.024, 10, 8), shine);
      hl.position.set(-0.05, 0.07, 0.15);
      g.add(hl);
      g.userData = { look, dir: new THREE.Vector3(0.62, 0.3, 0.55 * side).normalize() };
      this.root.add(g);
      return g;
    });

    // a small round mouth
    this.mouth = new THREE.Group();
    this.mouth.add(new THREE.Mesh(new THREE.TorusGeometry(0.07, 0.026, 12, 28), new THREE.MeshStandardMaterial({ color: 0xffa3b5, roughness: 0.4 })),
                   new THREE.Mesh(new THREE.CircleGeometry(0.07, 24), new THREE.MeshBasicMaterial({ color: 0x1a0610 })));
    this.root.add(this.mouth);
    this.mouthDir = new THREE.Vector3(1, -0.2, 0).normalize();

    this.tmp = { a: new THREE.Color(), b: new THREE.Color(), white: new THREE.Color(0xffffff), m: new THREE.Matrix4(),
                 q: new THREE.Quaternion(), qi: new THREE.Quaternion(), inv: new THREE.Quaternion(), p: new THREE.Vector3(),
                 n: new THREE.Vector3(), tan: new THREE.Vector3(), sc: new THREE.Vector3(), c: new THREE.Vector3(),
                 v: new THREE.Vector3(), f: new THREE.Vector3(), fw: new THREE.Vector3(0.35, 0, 0) };
  }

  resize() {
    const w = this.el.clientWidth || 1, h = this.el.clientHeight || 1;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
  }

  pointer(e) {
    const r = this.el.getBoundingClientRect();
    this.ptr = { x: (e.clientX - (r.left + r.width / 2)) / (r.width / 2), y: (e.clientY - (r.top + r.height / 2)) / (r.height / 2) };
  }

  poke() { this.flinch = Math.max(this.flinch, 0.8); }   // startled: it puffs up
  setState(s) {
    if (!MOODS[s] || s === this.state) return;
    if (s === "interrupted") this.flinch = 1;
    this.state = s;
  }
  setLevel(v) { this.level = clamp(v, 0, 1); }

  // a point on the body's surface, and the surface normal there, for a unit direction d
  surface(d, rx, ry, rz, pos, n) {
    n.set(d.x / rx, d.y / ry, d.z / rz).normalize();
    pos.set(d.x * rx, d.y * ry, d.z * rz);
  }

  frame(now) {
    if (now - this.last < 30) return;                     // ~30 fps keeps the agent snappy
    const dt = Math.min(0.06, (now - this.last) / 1000);
    this.last = now;
    if (document.hidden || this.el.offsetParent === null) return;
    const tg = MOODS[this.state], k = 1 - Math.exp(-dt * 4), T = this.tmp;
    for (const key of ["puff", "energy", "pulse", "talk", "swim"]) this.mood[key] += (tg[key] - this.mood[key]) * k;
    // colours blend toward the state's; while you talk, the listening cyan comes through whatever the state
    const kc = 1 - Math.exp(-dt * 3.2);
    this.colBack.lerp(T.a.set(tg.back).lerp(T.b.set(MOODS.listening.back), this.level * 0.6), kc);
    this.colFin.lerp(T.a.set(tg.fin), kc);
    this.clock += dt;
    const t = (this.t += dt * (this.reduced ? 0.15 : 0.6 + this.mood.swim * 0.6));

    // the cursor: it turns to face it and swims a little toward it
    const p = this.ptr, dist = p ? Math.hypot(p.x, p.y) : 9;
    const near = p ? clamp((1.25 - dist) / 0.75, 0, 1) : 0;
    this.reach += (near - this.reach) * (1 - Math.exp(-dt * 5));
    const L = this.look;
    if (p) { L.ty = -Math.PI / 2 + clamp(p.x) * 1.1; L.tp = clamp(-p.y) * 0.3; }
    else { L.ty = -Math.PI / 2 + noise(t * 0.2 + 3) * 0.7; L.tp = noise(t * 0.17 + 7) * 0.12; }
    L.vy += (16 * (L.ty - L.yaw) - 6 * L.vy) * dt; L.yaw += L.vy * dt;
    L.vp += (16 * (L.tp - L.pitch) - 6 * L.vp) * dt; L.pitch += L.vp * dt;
    this.flinch = Math.max(0, this.flinch - dt * 1.6);

    // puffing: a bouncy spring toward the mood, your voice, a poke, a thinking pulse
    const target = Math.min(1.1, this.mood.puff + this.level * 1.1 + this.flinch * 0.7 + this.mood.pulse * 0.1 * (0.5 + 0.5 * Math.sin(t * 8)));
    this.puffV += ((target - this.puff) * 60 - this.puffV * 9) * dt;
    this.puff = clamp(this.puff + this.puffV * dt, -0.05, 1.2);
    const pf = clamp(this.puff, 0, 1);
    const talk = this.mood.talk * (0.5 + 0.5 * Math.sin(this.clock * 15));
    const s = 0.6 + 0.3 * this.puff + talk * 0.015;
    const rx = s * (1.2 - 0.2 * pf), ry = s * (0.84 + 0.16 * pf), rz = s * (0.88 + 0.12 * pf);
    this.body.scale.set(rx, ry, rz);

    const bob = Math.sin(this.clock * 1.6) * 0.06 * (1 - 0.5 * pf);
    this.root.position.set(p ? clamp(p.x) * 0.3 * this.reach : noise(t * 0.13 + 11) * 0.12, bob + (p ? -clamp(p.y) * 0.18 * this.reach : 0), 0);
    this.root.rotation.set(Math.sin(this.clock * 1.3) * 0.06, L.yaw, L.pitch + this.flinch * 0.1);

    // spines: lie back along the skin when deflated, stand straight out when puffed
    const stand = smooth(0.15, 0.85, pf), len = 0.05 + 0.2 * Math.max(0, this.puff);
    this.spineDirs.forEach((d, i) => {
      this.surface(d, rx, ry, rz, T.p, T.n);
      T.tan.set(-1, 0, 0).addScaledVector(T.n, T.n.x).normalize().lerp(T.n, stand).normalize();
      T.p.addScaledVector(T.n, -0.012);
      T.q.setFromUnitVectors(UP, T.tan);
      const w = 1 + 0.4 * pf;
      T.sc.set(w, len * (0.85 + 0.03 * ((i * 37) % 10)), w);
      T.m.compose(T.p, T.q, T.sc);
      this.spines.setMatrixAt(i, T.m);
    });
    this.spines.instanceMatrix.needsUpdate = true;

    // fins: the tail wags, the side fins flutter (faster when puffed, as real puffers do)
    this.tail.position.set(-rx * 0.88, 0, 0);
    this.tail.rotation.set(0, Math.PI + Math.sin(this.clock * (3 + 4 * this.mood.swim)) * 0.35 * (1 - 0.4 * pf), 0);
    this.dorsal.position.set(-0.2 * rx, ry * 0.88, 0);
    this.dorsal.rotation.set(Math.sin(this.clock * 4) * 0.12, 0, Math.PI / 2 + 0.5);
    for (const g of this.pecs) {
      const side = g.userData.side;
      g.position.set(0.1 * rx, -0.12 * ry, side * rz * 0.95);
      g.rotation.set(0, -side * (Math.PI / 2 + 0.55), 0);
      g.userData.m.rotation.y = Math.sin(this.clock * (8 + 6 * pf) + side) * 0.4;
    }

    // eyes follow the cursor (or you, when there is none) and blink now and then
    const cw = T.c.set(p ? clamp(p.x) : 0, p ? -clamp(p.y) : 0, 1.4).normalize();
    T.inv.copy(this.root.quaternion).invert();
    cw.applyQuaternion(T.inv);
    if (now > this.blinkAt) { this.blinkUntil = now + 140; this.blinkAt = now + 2200 + Math.random() * 3800; }
    const lid = now < this.blinkUntil ? 0.12 : 1;
    for (const e of this.eyes) {
      this.surface(e.userData.dir, rx, ry, rz, T.p, T.n);
      e.position.copy(T.p).addScaledVector(T.n, -0.05);
      e.quaternion.setFromUnitVectors(Z, T.f.copy(T.n).add(T.fw).normalize());
      e.scale.set(1, lid, 1);
      const lc = T.v.copy(cw).applyQuaternion(T.qi.copy(e.quaternion).invert());
      e.userData.look.rotation.set(-clamp(lc.y) * 0.55, clamp(lc.x) * 0.55, 0);
    }

    // the mouth: a small "o", moving while it speaks
    this.surface(this.mouthDir, rx, ry, rz, T.p, T.n);
    this.mouth.position.copy(T.p).addScaledVector(T.n, 0.005);
    this.mouth.quaternion.setFromUnitVectors(Z, T.n);
    this.mouth.scale.set(0.9 + 0.2 * pf, 0.45 + 0.25 * pf + this.mood.talk * 0.75 * (0.5 + 0.5 * Math.sin(this.clock * 16)), 1);

    // colours and glow
    const energy = clamp(this.mood.energy + this.level * 0.45 + this.reach * 0.1 + this.flinch * 0.3 + this.mood.pulse * 0.18 * Math.sin(t * 8), 0, 1);
    this.body.material.uniforms.uEnergy.value = energy;
    this.body.material.uniforms.uFlash.value = this.flinch;
    this.spineMat.color.copy(this.colBack).lerp(T.white, 0.45);
    this.finMat.color.copy(this.colFin);
    this.finMat.emissive.copy(this.colFin).multiplyScalar(0.15 + 0.25 * energy);
    this.irisMat.color.copy(this.colBack).multiplyScalar(0.8);
    this.renderer.render(this.scene, this.camera);
    this.frames++;
  }
}
