import { Link, useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { MODE_LABEL, setLabel, stageText, when } from "../lib/format";
import { Loading, Notice, PageHead, Progress, Status } from "../components/ui";
import { AutoPlanSwitch } from "../components/AutoPlan";
import { useReadyScope } from "../app/scope";
import { useSession } from "../app/session";

export default function Runs() {
  const navigate = useNavigate();
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs, refetchInterval: 10_000 });
  const ready = useReadyScope();
  const session = useSession();
  if (runs.isPending) return <Loading />;
  if (runs.isError) return <Notice tone="error">{runs.error.message}</Notice>;
  return (
    <>
      <PageHead title="Plans" lead="Every CRAFT run and database capture, newest first.">
        <Link className="btn" to="/compare">Compare plans</Link>
        <Link className="btn primary" to="/plan">New plan</Link>
      </PageHead>
      {ready && <AutoPlanSwitch d={ready[0]} f={ready[1]} canChange={session.can_request} />}
      {!runs.data.length ? (
        <section className="panel empty"><h2>No plans yet</h2><Link className="btn primary" to="/plan">Start a plan</Link></section>
      ) : (
        <section className="panel table-wrap">
          <table>
            <thead><tr><th>Requested</th><th>What</th><th>Status</th><th className="num">Late orders</th><th>Decision</th></tr></thead>
            <tbody>
              {runs.data.map((run) => {
                const v3 = run.summary.craft_v3;
                return (
                  <tr key={run.id} className="clickable" onClick={() => navigate(`/runs/${run.id}`)}>
                    <td><Link to={`/runs/${run.id}`} onClick={(e) => e.stopPropagation()}>{when(run.created_at)}</Link><br /><span className="muted small">{run.requested_by}</span></td>
                    <td>{run.kind === "CAPTURE" ? "Database capture" : <>{run.dataset?.label}, factory {run.factory_id}<br /><span className="muted small">{
                      run.purpose === "DISRUPTION" ? "Disruption re-plan" : run.purpose === "PROMISE" ? "New-order check" : setLabel(run.order_offset, run.orders ?? 0)}, {MODE_LABEL[run.mode].toLowerCase()}</span></>}</td>
                    <td style={{ minWidth: 150 }}><Status value={run.status} />
                      {run.status === "RUNNING" && <div style={{ marginTop: 6 }}><Progress value={run.progress} label="Progress" live /><span className="muted small">{stageText(run.stage)}</span></div>}</td>
                    <td className="num">{v3?.late != null ? <>{v3.late}<span className="muted small"> of {run.orders ?? "—"}</span></> : "—"}</td>
                    <td>{run.kind === "SCHEDULE" && run.status === "SUCCEEDED" ? <Status value={run.decision} /> : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      )}
    </>
  );
}
