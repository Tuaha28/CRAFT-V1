import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Pause, Play } from "lucide-react";
import type { GanttRow, LineRef, OrderRow, Row } from "../lib/api";
import { planDate, prettyLine } from "../lib/format";
import { useTheme } from "../app/theme";
import { FactoryMap, type Hall, type MachineState } from "./FactoryMap";
import { lineName } from "./Floor";
import type { SceneHall } from "./factory/FloorScene";
import type { LitMachine } from "./factory/scene";
import { sceneQuality } from "./factory/quality";

const FloorScene = lazy(() => import("./factory/FloorScene"));

const DAY = 86_400;
const calm = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

/** Same colour for an order everywhere it shows: the schedule, the floor and the lists. */
/** Hues 70 to 300 only (greens, teals, blues, violets): red and amber are kept for late and at risk. */
export function orderHue(index: number) { return 70 + ((index * 137.508 + 135) % 230); }
export function orderColor(index: number, theme: "dark" | "light") {
  return `hsl(${Math.round(orderHue(index))}, ${theme === "dark" ? "82%, 66%" : "72%, 42%"})`;
}

const lateText = (days: number) => { const n = Math.max(1, Math.round(days)); return `Late ${n} ${n === 1 ? "day" : "days"}`; };

/** The 3D scene when the browser can draw it, otherwise the flat factory map. */
export function FactoryDrawing({ halls, fallback, lit, belts, moving, selected, onSelect, compact, passive }: {
  halls: SceneHall[]; fallback?: Hall[];
  lit?: Map<string, LitMachine>; belts?: Map<string, string[]>; moving?: boolean;
  selected?: string | null; onSelect?: (line: string | null) => void; compact?: boolean; passive?: boolean;
}) {
  const [failed, setFailed] = useState(false);
  if (failed || sceneQuality() === "none") return fallback ? <FactoryMap halls={fallback} plain={compact} /> : null;
  return (
    <Suspense fallback={<div className={`scene${compact ? " compact" : ""}`} />}>
      <FloorScene halls={halls} lit={lit} belts={belts} moving={moving} selected={selected} onSelect={onSelect} compact={compact} passive={passive} onFail={() => setFailed(true)} />
    </Suspense>
  );
}

/**
 * Machines grouped by line, for the drawing. Machines name a line by its number ("LINE01") and plans
 * by its id (44), so the hall key is the id whenever the factory's line list knows it.
 */
export function hallsFromMachines(machines: Row[], refs: LineRef[] = []): SceneHall[] {
  const byLine = new Map<string, Row[]>();
  for (const m of machines) {
    const key = String(m.line ?? "");
    if (!key) continue;
    if (!byLine.has(key)) byLine.set(key, []);
    byLine.get(key)!.push(m);
  }
  const state = (status: unknown): MachineState => status === "Breakdown" ? "down" : status === "Under_Maintenance" ? "fix" : status === "Active" ? "run" : "idle";
  return [...byLine.entries()].sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true })).map(([number, ms]) => {
    const ref = refs.find((r) => r.line_number === number);
    return {
      key: ref ? String(ref.line_id) : number,
      name: ref ? lineName(refs, ref.line_id) : prettyLine(number),
      note: `${ms.filter((m) => m.current_status === "Active").length} of ${ms.length} running`,
      machines: ms.map((m) => ({ id: String(m.asset_id), state: state(m.current_status) })),
    };
  });
}

/**
 * The factory running a plan. Press play or drag the timeline: machines the plan
 * assigns at that moment light up in the order's colour, workers sit down at them
 * and bundles ride the belts. Without a plan it shows the machines as they are now.
 */
