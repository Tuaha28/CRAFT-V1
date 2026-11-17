import * as THREE from "three";
import type { Theme } from "../../app/theme";

export type MachineState = "run" | "fix" | "down" | "idle";
export interface SceneMachine { id: string; state: MachineState }
export interface SceneLine { key: string; machines: SceneMachine[] }
/** A machine sewing right now: its order's colour and the words for its tooltip. */
export interface LitMachine { color: string; text: string }
export interface SceneOptions {
  lines: SceneLine[];
  theme: Theme;
  quality: "high" | "low";
  /** Called when this computer cannot keep up even at reduced quality. */
  onTooSlow?: () => void;
  compact?: boolean;
  /** Decoration only: no hover or picking, as on the sign-in page. */
  passive?: boolean;
  onHover?: (hit: { id: string; line: string; state: MachineState; lit?: LitMachine; x: number; y: number } | null) => void;
  onPick?: (line: string | null) => void;
  /** Called every frame with each line's label position, in pixels inside the canvas. */
  onAnchors?: (anchors: Record<string, { x: number; y: number }>) => void;
}

const PALETTES = {
  dark: {
    ground: 0x0a0c0f, grid: 0x1f2c2d, slab: 0x151a1e, belt: 0x2a3236, table: 0x6b8084, leg: 0x283034, head: 0x8fb3b0, idle: 0x3a4246, worker: 0xf0dfc8, fog: 0x0a0c0f,
    sky: 0xbfe9e3, under: 0x0e1214, hemi: 2.1, sun: 2.4, gridOpacity: 0.3, glassOpacity: 0.07, stripOpacity: 0.08, exposure: 1.1, blending: THREE.AdditiveBlending,
    lines: ["#fb923c", "#60a5fa", "#a78bfa", "#f472b6", "#a3e635", "#38bdf8"], fix: "#fbbf24", down: "#fb7185",
  },
  light: {
    ground: 0xeef1f8, grid: 0xc7d0e4, slab: 0xffffff, belt: 0xc3cde0, table: 0xdfe5f0, leg: 0x98a4bf, head: 0x5d7099, idle: 0xb9c2d4, worker: 0xb9835a, fog: 0xeef1f8,
    sky: 0xffffff, under: 0xb9c2d4, hemi: 1.75, sun: 1.5, gridOpacity: 0.7, glassOpacity: 0.09, stripOpacity: 0.12, exposure: 1, blending: THREE.NormalBlending,
    lines: ["#0d9488", "#2563eb", "#7c3aed", "#db2777", "#65a30d", "#0284c7"], fix: "#d97706", down: "#dc2626",
  },
};

/** The colour a line has in the scene and in its label. */
export function lineColor(index: number, theme: Theme) {
  const list = PALETTES[theme].lines;
  return list[index % list.length];
}

const HALL_DEPTH = 6.8, HALL_GAP = 2.8, SLOT = 1.55;
const MIN_POLAR = 0.42, MAX_POLAR = 1.28, MAX_AZIMUTH = 1.5;
const GLOW_POOL = 48, NEEDLE_POOL = 40, BUNDLES_PER_LINE = 7;
const STILL = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

interface Placed { id: string; line: string; lineIndex: number; state: MachineState; x: number; z: number; dir: number; slot: number; lit?: LitMachine; scale: number }

function glowTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const x = c.getContext("2d");
  if (x) {
    const g = x.createRadialGradient(64, 64, 2, 64, 64, 64);
    g.addColorStop(0, "rgba(255,255,255,1)"); g.addColorStop(0.25, "rgba(255,255,255,.45)"); g.addColorStop(1, "rgba(255,255,255,0)");
    x.fillStyle = g; x.fillRect(0, 0, 128, 128);
  }
  return new THREE.CanvasTexture(c);
}

/**
 * The factory in 3D: one open hall per sewing line with a belt down the middle and
 * machines on both sides. All machines of a kind share one instanced mesh, so a
 * factory with hundreds of machines stays smooth. It draws only when something
 * changes: a still factory costs the computer nothing.
 */
