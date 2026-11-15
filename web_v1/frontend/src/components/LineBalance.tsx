import { useMemo, useState } from "react";
import type { Row } from "../lib/api";
import { num } from "../lib/format";
import { Meter } from "./DataTable";
import { Measure, Notice } from "./ui";

export interface Balance {
  takt: number; targetPerDay: number; balancedPerDay: number; efficiency: number; bottleneck: number;
  steps: { step: number; name: string; sam: number; ideal: number; operators: number; cycle: number }[];
}

/** Classic line balancing for one style on one line.
 *  Takt = SAM / (operators × efficiency). Every step starts with one operator and
 *  each further operator goes to the currently slowest step, which minimises the
 *  bottleneck; the slowest step sets the balanced output; balance efficiency =
 *  SAM / (operators × slowest step time). Ideal operators = SAM share × operators. */
export function balance(routing: { step: number; name: string; sam: number }[], operators: number, minutes: number, efficiency: number): Balance | null {
  const total = routing.reduce((s, r) => s + r.sam, 0);
  if (!routing.length || total <= 0 || operators < routing.length || efficiency <= 0) return null;
  const ideal = routing.map((r) => (r.sam * operators) / total);
  const alloc = routing.map(() => 1);
  for (let left = operators - routing.length; left > 0; left--) {
    let slowest = 0;
    routing.forEach((r, i) => { if (r.sam / alloc[i] > routing[slowest].sam / alloc[slowest]) slowest = i; });
    alloc[slowest] += 1;
  }
  const steps = routing.map((r, i) => ({ ...r, ideal: ideal[i], operators: alloc[i], cycle: r.sam / alloc[i] / efficiency }));
  const slowest = Math.max(...steps.map((s) => s.cycle));
  return {
    takt: total / (operators * efficiency), targetPerDay: (operators * minutes * efficiency) / total,
    balancedPerDay: minutes / slowest, efficiency: (total / (operators * slowest * efficiency)) * 100,
    bottleneck: steps.findIndex((s) => s.cycle === slowest), steps,
  };
}

export function LineBalance({ routing }: { routing: Row[] }) {
  const steps = useMemo(() => routing.map((r) => ({ step: Number(r.step), name: String(r.operation_name), sam: Number(r.sam) })), [routing]);
  const [operators, setOperators] = useState(Math.max(steps.length, 30));
  const [minutes, setMinutes] = useState(480);
  const [efficiency, setEfficiency] = useState(78);
  const b = balance(steps, operators, minutes, efficiency / 100);
  return (
    <section className="panel section">
      <h2>Line balancing</h2>
      <p className="muted small">How many operators each step needs on one line, where the bottleneck is and how many pieces a day the line can make.
        The default efficiency, 78%, is about the median line target in the company's plans.</p>
      <div className="toolbar">
        <div className="field"><label htmlFor="lb-n">Operators on the line</label>
          <input id="lb-n" type="number" min={steps.length} max={200} value={operators} onChange={(e) => setOperators(Number(e.target.value) || steps.length)} /></div>
        <div className="field"><label htmlFor="lb-m">Working minutes per day</label>
          <input id="lb-m" type="number" min={60} max={1440} step={30} value={minutes} onChange={(e) => setMinutes(Number(e.target.value) || 480)} /></div>
        <div className="field"><label htmlFor="lb-e">Expected efficiency (%)</label>
          <input id="lb-e" type="number" min={30} max={100} value={efficiency} onChange={(e) => setEfficiency(Number(e.target.value) || 78)} /></div>
      </div>
      {!b ? <Notice>Give at least one operator per step ({steps.length}).</Notice> : (
        <>
          <div className="measures">
            <Measure value={num(b.takt, 2)} label="takt time (minutes per piece)" />
            <Measure value={Math.floor(b.targetPerDay).toLocaleString()} label="target pieces per day" />
            <Measure value={Math.floor(b.balancedPerDay).toLocaleString()} label="pieces per day after balancing" />
            <Measure value={`${num(b.efficiency, 1)}%`} label="balance efficiency" tone={b.efficiency < 85 ? "late" : "ontime"} />
          </div>
          <div className="table-wrap"><table>
            <thead><tr><th className="num">Step</th><th>Operation</th><th className="num">SAM (min)</th><th className="num">Ideal operators</th>
              <th className="num">Operators</th><th className="num">Minutes per piece</th><th>Load</th></tr></thead>
            <tbody>{b.steps.map((s, i) => (
              <tr key={s.step} className={i === b.bottleneck ? "bottleneck" : undefined}>
                <td className="num">{s.step}</td><td>{s.name}{i === b.bottleneck && <strong className="late-text"> bottleneck</strong>}</td>
                <td className="num">{num(s.sam, 2)}</td><td className="num">{num(s.ideal, 2)}</td><td className="num">{s.operators}</td>
                <td className="num">{num(s.cycle, 2)}</td>
                <td style={{ minWidth: 120 }}><Meter value={s.cycle} max={b.steps[b.bottleneck].cycle} bad={i === b.bottleneck} /></td></tr>))}
            </tbody></table></div>
        </>)}
    </section>
  );
}