export function FloorView({ halls, fallback, plan, autoplay }: {
  halls: SceneHall[]; fallback?: Hall[];
  plan?: { rows: GanttRow[]; reference: string; orders: OrderRow[] };
  autoplay?: boolean;
}) {
  const theme = useTheme();
  const rows = useMemo(() => plan?.rows ?? [], [plan]);
  const endDay = useMemo(() => (rows.length ? Math.ceil(Math.max(...rows.map((r) => r.end)) / DAY) : 0), [rows]);
  const orderIds = useMemo(() => [...new Set(rows.map((r) => r.order_id))].sort((a, b) => Number(a) - Number(b)), [rows]);
  const colorOf = useCallback((order: string) => orderColor(Math.max(0, orderIds.indexOf(order)), theme), [orderIds, theme]);
  const finishes = useMemo(() => (plan?.orders ?? []).map((o) => ({ id: o.order_id, finish: o.methods.CRAFT_V3?.finish_day ?? null, due: o.due_day })).filter((o) => o.finish != null) as { id: string; finish: number; due: number | null }[], [plan]);
  const [selected, setSelected] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  // A strong computer plays the plan once when the page opens; others wait for Play.
  const [touring, setTouring] = useState(() => Boolean(autoplay) && (plan?.rows.length ?? 0) > 0 && !calm() && sceneQuality() === "high");
  const [moment, setMoment] = useState(0);  // the plan time drawn, in seconds; moves only when the running steps change
  const time = useRef(0), playingRef = useRef(false);
  const range = useRef<HTMLInputElement>(null), out = useRef<HTMLOutputElement>(null);
  const runningKey = useRef("");
  const hasPlan = rows.length > 0;

  const label = useCallback((t: number) => {
    const day = t / DAY, hours = Math.floor(((day % 1) + 1) % 1 * 24);
    const done = finishes.filter((o) => o.finish <= day).length;
    return { when: `${planDate(plan?.reference ?? "", Math.floor(day))}, ${String(hours).padStart(2, "0")}:00`, done: `${done} of ${plan?.orders.length ?? 0} orders finished` };
  }, [finishes, plan]);

  const paintClock = useCallback(() => {
    const t = time.current;
    if (range.current) { range.current.value = String(t / DAY); range.current.style.setProperty("--p", `${(t / DAY / Math.max(1, endDay)) * 100}%`); }
    if (out.current) { const l = label(t); out.current.innerHTML = `${l.when}<span>${l.done}</span>`; }
    const running = rows.filter((r) => r.start <= t && t < r.end).map((r) => r.key).join();
    if (running !== runningKey.current) { runningKey.current = running; setMoment(t); }
  }, [endDay, label, rows]);

  const moving = playing || touring;
  useEffect(() => { if (hasPlan) paintClock(); }, [hasPlan, paintClock]);
  useEffect(() => {
    if (!hasPlan || !moving) return;
    let frame = 0, last = 0;
    const loop = (ts: number) => {
      frame = requestAnimationFrame(loop);
      const dt = Math.min(0.1, (ts - (last || ts)) / 1000); last = ts;
      const day = time.current / DAY;
      time.current += dt * (day < 14 ? 0.9 : 6) * DAY;
      if (touring && time.current / DAY > Math.min(13, endDay)) { time.current = 0; setTouring(false); }
      else if (time.current / DAY >= endDay) { time.current = endDay * DAY; playingRef.current = false; setPlaying(false); }
      paintClock();
    };
    frame = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(frame);
  }, [hasPlan, moving, touring, endDay, paintClock]);

  const toggle = () => {
    if (touring) { setTouring(false); playingRef.current = true; setPlaying(true); return; }
    if (!playingRef.current && time.current / DAY >= endDay - 0.01) time.current = 0;
    playingRef.current = !playingRef.current; setPlaying(playingRef.current);
  };

  const active = useMemo(() => rows.filter((r) => r.start <= moment && moment < r.end), [rows, moment]);
  const lit = useMemo(() => {
    const map = new Map<string, LitMachine>();
    for (const r of active) for (const m of r.machines) map.set(m, { color: colorOf(r.order_id), text: `Sewing ${(r.name ?? "a step").toLowerCase()}, order ${r.order_id}` });
    return map;
  }, [active, colorOf]);
  const belts = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const r of active) { const key = String(r.line); map.set(key, [...(map.get(key) ?? []), colorOf(r.order_id)]); }
    return map;
  }, [active, colorOf]);
  const lastDone = useMemo(() => {
    const day = moment / DAY;
    return finishes.filter((o) => o.finish <= day).sort((a, b) => b.finish - a.finish)[0];
  }, [finishes, moment]);
  const shown = selected ? halls.filter((h) => h.key === selected) : halls;

  return (
    <div className="floor-stage">
      <section className="panel floor-panel" aria-label="Factory floor">
        <div className="floor-legend" aria-hidden="true">
          <span><i style={{ background: "var(--line-a)" }} />Running</span>
          <span><i style={{ background: "var(--warn)" }} />Maintenance</span>
          <span><i style={{ background: "var(--late)" }} />Breakdown</span>
          <span><i style={{ background: "var(--ink-3)" }} />Idle</span>
          {hasPlan && <span><i style={{ background: "var(--ink)" }} />Bright: sewing, in the order's colour</span>}
        </div>
        <FactoryDrawing halls={halls} fallback={fallback} lit={lit} belts={belts} moving={moving} selected={selected} onSelect={setSelected} />
        {hasPlan && (
          <div className="scrub">
            <button type="button" className="btn primary" onClick={toggle} aria-label={playing ? "Pause the plan" : "Play the plan"}>
              {playing ? <Pause size={15} aria-hidden="true" /> : <Play size={15} aria-hidden="true" />}{playing ? "Pause" : "Play"}
            </button>
            <div className="scrub-range">
              <input ref={range} type="range" min={0} max={endDay} step={0.05} defaultValue={0} aria-label="Time in the plan"
                onInput={(e) => { setTouring(false); playingRef.current = false; setPlaying(false); time.current = Number(e.currentTarget.value) * DAY; paintClock(); }} />
              <div className="scrub-marks" aria-hidden="true">{finishes.map((o) => <i key={o.id} title={`Order ${o.id} finishes`} style={{ left: `${(o.finish / Math.max(1, endDay)) * 100}%`, background: colorOf(o.id) }} />)}</div>
              <div className="scrub-axis" aria-hidden="true">{Array.from({ length: 6 }, (_, i) => Math.round((i * endDay) / 5)).map((d) => <span key={d} style={{ left: `${(d / Math.max(1, endDay)) * 100}%` }}>{planDate(plan?.reference ?? "", d)}</span>)}</div>
            </div>
            <output ref={out} aria-live="off" />
          </div>
        )}
      </section>
      <section className="panel section" aria-labelledby="now-title">
        <h2 id="now-title">{hasPlan ? "Sewing at this moment" : "Lines"}</h2>
        <div>
          {shown.map((hall) => {
            const ops = active.filter((r) => String(r.line) === hall.key);
            return (
              <div className="now-line" key={hall.key}>
                <b>{hall.name}</b>
                {!hasPlan ? <span className="muted small">{hall.note}</span> : ops.length ? (
                  <>
                    {ops.slice(0, 5).map((r) => (
                      <div className="now-op" key={r.key}><i style={{ background: colorOf(r.order_id) }} /><span>{r.name ?? "Step"}, order {r.order_id}<br />
                        <span className="muted small">Crew of {r.crew}, until {planDate(plan?.reference ?? "", r.end / DAY)}</span></span></div>))}
                    {ops.length > 5 && <span className="now-more">and {ops.length - 5} more steps</span>}
                  </>
                ) : <span className="muted small">Nothing planned at this time.</span>}
              </div>
            );
          })}
        </div>
        {lastDone && (
          <div className="just-done">
            <span className="muted small">Last to finish</span><b>Order {lastDone.id}</b>
            {lastDone.due != null && <span className={lastDone.finish > lastDone.due ? "late-text" : "ontime-text"}>
              {lastDone.finish > lastDone.due ? lateText(lastDone.finish - lastDone.due) : "On time"}</span>}
          </div>
        )}
        {hasPlan && <p className="muted small">Steps, crews and machines come from the plan.</p>}
      </section>
    </div>
  );
}