export class FactoryScene {
  private renderer: THREE.WebGLRenderer;
  private scene = new THREE.Scene();
  private camera = new THREE.PerspectiveCamera(30, 1.6, 0.5, 400);
  private el: HTMLElement;
  private opts: SceneOptions;
  private placed: Placed[] = [];
  private halls = new Map<string, { z: number; length: number; index: number }>();
  private themed: { hemi?: THREE.HemisphereLight; sun?: THREE.DirectionalLight; ground?: THREE.MeshStandardMaterial; grid?: THREE.GridHelper; slab?: THREE.MeshStandardMaterial; belt?: THREE.MeshStandardMaterial; table?: THREE.MeshStandardMaterial; leg?: THREE.MeshStandardMaterial; worker?: THREE.MeshStandardMaterial } = {};
  private inst: Record<"table" | "leg" | "base" | "col" | "arm" | "pole" | "lamp" | "body" | "skull", THREE.InstancedMesh>;
  private lineParts: { index: number; glass: THREE.MeshBasicMaterial; edge: THREE.LineBasicMaterial; top: THREE.LineBasicMaterial; strip: THREE.MeshBasicMaterial; stripGlow: THREE.SpriteMaterial }[] = [];
  private glows: THREE.Sprite[] = [];
  private needles: THREE.Mesh[] = [];
  private bundles: { line: string; k: number; mesh: THREE.Mesh; mat: THREE.MeshStandardMaterial; length: number; z: number }[] = [];
  private texture = glowTexture();
  private theme: Theme;
  private frame = 0;
  private scheduled = false;
  private disposed = false;
  private dirty = true;
  private moving = false;
  private lastDraw = 0;
  private lastHover = 0;
  private drawn = 0;
  private slowFrames = 0;
  private reduced = false;
  private start = 0;
  private entering: boolean;
  private visible = true;
  private ro: ResizeObserver;
  private io: IntersectionObserver;
  private az = -0.5; private pol = 0.86; private tAz = -0.5; private tPol = 0.86;
  private R0 = 40; private R = 40; private tR = 40; private fit = 1;
  private target = new THREE.Vector3(0, 0.4, 0); private targetTo = new THREE.Vector3(0, 0.4, 0);
  private drag: { x: number; y: number; az: number; pol: number; moved: number; touch: boolean } | null = null;
  private ray = new THREE.Raycaster();
  private dummy = new THREE.Object3D();
  private tmp = new THREE.Color();
  private lastMove: PointerEvent | null = null;

