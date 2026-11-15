import { useMemo, useState } from "react";
import { ChevronRight } from "lucide-react";
import type { GanttRow, OrderRow } from "../lib/api";
import { num, planDate } from "../lib/format";
import { useTheme } from "../app/theme";
import { orderColor } from "./FloorView";
import { standing, type Standing } from "../lib/standing";

const DAY = 86_400;
const STANDING_TEXT: Record<Standing, string> = { late: "Late", risk: "At risk", ontime: "On time", open: "Not finished" };
const STANDING_CHIP: Record<Standing, string> = { late: "bad", risk: "warn", ontime: "good", open: "bad" };

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** Every order in the plan with its due date, planned finish and whether it makes it. */
export function DeliveryBoard({ rows, reference, onPick }: { rows: OrderRow[]; reference: string; onPick: (row: OrderRow) => void }) {
  const [filter, setFilter] = useState<"all" | "late" | "risk" | "ontime">("all");
  const [all, setAll] = useState(false);
  const items = useMemo(() => {
    const order: Record<Standing, number> = { open: 0, late: 1, risk: 2, ontime: 3 };
    return rows.map((row) => ({ row, ...standing(row) })).sort((a, b) =>
      order[a.kind] - order[b.kind] || (a.spare ?? 0) - (b.spare ?? 0) || (a.row.due_day ?? 1e9) - (b.row.due_day ?? 1e9));
  }, [rows]);
  const end = useMemo(() => Math.max(1, ...items.map((i) => Math.max(i.finish ?? 0, i.row.due_day ?? 0))), [items]);
  const count = (k: Standing[]) => items.filter((i) => k.includes(i.kind)).length;
  const counts = { all: items.length, late: count(["late", "open"]), risk: count(["risk"]), ontime: count(["ontime"]) };
  const shown = items.filter((i) => filter === "all" || (filter === "late" ? i.kind === "late" || i.kind === "open" : i.kind === filter));
  const visible = all ? shown : shown.slice(0, 5);
  const pct = (day: number) => `${Math.max(0, Math.min(100, (day / end) * 100))}%`;

  return (
    <section className="panel section deliveries" aria-labelledby="dl-title">
      <div className="section-head">
        <h2 id="dl-title">Order deliveries</h2>
        <div className="tabs filter-tabs" role="group" aria-label="Show orders">
          {(["all", "late", "risk", "ontime"] as const).map((k) => (
            <button key={k} type="button" className={filter === k ? "on" : undefined} aria-pressed={filter === k} onClick={() => { setFilter(k); setAll(false); }}>
              {k === "all" ? "All" : STANDING_TEXT[k]} <span className="count-pill">{counts[k]}</span>
            </button>
          ))}
        </div>
      </div>
      {!visible.length ? <p className="muted empty-note">No orders in this group.</p> : (
        <ul className="delivery-list">
          {visible.map(({ row, kind, finish, spare }, i) => (
            <li key={row.order_id} style={{ animationDelay: `${Math.min(i, 10) * 30}ms` }}>
              <button type="button" onClick={() => onPick(row)} aria-label={`Order ${row.order_id}, ${STANDING_TEXT[kind].toLowerCase()}. Open details`}>
                <span className="dl-id"><strong>Order {row.order_id}</strong><small>{row.pieces?.toLocaleString() ?? "?"} pieces, {plural(row.steps, "step")}</small></span>
                <span className="dl-track" aria-hidden="true">
                  {finish != null && <i className={`dl-bar ${kind}`} style={{ left: pct(row.release_day), width: `calc(${pct(finish)} - ${pct(row.release_day)})` }} />}
                  {row.due_day != null && <b className="dl-due" style={{ left: pct(row.due_day) }} />}
                </span>
                <span className="dl-dates">
                  <span>Due {row.due_day != null ? planDate(reference, row.due_day) : "—"}</span>
                  <small>{finish != null ? `Finishes ${planDate(reference, finish)}` : "Does not finish in this plan"}</small>
                </span>
                <span className="dl-state">
                  <span className={`chip ${STANDING_CHIP[kind]}`}>{STANDING_TEXT[kind]}</span>
                  <small className="muted">{spare == null ? "" : spare < 0 ? `${num(-spare, 1)} days late` : `${num(spare, 1)} days spare`}</small>
                </span>
                <ChevronRight className="row-go" size={18} aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}
      {shown.length > 5 && (
        <button type="button" className="btn ghost show-more" onClick={() => setAll((v) => !v)}>{all ? "Show fewer" : `Show all ${shown.length} orders`}</button>
      )}
      <p className="muted small legend-note"><span className="legend-bar" aria-hidden="true" />Start to planned finish <span className="legend-due" aria-hidden="true" />Due date</p>
    </section>
  );
}

interface Bar { order: string; start: number; end: number; lane: number }

/** One row per line: when each order is on it, so busy and free lines show at a glance. */
export function LineTimeline({ rows, orders, reference, lineLabel, onPick }: {
  rows: GanttRow[]; orders: OrderRow[]; reference: string; lineLabel: (line: number) => string; onPick: (order: string) => void;
}) {
  const theme = useTheme();
  const orderIds = useMemo(() => [...new Set(rows.map((r) => r.order_id))].sort((a, b) => Number(a) - Number(b)), [rows]);
  const late = useMemo(() => new Set(orders.filter((o) => standing(o).kind === "late" || standing(o).kind === "open").map((o) => o.order_id)), [orders]);
  const { lines, endDay } = useMemo(() => {
    const spans = new Map<number, Map<string, { start: number; end: number }>>();
    for (const r of rows) {
      const byOrder = spans.get(r.line) ?? new Map();
      const s = byOrder.get(r.order_id);
      byOrder.set(r.order_id, s ? { start: Math.min(s.start, r.start), end: Math.max(s.end, r.end) } : { start: r.start, end: r.end });
      spans.set(r.line, byOrder);
    }
    const out = [...spans.entries()].sort(([a], [b]) => a - b).map(([line, byOrder]) => {
      // Orders that overlap in time on one line get their own lane, so no bar hides another.
      const lanesEnd: number[] = [];
      const bars: Bar[] = [...byOrder.entries()].sort((a, b) => a[1].start - b[1].start).map(([order, s]) => {
        let lane = lanesEnd.findIndex((e) => e <= s.start);
        if (lane < 0) { lane = lanesEnd.length; lanesEnd.push(0); }
        lanesEnd[lane] = s.end;
        return { order, start: s.start / DAY, end: s.end / DAY, lane };
      });
      const busy = rows.filter((r) => r.line === line).reduce((t, r) => t + (r.end - r.start), 0) / DAY;
      return { line, bars, lanes: Math.max(1, lanesEnd.length), busy };
    });
    return { lines: out, endDay: Math.max(1, Math.ceil(Math.max(0, ...rows.map((r) => r.end)) / DAY)) };
  }, [rows]);
  const step = endDay <= 10 ? 1 : endDay <= 30 ? 5 : endDay <= 90 ? 10 : endDay <= 200 ? 30 : 60;
  const ticks = Array.from({ length: Math.floor(endDay / step) + 1 }, (_, i) => i * step);
  const pct = (day: number) => `${(day / endDay) * 100}%`;

  if (!lines.length) return null;
  return (
    <section className="panel section line-timeline" aria-labelledby="tl-title">
      <div className="section-head">
        <h2 id="tl-title">Lines over the plan</h2>
        <span className="muted small">{plural(lines.length, "line")}, {plural(orderIds.length, "order")}, {planDate(reference, 0)} to {planDate(reference, endDay)}</span>
      </div>
      <div className="tl-grid" role="list">
        {lines.map(({ line, bars, lanes, busy }) => (
          <div key={line} className="tl-row" role="listitem">
            <span className="tl-name"><strong>{lineLabel(line)}</strong><small>{plural(bars.length, "order")}, {num(busy, 1)} work-days</small></span>
            <span className="tl-track" style={{ height: lanes * 11 + 10 }}>
              {ticks.map((t) => <i key={t} className="tl-tick" style={{ left: pct(t) }} />)}
              {bars.map((b, i) => (
                <button key={b.order} type="button" className={`tl-bar${late.has(b.order) ? " late" : ""}`}
                  style={{ left: pct(b.start), width: `max(4px, calc(${pct(b.end)} - ${pct(b.start)}))`, top: 5 + b.lane * 11,
                    background: orderColor(Math.max(0, orderIds.indexOf(b.order)), theme), animationDelay: `${Math.min(i, 12) * 25}ms` }}
                  title={`Order ${b.order} on ${lineLabel(line)}: ${planDate(reference, b.start)} to ${planDate(reference, b.end)}${late.has(b.order) ? ", finishes late" : ""}`}
                  aria-label={`Order ${b.order}, ${planDate(reference, b.start)} to ${planDate(reference, b.end)}${late.has(b.order) ? ", late" : ""}. Open details`}
                  onClick={() => onPick(b.order)} />
              ))}
            </span>
          </div>
        ))}
        <div className="tl-row tl-axis" aria-hidden="true">
          <span />
          <span className="tl-track">{ticks.map((t) => <small key={t} style={{ left: pct(t) }}>{planDate(reference, t)}</small>)}</span>
        </div>
      </div>
      <p className="muted small legend-note">Each bar is one order on that line, from its first step to its last. Select a bar to see the order. <span className="legend-late" aria-hidden="true" />Red outline: finishes late.</p>
    </section>
  );
}
