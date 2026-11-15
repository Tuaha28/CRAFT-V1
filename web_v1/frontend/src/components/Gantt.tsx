import { useMemo, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";
import type { GanttRow } from "../lib/api";
import { planDate } from "../lib/format";
import { applyOffsets, clashes, HOUR, orderEffects, type Offsets } from "../lib/sketch";
import { Sketch } from "./Sketch";

type Group = "worker" | "machine" | "line" | "order";
const GROUP_LABEL: Record<Group, string> = { worker: "Worker", machine: "Machine", line: "Line", order: "Order" };
const ROW_H = 22;
const LABEL_W = 110;
const ORDER_ROW_H = 38, ORDER_LABEL_W = 136;
const HEAD_H = 26;
const DAY = 86_400;

const lateText = (days: number) => { const n = Math.max(1, Math.round(days)); return `Late ${n} ${n === 1 ? "day" : "days"}`; };

/** Stable, muted colour per order (hue spread by golden angle). */
function orderColour(index: number) {
  const hue = (index * 137.508 + 205) % 360;
  return { fill: `hsl(${hue} 80% 74%)`, stroke: `hsl(${hue} 70% 58%)` };
}

/**
 * Operations on a time axis, one row per worker, machine, line or order. Each
 * bar has an edge so crews stacked on one machine row still read as separate
 * pieces of work. By order, the striped band runs from the due date to the finish.
 */
export function Gantt({ rows: planRows, reference, initialGroup = "worker", initialZoom = 24, maxHeight, dueDay, lineLabel = (line) => `Line ${line}` }:
  { rows: GanttRow[]; reference: string; initialGroup?: Group; initialZoom?: number; maxHeight?: number;
    /** How to name a line; plans know a line by its id. */
    lineLabel?: (line: number) => string;
    /** Order due days; when given, bars can be dragged (or moved with arrow keys) to sketch a change. */
    dueDay?: Record<string, number | null> }) {
  const [group, setGroup] = useState<Group>(initialGroup);
  const [zoom, setZoom] = useState(initialZoom);
  const [focus, setFocus] = useState("");
  const [history, setHistory] = useState<Offsets[]>([{}]);
  const [live, setLive] = useState<Offsets | null>(null);  // while dragging
  const drag = useRef<{ key: string; x: number; from: Offsets } | null>(null);
  const editable = Boolean(dueDay);
  const offsets = live ?? history[history.length - 1];
  const rows = useMemo(() => applyOffsets(planRows, offsets), [planRows, offsets]);
  const problems = useMemo(() => (editable ? clashes(planRows, offsets) : []), [editable, planRows, offsets]);
  const effects = useMemo(() => (dueDay ? orderEffects(planRows, offsets, dueDay) : []), [dueDay, planRows, offsets]);
  const clashing = useMemo(() => new Set(problems.flatMap((c) => [c.key, c.other ?? ""])), [problems]);
  const commit = (next: Offsets) => setHistory((h) => [...h, next]);
  const planStart = useMemo(() => new Map(planRows.map((r) => [r.key, r.start])), [planRows]);
  const shift = (from: Offsets, key: string, by: number) => {
    // Never before the plan start: earlier time is already past.
    const value = Math.max((from[key] ?? 0) + by, -(planStart.get(key) ?? 0));
    const next = { ...from };
    if (value) next[key] = value; else delete next[key];
    return next;
  };

  const orders = useMemo(() => [...new Set(rows.map((r) => r.order_id))].sort((a, b) => Number(a) - Number(b)), [rows]);
  const colours = useMemo(() => new Map(orders.map((o, i) => [o, orderColour(i)])), [orders]);
  const lanes = useMemo(() => {
    const map = new Map<string, { row: GanttRow }[]>();
    for (const row of rows) {
      const keys = group === "worker" ? row.workers : group === "machine" ? row.machines
        : group === "line" ? [String(row.line)] : [row.order_id];
      for (const key of keys) {
        if (!map.has(key)) map.set(key, []);
        map.get(key)!.push({ row });
      }
    }
    const lateFirst = group === "order" && dueDay ? (a: string, b: string) => (dueDay[a] ?? 1e9) - (dueDay[b] ?? 1e9) : () => 0;
    return [...map.entries()].sort(([a], [b]) => lateFirst(a, b) || (Number(a) - Number(b)) || a.localeCompare(b));
  }, [rows, group, dueDay]);

  if (!rows.length) return <p className="muted">This plan has no scheduled operations.</p>;
  const end = Math.max(...rows.map((r) => r.end));
  const days = Math.ceil(end / DAY) + 1;
  const byOrder = group === "order";
  const rowH = byOrder ? ORDER_ROW_H : ROW_H, labelW = byOrder ? ORDER_LABEL_W : LABEL_W;
  const scale = zoom / DAY; // px per second
  // Drag snaps to whole hours (quarter hours at the shift zoom).
  const snap = zoom >= 240 ? HOUR / 4 : HOUR;
  const onDown = (e: PointerEvent<SVGRectElement>, key: string) => {
    if (!editable || e.button !== 0) return;
    (e.target as Element).setPointerCapture(e.pointerId);
    drag.current = { key, x: e.clientX, from: offsets };
  };
  const onMove = (e: PointerEvent<SVGRectElement>) => {
    const d = drag.current;
    if (!d) return;
    const by = Math.round((e.clientX - d.x) / scale / snap) * snap;
    setLive(shift(d.from, d.key, by));
  };
  const onUp = () => {
    const d = drag.current;
    drag.current = null;
    if (d && live && live !== d.from) commit(live);
    setLive(null);
  };
  const onKey = (e: KeyboardEvent<SVGRectElement>, key: string) => {
    if (!editable || (e.key !== "ArrowLeft" && e.key !== "ArrowRight")) return;
    e.preventDefault();
    const by = (e.key === "ArrowLeft" ? -1 : 1) * (e.shiftKey ? DAY : HOUR);
    commit(shift(offsets, key, by));
  };
  const width = labelW + days * zoom + 8;
  const height = HEAD_H + lanes.length * rowH + 4;
  const step = zoom >= 40 ? 1 : zoom >= 16 ? 7 : 14;

  return (
    <div className="section">
      <div className="gantt-tools">
        <div className="field"><label htmlFor="g-group">Rows</label>
          <select id="g-group" value={group} onChange={(e) => setGroup(e.target.value as Group)}>
            {(Object.keys(GROUP_LABEL) as Group[]).map((g) => <option key={g} value={g}>{GROUP_LABEL[g]}s</option>)}
          </select></div>
        <div className="field"><label htmlFor="g-zoom">Zoom</label>
          <select id="g-zoom" value={zoom} onChange={(e) => setZoom(Number(e.target.value))}>
            <option value={8}>Whole plan</option><option value={24}>Weeks</option><option value={60}>Days</option><option value={240}>Shifts</option>
          </select></div>
        <div className="field"><label htmlFor="g-order">Trace an order</label>
          <select id="g-order" value={focus} onChange={(e) => setFocus(e.target.value)}>
            <option value="">All orders</option>
            {orders.map((o) => <option key={o} value={o}>Order {o}</option>)}
          </select></div>
        <p className="muted small" style={{ alignSelf: "center" }}>{rows.length} steps on {lanes.length} {GROUP_LABEL[group].toLowerCase()}s
          {editable && ". Drag a bar, or focus it and use the arrow keys (Shift for a day), to try a change."}</p>
      </div>
      <div className={`gantt${editable ? " editable" : ""}`} style={maxHeight ? { maxHeight } : undefined} role={editable ? "group" : "img"} aria-label={`Gantt chart by ${GROUP_LABEL[group].toLowerCase()}`}>
        <svg width={width} height={height}>
          <defs>
            <pattern id="late-hatch" width="8" height="8" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <rect className="hatch-a" width="8" height="8" /><line className="hatch-b" x1="0" y1="0" x2="0" y2="8" />
            </pattern>
          </defs>
          {Array.from({ length: Math.ceil(days / step) + 1 }, (_, i) => i * step).map((d) => (
            <g key={d}>
              <line className="grid" x1={labelW + d * zoom} x2={labelW + d * zoom} y1={HEAD_H - 6} y2={height} />
              <text className="day" x={labelW + d * zoom + 3} y={14}>{planDate(reference, d)}</text>
            </g>
          ))}
          <line x1={labelW} x2={labelW} y1={0} y2={height} stroke="var(--accent-2)" strokeWidth={2} />
          <text x={labelW + 4} y={HEAD_H - 2} className="day" style={{ fill: "var(--accent-2-ink)", fontWeight: 600 }}>plan start</text>
          {lanes.map(([key, items], i) => {
            const y = HEAD_H + i * rowH;
            const due = byOrder ? dueDay?.[key] ?? null : null;
            const finish = byOrder ? Math.max(...items.map(({ row }) => row.end)) / DAY : 0;
            const lateBy = due != null ? finish - due : null;
            return (
              <g key={key} className="lane">
                <rect className="row-hover" x={0} y={y} width={width} height={rowH} />
                {byOrder ? (
                  <>
                    <rect x={6} y={y + 8} width={10} height={10} rx={3} fill={colours.get(key)?.stroke} />
                    <text className="row-label" x={22} y={y + 17}>Order {key}</text>
                    {lateBy != null && <text className={`row-status ${lateBy > 0.05 ? "is-late" : "is-ok"}`} x={22} y={y + 32}>{lateBy > 0.05 ? lateText(lateBy) : "On time"}</text>}
                  </>
                ) : <text className="row-label" x={6} y={y + 15}>{group === "line" ? lineLabel(Number(key)) : `${GROUP_LABEL[group]} ${key}`}</text>}
                <line className="grid" x1={labelW} x2={width} y1={y + rowH} y2={y + rowH} />
                {due != null && lateBy != null && lateBy > 0.05 && (
                  <rect className="late-span" fill="url(#late-hatch)" x={labelW + Math.max(0, due) * zoom} y={y + 4} width={Math.max(2, finish * zoom - Math.max(0, due) * zoom)} height={rowH - 8} rx={5}><title>Past its due date</title></rect>
                )}
                {items.map(({ row }) => {
                  const c = colours.get(row.order_id)!;
                  const x = labelW + row.start * scale;
                  const w = Math.max(2, (row.end - row.start) * scale);
                  return (
                    <rect key={row.key + key} className={`bar${focus && focus !== row.order_id ? " dim" : ""}${offsets[row.key] ? " moved" : ""}${clashing.has(row.key) ? " clash" : ""}`}
                      style={{ animationDelay: `${Math.round((row.start / end) * 700)}ms` }}
                      x={x} y={y + (byOrder ? 10 : 3)} width={w} height={byOrder ? rowH - 20 : ROW_H - 6} rx={byOrder ? 4 : 2}
                      fill={byOrder ? (row.line % 2 ? "var(--line-b)" : "var(--line-a)") : c.fill} stroke={byOrder ? "var(--inset)" : c.stroke}
                      {...(editable ? { tabIndex: 0, role: "button", "aria-label": `Order ${row.order_id}, step ${row.step}, ${planDate(reference, row.start / DAY)}. Arrow keys move it.`,
                        onPointerDown: (e: PointerEvent<SVGRectElement>) => onDown(e, row.key), onPointerMove: onMove, onPointerUp: onUp, onPointerCancel: onUp,
                        onKeyDown: (e: KeyboardEvent<SVGRectElement>) => onKey(e, row.key) } : {})}>
                      <title>{`Order ${row.order_id}, step ${row.step}: ${row.name ?? row.key}\n${row.machine_type ?? ""}\nCrew ${row.crew}, ${row.pieces ?? "?"} pieces, ${lineLabel(row.line)}\nWorkers ${row.workers.join(", ")}\nMachines ${row.machines.join(", ")}\n${planDate(reference, row.start / DAY)} → ${planDate(reference, row.end / DAY)}`}</title>
                    </rect>
                  );
                })}
                {due != null && due >= 0 && (
                  <g className="due-pin"><title>Due {planDate(reference, due)}</title>
                    <circle cx={labelW + due * zoom} cy={y + 6} r={3.5} /><line x1={labelW + due * zoom} x2={labelW + due * zoom} y1={y + 6} y2={y + rowH - 4} strokeWidth={1.5} /></g>
                )}
              </g>
            );
          })}
        </svg>
      </div>
      {editable && history.length > 1 && (
        <Sketch rows={planRows} offsets={offsets} clashes={problems} effects={effects} reference={reference}
          onUndo={() => setHistory((h) => h.slice(0, -1))} onReset={() => setHistory([{}])} />
      )}
    </div>
  );
}
