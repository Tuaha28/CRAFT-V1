import type { GanttRow } from "../lib/api";
import { planDate } from "../lib/format";

const DAY = 86_400;

/** Planned worker-hours per line and day: where the plan loads the floor. */
export function LineLoad({ rows, reference, days = 14 }: { rows: GanttRow[]; reference: string; days?: number }) {
  const load = new Map<string, number[]>();
  for (const r of rows) {
    const line = String(r.line);
    if (!load.has(line)) load.set(line, Array(days).fill(0));
    const hours = load.get(line)!;
    // Spread each operation's crew time over the days it spans.
    for (let d = Math.floor(r.start / DAY); d < Math.min(days, Math.ceil(r.end / DAY)); d++) {
      const overlap = Math.min(r.end, (d + 1) * DAY) - Math.max(r.start, d * DAY);
      if (overlap > 0) hours[d] += (overlap / 3600) * r.crew;
    }
  }
  const lines = [...load.entries()].sort(([a], [b]) => Number(a) - Number(b));
  const peak = Math.max(1, ...lines.flatMap(([, h]) => h));
  if (!lines.length) return <p className="muted">No planned work in the first {days} days.</p>;
  return (
    <div className="table-wrap">
      <table className="heat" aria-label="Planned worker-hours per line and day">
        <thead><tr><th>Line</th>{Array.from({ length: days }, (_, d) => <th key={d} className="num">{planDate(reference, d)}</th>)}</tr></thead>
        <tbody>{lines.map(([line, hours]) => (
          <tr key={line}><th>Line {line}</th>
            {hours.map((h, d) => {
              const a = h / peak;
              return <td key={d} title={`${Math.round(h)} worker-hours`}
                style={{ background: h ? `color-mix(in srgb, var(--accent) ${Math.round(15 + 70 * a)}%, var(--surface))` : "var(--inset)",
                  color: a > 0.6 ? "var(--on-primary)" : "var(--ink)" }}>{h ? Math.round(h) : ""}</td>;
            })}</tr>))}
        </tbody>
      </table>
      <p className="muted small" style={{ marginTop: 6 }}>Numbers are planned worker-hours (operation time × crew size) per day.</p>
    </div>
  );
}
