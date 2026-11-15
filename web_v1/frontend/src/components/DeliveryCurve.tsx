import { useMemo } from "react";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { OrderRow } from "../lib/api";
import { methodLabel, planDate } from "../lib/format";

/**
 * Orders delivered over time: for each day of the plan, how many orders are due
 * by then and how many each method has finished by then. Where a method's line
 * sits under the due line, that many orders are running late on that day.
 */
export function DeliveryCurve({ rows, reference, policy = "CRAFT_V3", compare }:
  { rows: OrderRow[]; reference: string; policy?: string; compare?: string }) {
  const data = useMemo(() => {
    const finish = (row: OrderRow, method: string) => {
      const m = row.methods[method];
      return m?.complete && m.finish_day != null ? m.finish_day : null;
    };
    const ends = rows.flatMap((r) => [r.due_day, finish(r, policy), compare ? finish(r, compare) : null]).filter((v): v is number => v != null);
    const last = Math.ceil(Math.max(7, ...ends)) + 1;
    const by = (day: number, value: (r: OrderRow) => number | null) => rows.filter((r) => { const v = value(r); return v != null && v <= day; }).length;
    return Array.from({ length: last + 1 }, (_, day) => ({
      day, label: planDate(reference, day),
      due: by(day, (r) => r.due_day),
      mine: by(day, (r) => finish(r, policy)),
      other: compare ? by(day, (r) => finish(r, compare)) : 0,
    }));
  }, [rows, reference, policy, compare]);

  if (!rows.length) return null;
  const stepY = Math.max(1, Math.ceil(rows.length / 5));
  const yTicks = Array.from({ length: Math.floor(rows.length / stepY) + 1 }, (_, i) => i * stepY);
  if (yTicks[yTicks.length - 1] !== rows.length) yTicks.push(rows.length);
  const weekly = data.filter((d) => d.day % 7 === 0 || d.day === data.length - 1);
  return (
    <div className="section">
      <div className="curve" role="img" aria-label={`Orders due and finished by each day, ${methodLabel(policy)}${compare ? ` against ${methodLabel(compare)}` : ""}`}>
        <ResponsiveContainer width="100%" height={240}>
          <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: -18 }}>
            <CartesianGrid vertical={false} stroke="var(--rule)" />
            <XAxis dataKey="label" interval="preserveStartEnd" minTickGap={28} tick={{ fill: "var(--ink-2)", fontSize: 11 }} stroke="var(--rule-strong)" />
            <YAxis allowDecimals={false} domain={[0, rows.length]} ticks={yTicks} tick={{ fill: "var(--ink-2)", fontSize: 11 }} stroke="var(--rule-strong)" />
            <Tooltip cursor={{ stroke: "var(--rule-strong)" }}
              content={({ active, payload, label }) => active && payload?.length ? (
                <div className="chart-tip">
                  <strong>By {label}</strong>
                  {payload.map((p) => <span key={String(p.dataKey)}><i style={{ background: String(p.color) }} />{p.name}<b>{String(p.value)}</b></span>)}
                </div>) : null} />
            <Line type="stepAfter" dataKey="due" name="Due" stroke="var(--ink-2)" strokeWidth={2} strokeDasharray="5 4" dot={false} isAnimationActive={false} />
            {compare && <Line type="stepAfter" dataKey="other" name={`${methodLabel(compare)} finished`} stroke="var(--series-compare)" strokeWidth={2} dot={false} isAnimationActive={false} />}
            <Line type="stepAfter" dataKey="mine" name={`${methodLabel(policy)} finished`} stroke="var(--series-craft)" strokeWidth={2} dot={false}
              isAnimationActive={!matchMedia("(prefers-reduced-motion: reduce)").matches} animationDuration={1400} animationEasing="ease-out" />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <div className="legend chart-legend" aria-hidden="true">
        <span><i className="dash" />Orders due</span>
        {compare && <span><i style={{ background: "var(--series-compare)" }} />{methodLabel(compare)} finished</span>}
        <span><i style={{ background: "var(--series-craft)" }} />{methodLabel(policy)} finished</span>
      </div>
      <details>
        <summary className="small">Show as a table</summary>
        <div className="table-wrap"><table className="small">
          <thead><tr><th scope="col">By</th><th scope="col">Due</th><th scope="col">{methodLabel(policy)} finished</th>{compare && <th scope="col">{methodLabel(compare)} finished</th>}</tr></thead>
          <tbody>{weekly.map((d) => <tr key={d.day}><td>{d.label}</td><td>{d.due}</td><td>{d.mine}</td>{compare && <td>{d.other}</td>}</tr>)}</tbody>
        </table></div>
      </details>
    </div>
  );
}