  constructor(el: HTMLElement, opts: SceneOptions) {
    this.el = el; this.opts = opts; this.theme = opts.theme; this.entering = !STILL() && !opts.passive;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: opts.quality === "high" ? "high-performance" : "default" });  // throws without WebGL; the caller falls back
    this.renderer.setPixelRatio(opts.quality === "high" ? Math.min(window.devicePixelRatio || 1, 1.5) : 1);
    this.renderer.toneMapping = THREE.ACESFilmicToneMapping;
    const shadows = opts.quality === "high" && !opts.compact && !opts.passive;
    this.renderer.shadowMap.enabled = shadows;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.inst = this.build(shadows);
    this.applyTheme();
    const canvas = this.renderer.domElement;
    canvas.setAttribute("aria-hidden", "true");
    el.prepend(canvas);
    if (!opts.passive) this.bind(canvas);
    this.resize();
    this.ro = new ResizeObserver(() => this.resize()); this.ro.observe(el);
    this.io = new IntersectionObserver((e) => { this.visible = e[0]?.isIntersecting ?? true; if (this.visible) this.wake(); }); this.io.observe(el);
    this.wake();
  }

  private build(shadows: boolean) {
    const s = this.scene, lines = this.opts.lines;
    this.themed.hemi = new THREE.HemisphereLight(0x7e93d8, 0x05070f, 1); s.add(this.themed.hemi);
    const sun = new THREE.DirectionalLight(0xcfd9ff, 1); sun.position.set(-14, 30, 16); sun.castShadow = shadows;
    sun.shadow.mapSize.set(1024, 1024); sun.shadow.bias = -0.0006; sun.shadow.camera.far = 140;
    this.themed.sun = sun; s.add(sun);
    s.fog = new THREE.Fog(0x070c1a, 70, 190);

    const ground = new THREE.MeshStandardMaterial({ roughness: 0.85, metalness: 0 }); this.themed.ground = ground;
    const plane = new THREE.Mesh(new THREE.PlaneGeometry(800, 800), ground); plane.rotation.x = -Math.PI / 2; plane.position.y = -0.26; plane.receiveShadow = shadows; s.add(plane);
    const grid = new THREE.GridHelper(240, 120, 0xffffff, 0xffffff); grid.position.y = -0.25; (grid.material as THREE.Material).transparent = true; this.themed.grid = grid; s.add(grid);

    this.themed.slab = new THREE.MeshStandardMaterial({ roughness: 0.7, metalness: 0.1 });
    this.themed.belt = new THREE.MeshStandardMaterial({ roughness: 0.85 });
    this.themed.table = new THREE.MeshStandardMaterial({ roughness: 0.5, metalness: 0.15 });
    this.themed.leg = new THREE.MeshStandardMaterial({ roughness: 0.7 });
    this.themed.worker = new THREE.MeshStandardMaterial({ roughness: 0.7 });

    const total = lines.length * HALL_DEPTH + Math.max(0, lines.length - 1) * HALL_GAP;
    let maxLength = 0;
    lines.forEach((line, index) => {
      const rows = Math.max(1, Math.ceil(line.machines.length / 2)), length = rows * SLOT + 1, z = -total / 2 + HALL_DEPTH / 2 + index * (HALL_DEPTH + HALL_GAP);
      maxLength = Math.max(maxLength, length);
      this.halls.set(line.key, { z, length, index });
      this.addHall(line.key, index, length, z, shadows);
      line.machines.forEach((m, i) => {
        const row = i % 2, slot = Math.floor(i / 2);
        this.placed.push({ id: m.id, line: line.key, lineIndex: index, state: m.state, x: -length / 2 + 1.05 + slot * SLOT, z: z + (row ? 1.75 : -1.75), dir: row ? 1 : -1, slot, scale: 1 });
      });
      for (let k = 0; k < BUNDLES_PER_LINE; k++) {
        const mat = new THREE.MeshStandardMaterial({ roughness: 0.55 });
        const mesh = new THREE.Mesh(new THREE.BoxGeometry(0.52, 0.2, 0.52), mat); mesh.castShadow = shadows; mesh.visible = false; s.add(mesh);
        this.bundles.push({ line: line.key, k, mesh, mat, length, z });
      }
    });
    const compact = this.opts.compact;
    this.R0 = Math.max(maxLength, total, 14) * (compact ? 1.5 : this.opts.passive ? 1.7 : 1.62);
    this.R = this.entering ? this.R0 * 1.4 : this.R0; this.tR = this.R0;
    if (sun.shadow.camera instanceof THREE.OrthographicCamera) {
      const reach = Math.max(maxLength, total) * 0.75 + 6, c = sun.shadow.camera;
      c.left = c.bottom = -reach; c.right = c.top = reach; c.updateProjectionMatrix();
    }

    const count = this.placed.length;
    const steel = new THREE.MeshStandardMaterial({ color: 0xc8d2e8, roughness: 0.3, metalness: 0.85 });
    const make = (geo: THREE.BufferGeometry, mat: THREE.Material, cast = false) => {
      const mesh = new THREE.InstancedMesh(geo, mat, Math.max(1, count));
      mesh.count = count; mesh.castShadow = cast && shadows; mesh.frustumCulled = false; s.add(mesh);
      return mesh;
    };
    const head = new THREE.MeshStandardMaterial({ roughness: 0.38, metalness: 0.2 });
    const inst = {
      table: make(new THREE.BoxGeometry(1.2, 0.06, 0.82), this.themed.table, true),
      leg: make(new THREE.BoxGeometry(1.08, 0.72, 0.08), this.themed.leg),
      base: make(new THREE.BoxGeometry(0.62, 0.1, 0.24), head, true),
      col: make(new THREE.BoxGeometry(0.15, 0.36, 0.2), head, true),
      arm: make(new THREE.BoxGeometry(0.64, 0.13, 0.19), head, true),
      pole: make(new THREE.CylinderGeometry(0.018, 0.018, 0.78, 6), this.themed.leg),
      lamp: make(new THREE.SphereGeometry(0.085, 12, 10), new THREE.MeshBasicMaterial()),
      body: make(new THREE.CylinderGeometry(0.15, 0.19, 0.68, 10), this.themed.worker, true),
      skull: make(new THREE.SphereGeometry(0.125, 12, 10), this.themed.worker),
    };
    for (let i = 0; i < GLOW_POOL; i++) {
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: this.texture, transparent: true, depthWrite: false }));
      sprite.visible = false; this.glows.push(sprite); s.add(sprite);
    }
    const needleGeo = new THREE.CylinderGeometry(0.014, 0.014, 0.2, 6);
    for (let i = 0; i < NEEDLE_POOL; i++) { const n = new THREE.Mesh(needleGeo, steel); n.visible = false; this.needles.push(n); s.add(n); }
    return inst;
  }

  private addHall(key: string, index: number, length: number, z: number, shadows: boolean) {
    const s = this.scene, group = new THREE.Group(); group.position.z = z; s.add(group);
    const slab = new THREE.Mesh(new THREE.BoxGeometry(length, 0.24, HALL_DEPTH), this.themed.slab); slab.position.y = -0.12; slab.receiveShadow = shadows; slab.userData.line = key; group.add(slab);
    const edgeMat = new THREE.LineBasicMaterial(); const edge = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(length + 0.02, 0.26, HALL_DEPTH + 0.02)), edgeMat); edge.position.y = -0.115; group.add(edge);
    const glass = new THREE.MeshBasicMaterial({ transparent: true, side: THREE.DoubleSide, depthWrite: false });
    const back = new THREE.Mesh(new THREE.PlaneGeometry(length, 2.6), glass); back.position.set(0, 1.3, -HALL_DEPTH / 2); group.add(back);
    const side = new THREE.Mesh(new THREE.PlaneGeometry(HALL_DEPTH, 2.6), glass); side.position.set(-length / 2, 1.3, 0); side.rotation.y = Math.PI / 2; group.add(side);
    const top = new THREE.LineBasicMaterial({ transparent: true, opacity: 0.75 });
    group.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(-length / 2, 2.6, HALL_DEPTH / 2), new THREE.Vector3(-length / 2, 2.6, -HALL_DEPTH / 2), new THREE.Vector3(length / 2, 2.6, -HALL_DEPTH / 2)]), top));
    const belt = new THREE.Mesh(new THREE.BoxGeometry(Math.max(0.5, length - 0.8), 0.16, 0.9), this.themed.belt); belt.position.y = 0.08; belt.receiveShadow = shadows; group.add(belt);
    const strip = new THREE.MeshBasicMaterial(); const stripMesh = new THREE.Mesh(new THREE.BoxGeometry(Math.max(0.5, length - 1.4), 0.05, 0.16), strip); stripMesh.position.y = 3.05; group.add(stripMesh);
    const stripGlow = new THREE.SpriteMaterial({ map: this.texture, transparent: true, depthWrite: false }); const g = new THREE.Sprite(stripGlow); g.position.y = 3; g.scale.set(length * 1.15, 5, 1); group.add(g);
    for (const side2 of [-1, 1]) {
      const post = new THREE.Mesh(new THREE.BoxGeometry(0.07, 3.05, 0.07), this.themed.leg); post.position.set(side2 * Math.max(0.2, length / 2 - 0.7), 1.52, 0); group.add(post);
    }
    this.lineParts.push({ index, glass, edge: edgeMat, top, strip, stripGlow });
  }

  /** Which machines are sewing and what each belt carries. */
  setLit(lit: Map<string, LitMachine>, belts: Map<string, string[]>) {
    for (const m of this.placed) m.lit = lit.get(m.id);
    this.paintMachines();
    for (const b of this.bundles) {
      const colors = belts.get(b.line) ?? [];
      b.mesh.visible = colors.length > 0;
      if (colors.length) { const c = this.tmp.set(colors[b.k % colors.length]); b.mat.color.copy(c); b.mat.emissive.copy(c); b.mat.emissiveIntensity = this.theme === "dark" ? 0.28 : 0.1; }
      b.mesh.position.set(-b.length / 2 + 0.9 + (b.k / BUNDLES_PER_LINE) * Math.max(0.1, b.length - 1.8), 0.27, b.z);
    }
    this.invalidate();
  }

  setTheme(theme: Theme) { this.theme = theme; this.applyTheme(); this.invalidate(); }

  /** Belts and needles move only while the plan is playing. */
  setMoving(on: boolean) { this.moving = on && !STILL(); this.invalidate(); }

  private invalidate() { this.dirty = true; this.wake(); }

  private wake() {
    if (this.scheduled || this.disposed) return;
    this.scheduled = true;
    this.frame = requestAnimationFrame(this.tick);
  }

  focus(line: string | null) {
    const hall = line ? this.halls.get(line) : undefined;
    this.targetTo.set(0, 0.4, hall ? hall.z : 0);
    this.tR = hall ? this.R0 * 0.7 : this.R0;
    this.wake();
  }

  private applyTheme() {
    const p = PALETTES[this.theme], t = this.themed;
    this.renderer.toneMappingExposure = p.exposure;
    if (t.hemi) { t.hemi.intensity = p.hemi; t.hemi.color.setHex(p.sky); t.hemi.groundColor.setHex(p.under); }
    if (t.sun) t.sun.intensity = p.sun;
    t.ground?.color.setHex(p.ground); t.slab?.color.setHex(p.slab); t.belt?.color.setHex(p.belt); t.table?.color.setHex(p.table); t.leg?.color.setHex(p.leg); t.worker?.color.setHex(p.worker);
    (this.scene.fog as THREE.Fog).color.setHex(p.fog);
    if (t.grid) { const m = t.grid.material as THREE.LineBasicMaterial; m.color.setHex(p.grid); m.opacity = p.gridOpacity; m.vertexColors = false; m.needsUpdate = true; }
    for (const part of this.lineParts) {
      const c = p.lines[part.index % p.lines.length];
      part.glass.color.set(c); part.glass.opacity = p.glassOpacity; part.edge.color.set(c); part.top.color.set(c);
      part.strip.color.set(c).lerp(this.tmp.set(0xffffff), 0.35);
      part.stripGlow.color.set(c); part.stripGlow.opacity = p.stripOpacity; part.stripGlow.blending = p.blending; part.stripGlow.needsUpdate = true;
    }
    this.placeMachines();
    this.paintMachines();
  }

  private paintMachines() {
    const p = PALETTES[this.theme], arm = this.inst.arm, base = this.inst.base, col = this.inst.col, lamp = this.inst.lamp;
    let glow = 0, needle = 0;
    this.placed.forEach((m, i) => {
      const line = p.lines[m.lineIndex % p.lines.length];
      const headColor = m.lit ? m.lit.color : m.state === "fix" ? p.fix : m.state === "down" ? p.down : m.state === "idle" ? p.idle : p.head;
      const lampColor = m.lit ? m.lit.color : m.state === "fix" ? p.fix : m.state === "down" ? p.down : m.state === "idle" ? p.idle : line;
      this.tmp.set(headColor); arm.setColorAt(i, this.tmp); base.setColorAt(i, this.tmp); col.setColorAt(i, this.tmp);
      this.tmp.set(lampColor); lamp.setColorAt(i, this.tmp);
      const showGlow = Boolean(m.lit) || m.state === "fix" || m.state === "down";
      if (showGlow && glow < this.glows.length) {
        const g = this.glows[glow++], mat = g.material;
        mat.color.set(lampColor); mat.blending = p.blending; mat.opacity = this.theme === "dark" ? 0.75 : 0.5; mat.needsUpdate = true;
        g.position.set(m.x + (m.lit ? 0 : 0.5), m.lit ? 1.1 : 1.6, m.z - (m.lit ? 0 : m.dir * 0.34));
        const size = m.lit ? 3 : 1; g.scale.set(size, size, 1); g.visible = true; g.userData.pulse = !m.lit;
      }
      if (m.lit && needle < this.needles.length) { const n = this.needles[needle++]; n.position.set(m.x - 0.2, 1.1, m.z); n.visible = true; }
    });
    for (let i = glow; i < this.glows.length; i++) this.glows[i].visible = false;
    for (let i = needle; i < this.needles.length; i++) this.needles[i].visible = false;
    for (const mesh of [arm, base, col, lamp]) if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    this.placeWorkers();
  }

  private setMatrix(mesh: THREE.InstancedMesh, i: number, x: number, y: number, z: number, scale: number) {
    const d = this.dummy; d.position.set(x, y, z); d.scale.setScalar(Math.max(0.0001, scale)); d.updateMatrix(); mesh.setMatrixAt(i, d.matrix);
  }

  private placeMachines() {
    const { table, leg, base, col, arm, pole, lamp } = this.inst;
    this.placed.forEach((m, i) => {
      const s = m.scale, lift = (1 - s) * 1.6, y = (v: number) => lift + v * s, dx = (v: number) => m.x + v * s, dz = (v: number) => m.z + v * s;
      this.setMatrix(table, i, m.x, y(0.76), m.z, s);
      this.setMatrix(leg, i, m.x, y(0.36), dz(-m.dir * 0.22), s);
      this.setMatrix(base, i, m.x, y(0.84), m.z, s);
      this.setMatrix(col, i, dx(0.23), y(1.06), m.z, s);
      this.setMatrix(arm, i, m.x, y(1.27), m.z, s);
      this.setMatrix(pole, i, dx(0.5), y(1.17), dz(-m.dir * 0.34), s);
      this.setMatrix(lamp, i, dx(0.5), y(1.6), dz(-m.dir * 0.34), s);
    });
    for (const mesh of [table, leg, base, col, arm, pole, lamp]) mesh.instanceMatrix.needsUpdate = true;
    this.placeWorkers();
  }

  private placeWorkers() {
    const { body, skull } = this.inst;
    this.placed.forEach((m, i) => {
      const scale = m.lit ? m.scale : 0;
      this.setMatrix(body, i, m.x - 0.05, 0.58, m.z + m.dir * 0.68, scale);
      this.setMatrix(skull, i, m.x - 0.05, 1.07, m.z + m.dir * 0.68, scale);
    });
    body.instanceMatrix.needsUpdate = true; skull.instanceMatrix.needsUpdate = true;
  }

  private resize() {
    const w = this.el.clientWidth || 600, h = this.el.clientHeight || 400;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h; this.camera.updateProjectionMatrix();
    this.fit = Math.max(1, 1.55 / (w / h));
    this.invalidate();
  }

  private pointer(e: PointerEvent) {
    const r = this.renderer.domElement.getBoundingClientRect();
    return new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
  }

  private hit(e: PointerEvent): { machine?: Placed; line?: string } | null {
    this.ray.setFromCamera(this.pointer(e), this.camera);
    const found = this.ray.intersectObject(this.inst.table)[0] ?? this.ray.intersectObject(this.inst.arm)[0];
    if (found && found.instanceId !== undefined) return { machine: this.placed[found.instanceId] };
    const floor = this.ray.intersectObjects(this.scene.children.filter((c) => c instanceof THREE.Group), true).find((h) => h.object.userData.line);
    return floor ? { line: floor.object.userData.line as string } : null;
  }

  private bind(canvas: HTMLCanvasElement) {
    canvas.addEventListener("pointerdown", (e) => {
      this.drag = { x: e.clientX, y: e.clientY, az: this.tAz, pol: this.tPol, moved: 0, touch: e.pointerType === "touch" };
      if (e.pointerType !== "touch") canvas.setPointerCapture(e.pointerId);
      this.el.querySelector(".scene-hint")?.classList.add("gone");
    });
    canvas.addEventListener("pointermove", (e) => {
      const d = this.drag;
      if (!d) { if (e.pointerType !== "touch" && e.timeStamp - this.lastHover > 80) { this.lastHover = e.timeStamp; this.lastMove = e; this.hover(); } return; }
      const dx = e.clientX - d.x, dy = e.clientY - d.y;
      d.moved = Math.max(d.moved, Math.abs(dx) + Math.abs(dy));
      this.tAz = Math.max(-MAX_AZIMUTH, Math.min(MAX_AZIMUTH, d.az - dx * 0.006));
      if (!d.touch) this.tPol = Math.max(MIN_POLAR, Math.min(MAX_POLAR, d.pol - dy * 0.004));
      this.opts.onHover?.(null);
      this.wake();
    });
    canvas.addEventListener("pointerup", (e) => {
      const d = this.drag; this.drag = null;
      if (!d || d.moved > 6) return;
      const h = this.hit(e);
      this.opts.onPick?.(h?.machine?.line ?? h?.line ?? null);
    });
    canvas.addEventListener("pointercancel", () => { this.drag = null; });
    canvas.addEventListener("pointerleave", () => { this.lastMove = null; this.opts.onHover?.(null); });
  }

  private hover() {
    const e = this.lastMove; if (!e || this.drag) return;
    this.lastMove = null;
    const h = this.hit(e), m = h?.machine, canvas = this.renderer.domElement;
    canvas.style.cursor = h ? "pointer" : "grab";
    this.opts.onHover?.(m ? { id: m.id, line: m.line, state: m.state, lit: m.lit, x: e.clientX, y: e.clientY } : null);
  }

  private tick = (ts: number) => {
    this.scheduled = false;
    if (!this.visible || this.disposed) return;
    if (!this.start) this.start = ts;
    const calm = STILL();
    const settling = Boolean(this.drag) || Math.abs(this.tAz - this.az) > 0.002 || Math.abs(this.tPol - this.pol) > 0.002
      || Math.abs(this.tR * this.fit - this.R) > 0.02 || this.target.distanceTo(this.targetTo) > 0.01;
    const animating = this.entering || this.moving;
    if (!settling && !animating && !this.dirty) return;  // nothing changed: stop until something does

    // A moving belt does not need every frame; camera moves stay smooth.
    const gap = ts - this.lastDraw;
    if (animating && !settling && !this.dirty && gap < (this.opts.quality === "high" ? 30 : 48)) { this.wake(); return; }

    if (this.entering) {
      let done = true;
      for (const m of this.placed) {
        const k = Math.max(0, Math.min(1, (ts - this.start - m.slot * 42 - m.lineIndex * 180) / 520));
        m.scale = 1 - Math.pow(1 - k, 3);
        if (k < 1) done = false;
      }
      this.placeMachines();
      if (done) this.entering = false;
    }
    if (calm) { this.az = this.tAz; this.pol = this.tPol; this.R = this.tR * this.fit; this.target.copy(this.targetTo); }
    else {
      this.az += (this.tAz - this.az) * 0.14; this.pol += (this.tPol - this.pol) * 0.14;
      this.R += (this.tR * this.fit - this.R) * 0.08;
      this.target.lerp(this.targetTo, 0.1);
    }
    const s = Math.sin(this.pol);
    this.camera.position.set(this.target.x + this.R * s * Math.sin(this.az), this.target.y + this.R * Math.cos(this.pol), this.target.z + this.R * s * Math.cos(this.az));
    this.camera.lookAt(this.target);
    if (this.moving) {
      for (const n of this.needles) if (n.visible) n.position.y = 1.1 + Math.sin(ts / 42 + n.position.x) * 0.04;
      for (const g of this.glows) if (g.visible && g.userData.pulse) g.material.opacity = (this.theme === "dark" ? 0.5 : 0.35) + 0.35 * Math.sin(ts / 420 + g.position.x);
      for (const b of this.bundles) if (b.mesh.visible) {
        const u = (ts / 9000 + b.k / BUNDLES_PER_LINE) % 1;
        b.mesh.position.set(-b.length / 2 + 0.9 + u * Math.max(0.1, b.length - 1.8), 0.27, b.z);
      }
    }
    if (this.opts.onAnchors) {
      const W = this.el.clientWidth, H = this.el.clientHeight, out: Record<string, { x: number; y: number }> = {};
      for (const [key, hall] of this.halls) {
        const p = new THREE.Vector3(-hall.length / 2 - 0.4, 0.6, hall.z).project(this.camera);
        out[key] = { x: ((p.x + 1) / 2) * W, y: ((1 - p.y) / 2) * H };
      }
      this.opts.onAnchors(out);
    }
    this.renderer.render(this.scene, this.camera);
    this.watchSpeed(gap, animating);
    this.lastDraw = ts; this.dirty = false;
    if (settling || this.entering || this.moving) this.wake();
  };

  /** If frames stay slow while moving, draw less; if that is not enough, hand over to the flat map. */
  private watchSpeed(gap: number, animating: boolean) {
    this.drawn++;
    if (!animating || this.drawn < 12 || gap > 1000) return;
    this.slowFrames = gap > 110 ? this.slowFrames + 1 : Math.max(0, this.slowFrames - 1);
    if (this.slowFrames < 20) return;
    this.slowFrames = 0;
    if (!this.reduced) {
      this.reduced = true;
      this.renderer.setPixelRatio(0.75);
      this.renderer.shadowMap.enabled = false;
      this.resize();
    } else this.opts.onTooSlow?.();
  }

  dispose() {
    this.disposed = true;
    cancelAnimationFrame(this.frame);
    this.ro.disconnect(); this.io.disconnect();
    this.scene.traverse((o) => {
      const m = o as THREE.Mesh;
      m.geometry?.dispose();
      const mat = m.material as THREE.Material | THREE.Material[] | undefined;
      if (Array.isArray(mat)) mat.forEach((x) => x.dispose()); else mat?.dispose();
    });
    this.texture.dispose();
    this.renderer.domElement.remove();
    this.renderer.dispose();
    this.renderer.forceContextLoss();
  }
}
