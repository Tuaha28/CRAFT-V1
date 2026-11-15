import { Link, useSearchParams } from "react-router-dom";
import { useQueries, useQuery } from "@tanstack/react-query";
import { api, type MethodResult, type RunDetail } from "../lib/api";
import { MODE_LABEL, num, setLabel, signed, when } from "../lib/format";
import { DeliveryTape } from "../components/DeliveryTape";
import { Loading, Notice, PageHead } from "../components/ui";

const ROWS: { key: keyof MethodResult; label: string; digits: number; lowerIsBetter: boolean }[] = [
  { key: "complete", label: "Orders completed", digits: 0, lowerIsBetter: false },
  { key: "late", label: "Late orders", digits: 0, lowerIsBetter: true },
  { key: "mean_tardiness_days", label: "Days late on average", digits: 1, lowerIsBetter: true },
  { key: "max_tardiness_days", label: "Worst days late", digits: 1, lowerIsBetter: true },
  { key: "makespan_days", label: "Days to the last finish", digits: 1, lowerIsBetter: true },
];

function title(r: RunDetail) {
  return `${r.dataset?.label}, factory ${r.factory_id}, ${setLabel(r.order_offset, r.orders ?? 0)}, ${MODE_LABEL[r.mode]}`;
}

/** What-if: two finished CRAFT plans side by side. */
export default function Compare() {
  const [params, setParams] = useSearchParams();
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs });
  const finished = (runs.data ?? []).filter((r) => r.kind === "SCHEDULE" && r.status === "SUCCEEDED");
  const ids = [params.get("a") ?? "", params.get("b") ?? ""];
  const details = useQueries({ queries: ids.map((id) => ({ queryKey: ["run", id], queryFn: () => api.run(id), enabled: Boolean(id) })) });
  const [a, b] = details.map((d) => d.data);
  const pick = (slot: "a" | "b", id: string) => { const next = new URLSearchParams(params); next.set(slot, id); setParams(next, { replace: true }); };
  const va = a?.digest?.methods.find((m) => m.policy === "CRAFT_V3");
  const vb = b?.digest?.methods.find((m) => m.policy === "CRAFT_V3");

  return (
    <>
      <PageHead title="Compare plans" lead="Put two finished CRAFT plans side by side, for example a quick and a full run of the same orders, or the same orders on another capture." />
      {runs.isPending ? <Loading /> : finished.length < 2 ? <Notice>At least two finished plans are needed. <Link to="/plan">Start a plan</Link>.</Notice> : (
        <section className="panel grid-2">
          {(["a", "b"] as const).map((slot, i) => (
            <div key={slot} className="field"><label htmlFor={`cmp-${slot}`}>Plan {i === 0 ? "A" : "B"}</label>
              <select id={`cmp-${slot}`} value={ids[i]} onChange={(e) => pick(slot, e.target.value)}>
                <option value="">Choose a plan</option>
                {finished.map((r) => <option key={r.id} value={r.id}>{when(r.finished_at)}: {r.dataset?.label}, factory {r.factory_id}, {setLabel(r.order_offset, r.orders ?? 0)}, {r.mode.toLowerCase()}</option>)}
              </select></div>))}
        </section>)}
      {a && b && va && vb && (
        <>
          {(a.dataset?.id !== b.dataset?.id || a.factory_id !== b.factory_id || a.order_offset !== b.order_offset || a.orders !== b.orders) &&
            <Notice>These plans cover different orders or data, so differences come from the input as well as the plan.</Notice>}
          <section className="panel section">
            <div className="table-wrap"><table>
              <thead><tr><th>Result</th><th className="num"><Link to={`/runs/${a.id}`}>A</Link>: {title(a)}</th>
                <th className="num"><Link to={`/runs/${b.id}`}>B</Link>: {title(b)}</th><th className="num">B − A</th></tr></thead>
              <tbody>{ROWS.map((row) => {
                const x = va[row.key] as number | null, y = vb[row.key] as number | null;
                const diff = x != null && y != null ? y - x : null;
                const better = diff == null || Math.abs(diff) < 1e-9 ? "" : (diff < 0) === row.lowerIsBetter ? "ontime-text" : "late-text";
                return (<tr key={row.key}><td>{row.label}</td><td className="num">{num(x, row.digits)}</td><td className="num">{num(y, row.digits)}</td>
                  <td className={`num ${better}`}>{signed(diff, row.digits)}</td></tr>);
              })}</tbody>
            </table></div>
            <p className="muted small">Green: B is better on that measure; red: B is worse.</p>
          </section>
          <details className="panel section more">
            <summary>Show the deliveries of both plans</summary>
            <div className="grid-2">
              {[a, b].map((r, i) => (
                <section key={r.id} className="section"><h2>Plan {i === 0 ? "A" : "B"}</h2>
                  {r.digest && <DeliveryTape rows={r.digest.order_rows} reference={r.digest.reference_time} />}</section>))}
            </div>
          </details>
        </>)}
    </>
  );
}
