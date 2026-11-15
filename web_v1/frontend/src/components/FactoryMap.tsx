import { Link } from "react-router-dom";
import { useTheme } from "../app/theme";

export type MachineState = "run" | "fix" | "down" | "idle";
export interface Hall {
  key: string; name: string; to: string; machines: { id: string; state: MachineState }[];
  tone: string; toneColor: string; figure: string; note: string;
}

const S = 7;  // px per unit; x runs right-back, y left-back, z up
const COLS = 7;
type Fill = Record<MachineState | "floor" | "wall" | "store" | "roof", [string, string, string]>;
const LIGHT_FILL: Fill = {
  run: ["#99f6e4", "#14b8a6", "#0d9488"], fix: ["#fde68a", "#f59e0b", "#d97706"], down: ["#fca5a5", "#ef4444", "#dc2626"],
  idle: ["#cbd3e3", "#aeb8cd", "#98a4bd"], floor: ["#ffffff", "#e4e9f4", "#d6dcea"], wall: ["#eef1f8", "#dfe5f2", "#d2d9e9"],
  store: ["#d6dcea", "#c3cce0", "#b2bcd4"], roof: ["#aeb8cd", "#98a4bd", "#8794b0"],
};
const DARK_FILL: Fill = {
  run: ["#99f6e4", "#2dd4bf", "#14b8a6"], fix: ["#fde68a", "#fbbf24", "#d97706"], down: ["#fecaca", "#f87171", "#dc2626"],
  idle: ["#353e44", "#2a3237", "#22292d"], floor: ["#1a2024", "#141a1d", "#101518"], wall: ["#262e33", "#1e252a", "#192024"],
  store: ["#2f383d", "#262e33", "#20272b"], roof: ["#3d484e", "#2f383d", "#283035"],
};

const iso = (x: number, y: number, z: number): [number, number] => [(x - y) * 0.866 * S, (x + y) * 0.5 * S - z * S];

interface Face { points: [number, number][]; fill: string; cls: string; delay?: number }

function box(out: Face[], x: number, y: number, z: number, w: number, d: number, h: number, c: [string, string, string], cls = "", delay?: number) {
  out.push({ points: [iso(x, y + d, z), iso(x + w, y + d, z), iso(x + w, y + d, z + h), iso(x, y + d, z + h)], fill: c[1], cls, delay });
  out.push({ points: [iso(x + w, y, z), iso(x + w, y + d, z), iso(x + w, y + d, z + h), iso(x + w, y, z + h)], fill: c[2], cls, delay });
  out.push({ points: [iso(x, y, z + h), iso(x + w, y, z + h), iso(x + w, y + d, z + h), iso(x, y + d, z + h)], fill: c[0], cls, delay });
}

/**
 * The factory drawn in 3D: one open hall per sewing line with its machines
 * (blue in use, amber maintenance, red breakdown, dark idle) and the materials
 * store. Each bubble links to that line's page; `plain` draws the halls only.
 */
export function FactoryMap({ halls, store, plain }: { halls: Hall[]; store?: { to: string; note: string }; plain?: boolean }) {
  const FILL = useTheme() === "light" ? LIGHT_FILL : DARK_FILL;
  const faces: Face[] = [];
  const anchors: { hall: Hall | null; at: [number, number] }[] = [];
  let y = 0;
  halls.forEach((hall, hallIndex) => {
    const rows = Math.max(1, Math.ceil(hall.machines.length / COLS));
    const w = 4 + COLS * 5, d = 3 + rows * 5;
    box(faces, 0, y, 0, w, d, 1, FILL.floor);
    box(faces, 0, y, 1, w, 0.8, 6, FILL.wall);
    box(faces, 0, y, 1, 0.8, d, 6, FILL.wall);
    hall.machines.forEach((m, i) => {
      const r = Math.floor(i / COLS), c = i % COLS;
      // Machines light up once, line by line, when the map opens.
      box(faces, 4 + c * 5, y + 3 + r * 5, 1, 3, 2, 2, FILL[m.state], "m-on", hallIndex * 450 + i * 18);
    });
    anchors.push({ hall, at: iso(0, y + d / 2, 7) });  // above its own left wall, clear of the hall behind
    y += d + 8;
  });
  if (store) {
    box(faces, 46, 0, 0, 14, 12, 10, FILL.store);
    box(faces, 47, 1, 10, 12, 10, 0.6, FILL.roof);
    anchors.push({ hall: null, at: iso(53, 6, 17) });
  }
  const xs = faces.flatMap((f) => f.points.map((p) => p[0])), ys = faces.flatMap((f) => f.points.map((p) => p[1]));
  const pad = 24, top = plain ? pad : 70;  // room above for the bubbles
  const minX = Math.min(...xs) - pad, minY = Math.min(...ys) - top, width = Math.max(...xs) - minX + pad, height = Math.max(...ys) - minY + pad;
  const pct = ([px, py]: [number, number]) => ({ left: `${((px - minX) / width) * 100}%`, top: `${((py - minY) / height) * 100}%` });
  const label = halls.map((h) => `${h.name}: ${h.tone}`).join("; ");

  return (
    <div className="factory-map">
      <svg viewBox={`${minX} ${minY} ${width} ${height}`} width="100%" role="img" aria-label={`3D map of the factory. ${label}`}>
        {faces.map((f, i) => <polygon key={i} className={f.cls || undefined} style={f.delay !== undefined ? { animationDelay: `${f.delay}ms` } : undefined} points={f.points.map((p) => p.map((v) => v.toFixed(1)).join(",")).join(" ")}
          fill={f.fill} stroke="var(--rule)" strokeWidth={1} />)}
      </svg>
      {!plain && anchors.map(({ hall, at }, i) => hall ? (
        <Link key={hall.key} to={hall.to} className="map-bubble" style={{ ...pct(at), animationDelay: `${halls.length * 450 + i * 120}ms`, borderColor: hall.toneColor }}>
          <span className="map-bubble-top"><strong>{hall.name}</strong><span style={{ color: hall.toneColor }}>{hall.tone}</span></span>
          <span className="map-bubble-figure">{hall.figure}</span>
          <span className="map-bubble-note">{hall.note}</span>
        </Link>
      ) : store && (
        <Link key="store" to={store.to} className="map-bubble" style={{ ...pct(at), animationDelay: `${halls.length * 450 + i * 120}ms` }}>
          <span className="map-bubble-top"><strong>Materials store</strong><span>Read only</span></span>
          <span className="map-bubble-figure">Stock and issues</span>
          <span className="map-bubble-note">{store.note}</span>
        </Link>
      ))}
    </div>
  );
}
