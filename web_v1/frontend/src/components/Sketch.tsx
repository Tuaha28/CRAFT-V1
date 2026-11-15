import type { GanttRow } from "../lib/api";
import type { Clash, Offsets, OrderEffect } from "../lib/sketch";

const HOURS = (seconds: number) => {
  const minutes = Math.round(Math.abs(seconds) / 60);
  const h = Math.floor(minutes / 60), m = minutes % 60;
  const text = !m && h >= 24 && h % 24 === 0 ? `${h / 24} day${h === 24 ? "" : "s"}` : [h && `${h} h`, m && `${m} min`].filter(Boolean).join(" ");
  return `${text} ${seconds > 0 ? "later" : "earlier"}`;
};

/** Date and clock time of a plan offset in days: hour moves must stay visible. */
const at = (reference: string, day: number) => new Date(new Date(reference).getTime() + day * 86_400_000)
  .toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });

/** The list of sketched moves, what they break, and what they do to each order's finish. Nothing here is saved. */
export function Sketch({ rows, offsets, clashes, effects, reference, onUndo, onReset }:
  { rows: GanttRow[]; offsets: Offsets; clashes: Clash[]; effects: OrderEffect[]; reference: string;
    onUndo: () => void; onReset: () => void }) {
  const byKey = new Map(rows.map((r) => [r.key, r]));
  const name = (key: string) => { const r = byKey.get(key); return r ? `order ${r.order_id} step ${r.step}` : key; };
  const cap = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
  const moved = Object.entries(offsets);
  return (
    <section className="sketch" aria-live="polite" aria-label="Sketched moves">
      <div className="section-head">
        <h3>Your sketch</h3>
        <span style={{ display: "flex", gap: 8 }}>
          <button type="button" className="btn" onClick={onUndo}>Undo last move</button>
          <button type="button" className="btn" onClick={onReset}>Back to the plan</button>
        </span>
      </div>
      <p className="muted small">A sketch only: it is not saved and does not change the plan. It checks clashes and the order of steps;
        shift hours, setup and changeover are checked when CRAFT re-plans.</p>
      {moved.length > 0 && (
        <ul className="sketch-list">{moved.map(([key, by]) => <li key={key}>Moved {name(key)} {HOURS(by)}</li>)}</ul>
      )}
      {clashes.length ? (
        <ul className="alerts">{clashes.map((c, i) => (
          <li key={i}>{c.kind === "start" ? `${cap(name(c.key))} now starts before the plan start.`
            : c.kind === "sequence" ? `${cap(name(c.key))} now overlaps ${name(c.other!)}, which the plan ran after it.`
              : `${c.kind === "worker" ? "Worker" : "Machine"} ${c.resource} is now on ${name(c.key)} and ${name(c.other!)} at the same time.`}</li>))}</ul>
      ) : moved.length > 0 && <p className="small ontime-text">No clashes found with the rest of the plan.</p>}
      {effects.length > 0 && (
        <div className="table-wrap"><table className="small">
          <thead><tr><th scope="col">Order</th><th scope="col">Finish in plan</th><th scope="col">Finish in sketch</th><th scope="col">Due</th></tr></thead>
          <tbody>{effects.map((e) => (
            <tr key={e.order_id}><td>Order {e.order_id}</td>
              <td className={e.lateBefore ? "late-text" : undefined}>{at(reference, e.before)}{e.lateBefore ? ", late" : ""}</td>
              <td className={e.lateAfter ? "late-text" : "ontime-text"}>{at(reference, e.after)}{e.lateAfter ? ", late" : ", on time"}</td>
              <td>{e.due != null ? at(reference, e.due) : "—"}</td></tr>))}</tbody>
        </table></div>
      )}
    </section>
  );
}
