import type { OrderRow } from "../lib/api";
import { methodLabel, planDate } from "../lib/format";

const LABEL_W = 116;
const LANE_H = 30;
const TAPE_H = 30;

/**
 * The delivery tape: a tape measure laid from the plan start. Each order gets a
 * lane: release to planned finish as a seam (green when on time, red when late),
 * its due date as a notch, and the comparison method's finish as a hollow ring.
 */
export function DeliveryTape({ rows, reference, policy = "CRAFT_V3", compare }:
  { rows: OrderRow[]; reference: string; policy?: string; compare?: string }) {
  const finishes = rows.flatMap((r) => [r.methods[policy]?.finish_day, compare ? r.methods[compare]?.finish_day : null, r.due_day]);
  const last = Math.max(7, ...finishes.filter((v): v is number => typeof v === "number"));
  const days = Math.ceil(last / 7) * 7 + 7;
  const scale = Math.max(9, Math.min(22, 980 / days));
  const width = LABEL_W + days * scale + 24;
  const height = TAPE_H + rows.length * LANE_H + 12;
  const x = (day: number) => LABEL_W + day * scale;
  const sorted = [...rows].sort((a, b) => (a.due_day ?? 1e9) - (b.due_day ?? 1e9));

  return (
    <div className="section">
      <div className="tape" role="img" aria-label={`Due dates and planned finish of ${rows.length} orders under ${methodLabel(policy)}`}>
        <svg width={width} height={height}>
          <rect className="band" x={LABEL_W} y={4} width={days * scale} height={TAPE_H - 10} rx={2} />
          {Array.from({ length: days + 1 }, (_, d) => (
            <g key={d}>
              <line className="tick" x1={x(d)} x2={x(d)} y1={4} y2={d % 7 === 0 ? 18 : 10} strokeWidth={d % 7 === 0 ? 1.4 : 0.8} />
              {d % 7 === 0 && <text className="tick-label" x={x(d) + 3} y={TAPE_H - 9}>{d === 0 ? "start" : planDate(reference, d)}</text>}
            </g>
          ))}
          {sorted.map((row, i) => {
            const y = TAPE_H + i * LANE_H + LANE_H / 2;
            const delay = 450 + i * 70; // after the tape unrolls, one lane at a time
            const mine = row.methods[policy];
            const other = compare ? row.methods[compare] : undefined;
            const late = mine?.finish_day != null && row.due_day != null && mine.finish_day > row.due_day + 1e-6;
            const unfinished = mine && !mine.complete;
            return (
              <g key={row.order_id}>
                <title>{`Order ${row.order_id}: due ${row.due_day != null ? planDate(reference, row.due_day) : "—"}, ${methodLabel(policy)} finish ${mine?.finish_day != null ? planDate(reference, mine.finish_day) : "not planned"}${unfinished ? " (unfinished)" : ""}${compare ? `, ${methodLabel(compare)} finish ${other?.finish_day != null ? planDate(reference, other.finish_day) : "—"}` : ""}`}</title>
                <line className="lane-rule" x1={0} x2={width} y1={y + LANE_H / 2} y2={y + LANE_H / 2} />
                <text className="lane-label" x={4} y={y + 4}>Order {row.order_id}</text>
                {mine?.finish_day != null && (
                  <line className={`seam ${late || unfinished ? "span-late" : "span-ok"}`} style={{ animationDelay: `${delay}ms` }} x1={x(row.release_day)} x2={x(mine.finish_day)} y1={y} y2={y}
                    strokeWidth={3} strokeDasharray="6 3" strokeLinecap="round" />
                )}
                {row.due_day != null && <path className="due" style={{ animationDelay: `${delay}ms` }} d={`M ${x(row.due_day) - 5} ${y - 11} L ${x(row.due_day) + 5} ${y - 11} L ${x(row.due_day)} ${y - 4} Z`} />}
                {other?.finish_day != null && <circle className="finish" style={{ animationDelay: `${delay + 520}ms` }} cx={x(other.finish_day)} cy={y} r={5} fill="none" stroke="var(--ink-2)" strokeWidth={1.5} />}
                {mine?.finish_day != null && <circle className={`finish ${late || unfinished ? "finish-late" : "finish-ok"}`} style={{ animationDelay: `${delay + 600}ms` }} cx={x(mine.finish_day)} cy={y} r={4.5} />}
              </g>
            );
          })}
        </svg>
      </div>
      <div className="legend">
        <span><i style={{ background: "var(--ontime)" }} />{methodLabel(policy)}: on time</span>
        <span><i style={{ background: "var(--late)" }} />{methodLabel(policy)}: late</span>
        <span>▼ due date</span>
        {compare && <span>○ {methodLabel(compare)} finish</span>}
      </div>
    </div>
  );
}
