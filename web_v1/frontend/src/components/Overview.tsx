import { useEffect, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ChevronLeft, ChevronRight, X } from "lucide-react";
import { LineLabel } from "./Floor";
import type { GanttRow, OrderRow } from "../lib/api";
import { num, planDate } from "../lib/format";

/** Late under a method: unfinished, or finished after its due day. */
export function isLate(row: OrderRow, method = "CRAFT_V3") {
  const m = row.methods[method];
  if (!m) return false;
  if (!m.complete) return true;
  return m.finish_day != null && row.due_day != null && m.finish_day > row.due_day + 1e-6;
}

export function daysLate(row: OrderRow, method = "CRAFT_V3") {
  const m = row.methods[method];
  return m?.finish_day != null && row.due_day != null ? m.finish_day - row.due_day : null;
}

/** "3 min ago", kept current. */
export function useAgo(timestamp: number | undefined) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 5000); return () => clearInterval(id); }, []);
  if (!timestamp) return null;
  const s = Math.max(0, Math.round((now - timestamp) / 1000));
  return s < 10 ? "just now" : s < 60 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
}

const SHIFTS = [{ name: "Night shift", from: 22, to: 6 }, { name: "Morning shift", from: 6, to: 14 }, { name: "Evening shift", from: 14, to: 22 }];

/** The shift on the factory clock right now (shifts change at 06:00, 14:00 and 22:00). */
export function ShiftClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => { const id = setInterval(() => setNow(new Date()), 30_000); return () => clearInterval(id); }, []);
  const hours = now.getHours() + now.getMinutes() / 60;
  const shift = hours >= 22 || hours < 6 ? SHIFTS[0] : hours < 14 ? SHIFTS[1] : SHIFTS[2];
  const into = (hours - shift.from + 24) % 24;
  const left = 8 - into;
  return (
    <div className="shift-clock" aria-label={`${shift.name}, ${Math.floor(left)} hours ${Math.round((left % 1) * 60)} minutes left`}>
      <span className="clock">{now.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}</span>
      <span>{shift.name}</span>
      <span className="shift-bar" aria-hidden="true"><i style={{ width: `${(into / 8) * 100}%` }} /></span>
      <small>{Math.floor(left)} h {Math.round((left % 1) * 60)} min left</small>
    </div>
  );
}

/** One spool per order in the plan: wound in green when it finishes on time, red when late. */
export function SpoolTally({ rows, onPick }: { rows: OrderRow[]; onPick: (row: OrderRow) => void }) {
  const sorted = [...rows].sort((a, b) => (a.due_day ?? 1e9) - (b.due_day ?? 1e9));
  return (
    <ul className="spools" aria-label="Orders in this plan, by due date">
      {sorted.map((row, i) => {
        const late = isLate(row);
        return (
          <li key={row.order_id} style={{ animationDelay: `${200 + i * 60}ms` }}>
            <button type="button" className={`spool-btn ${late ? "late" : "ok"}`} onClick={() => onPick(row)}
              aria-label={`Order ${row.order_id}, ${late ? "late" : "on time"}. Show details.`}>
              <svg viewBox="0 0 24 32" aria-hidden="true">
                <rect x="3" y="1" width="18" height="4" rx="1" className="flange" />
                <rect x="6" y="5" width="12" height="22" className="thread" />
                <path d="M6 9h12M6 13h12M6 17h12M6 21h12" className="wind" />
                <rect x="3" y="27" width="18" height="4" rx="1" className="flange" />
              </svg>
              <span>{row.order_id}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

/** A Netflix-style row: cards side by side, scrolled with the buttons, a trackpad or the keyboard. */
export function Rail({ title, note, children, count }: { title: string; note?: ReactNode; children: ReactNode; count: number }) {
  const track = useRef<HTMLDivElement>(null);
  const move = (dir: number) => track.current?.scrollBy({ left: dir * track.current.clientWidth * 0.8, behavior: "smooth" });
  if (!count) return null;
  return (
    <section className="rail-row" aria-label={title}>
      <div className="rail-head">
        <h2>{title} <span className="count">{count}</span></h2>
        {note && <span className="muted small">{note}</span>}
        <span className="rail-nav">
          <button type="button" className="btn icon" onClick={() => move(-1)} aria-label={`Scroll ${title} left`}><ChevronLeft size={18} /></button>
          <button type="button" className="btn icon" onClick={() => move(1)} aria-label={`Scroll ${title} right`}><ChevronRight size={18} /></button>
        </span>
      </div>
      <div className="rail-track" ref={track}>{children}</div>
    </section>
  );
}

export function OrderCard({ row, reference, onOpen }: { row: OrderRow; reference: string; onOpen: () => void }) {
  const late = isLate(row);
  const mine = row.methods.CRAFT_V3;
  const by = daysLate(row);
  return (
    <button type="button" className={`card order-card ${late ? "late" : "ok"}`} onClick={onOpen}>
      <span className="card-top"><strong>Order {row.order_id}</strong><span className="small muted">{row.pieces?.toLocaleString() ?? "?"} pieces</span></span>
      <span className="card-figure">{late ? (mine?.complete ? `${num(by, 1)} days late` : "Not finished") : by != null && by < 0 ? `${num(-by, 1)} days early` : "On time"}</span>
      <span className="small">Due {row.due_day != null ? planDate(reference, row.due_day) : "—"}, finishes {mine?.finish_day != null ? planDate(reference, mine.finish_day) : "—"}</span>
    </button>
  );
}

/** Slide-in details of one order in the plan; Escape or the close button returns to the dashboard. */
export function OrderDrawer({ row, steps, reference, runId, onClose }:
  { row: OrderRow | null; steps: GanttRow[]; reference: string; runId?: string; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = dialog.current;
    if (!d) return;
    if (row && !d.open) d.showModal?.();
    if (!row && d.open) d.close();
  }, [row]);
  const at = (seconds: number) => new Date(new Date(reference).getTime() + seconds * 1000)
    .toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  const mine = row?.methods.CRAFT_V3;
  const late = row ? isLate(row) : false;
  return (
    <dialog ref={dialog} className="drawer" onClose={onClose} onClick={(e) => { if (e.target === dialog.current) onClose(); }}
      aria-labelledby="drawer-title">
      {row && (
        <div className="drawer-body">
          <div className="section-head">
            <h2 id="drawer-title">Order {row.order_id}</h2>
            <button type="button" className="btn icon" onClick={onClose} aria-label="Close details"><X size={18} /></button>
          </div>
          <p className={late ? "late-text" : "ontime-text"}><strong>{late ? (mine?.complete ? `${num(daysLate(row), 1)} days late` : "Not finished in this plan") : "On time"}</strong></p>
          <dl className="facts">
            <dt>Pieces</dt><dd>{row.pieces?.toLocaleString() ?? "—"}</dd>
            <dt>Released</dt><dd>{planDate(reference, row.release_day)}</dd>
            <dt>Due</dt><dd>{row.due_day != null ? planDate(reference, row.due_day) : "—"}</dd>
            <dt>Planned finish</dt><dd>{mine?.finish_day != null ? planDate(reference, mine.finish_day) : "—"}</dd>
          </dl>
          <h3>Steps in the plan</h3>
          <ol className="steps">{[...steps].sort((a, b) => a.step - b.step).map((s) => (
            <li key={s.key}><strong>{s.name ?? `Step ${s.step}`}</strong>
              <span className="small muted"><LineLabel id={s.line} />, crew of {s.crew}, {at(s.start)} to {at(s.end)}</span></li>))}</ol>
          <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>
            {runId && <Link className="btn primary" to={`/runs/${runId}`}>Open the plan</Link>}
            <Link className="btn" to={`/orders/${row.order_id}`}>Order record</Link>
          </div>
        </div>
      )}
    </dialog>
  );
}

/** A KPI tile; `part`/`whole` draws a meter under the figure (no invented history). */
export function Tile({ figure, label, part, whole, tone, to }:
  { figure: ReactNode; label: string; part?: number; whole?: number; tone?: "late" | "ontime"; to?: string }) {
  const pct = part != null && whole ? Math.max(0, Math.min(1, part / whole)) : null;
  const body = (
    <>
      <span className={`figure${tone ? ` ${tone}-text` : ""}`}>{figure}</span>
      <small>{label}</small>
      {pct != null && <span className="meter" aria-hidden="true"><i style={{ width: `${pct * 100}%` }} /></span>}
    </>
  );
  return to ? <Link className="tile" to={to}>{body}</Link> : <div className="tile">{body}</div>;
}
