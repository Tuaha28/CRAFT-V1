import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { governance, type Candidate } from "../services/governance";
import { Badge, ErrorBlock, LoadingBlock, Panel } from "../components/Primitives";

export function CandidateComparison({ candidate, canReview }: {candidate: Candidate; canReview: boolean}) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  const client = useQueryClient();
  const query = useQuery({queryKey:["governance", "comparison", candidate.id, candidate.status], queryFn:() => governance.compare(candidate.id), enabled:open});
  const reject = useMutation({mutationFn:() => governance.reject(candidate.id, reason), onSuccess:() => {setReason(""); void client.invalidateQueries({queryKey:["governance"]});}});
  const data = query.data;
  const horizon = Math.max(1, ...(data?.changes.flatMap(c => [c.before?.end_seconds ?? 0, c.after?.end_seconds ?? 0]) ?? []));
  return <Panel title={`Compare ${candidate.id.slice(0, 8)}`} subtitle="Outlined: current TEST schedule. Solid: candidate. Not observed production.">
    <div className="panel-body"><button className="button button-quiet" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? "Hide comparison" : "Compare active and candidate"}</button></div>
    {open && query.isLoading && <LoadingBlock label="Loading schedule comparison"/>}
    {open && query.isError && <ErrorBlock message={String(query.error)} retry={() => void query.refetch()}/>}
    {open && data && <div className="panel-body"><p>{data.active_version ? `Active ${data.active_version.slice(0, 8)}` : "No active TEST version"} → {data.candidate_version.slice(0, 8)} · {data.changed_operation_count} changed operations</p>
      <p>Reference: {data.reference_time} · dataset-local fixture clock. Axis shows elapsed seconds, not actual completion events.</p>
      <div className="status-grid">{(["replay_status", "physical_status", "scope_status", "quantity_status"] as const).map(key => <div key={key}>{key}: <Badge>{data.validation?.[key] ?? "NOT_RUN"}</Badge></div>)}<div>Materialization mismatches: <Badge>{String(data.validation?.materialization_mismatch_count ?? "NOT_RUN")}</Badge></div></div>
      {data.priority_impact?.candidate && <div className="detail-section"><h3>Measured order impact</h3><p>Times are seconds after the synthetic TEST reference. Customer deadlines are fixed; the manager target is a scheduling objective, not a physical guarantee.</p>
        <div className="table-scroll"><table className="data-table"><thead><tr><th>Order</th><th>Active completion</th><th>Candidate completion</th><th>Active tardiness</th><th>Candidate tardiness</th></tr></thead><tbody>{Object.keys(data.priority_impact.candidate.completion_seconds).sort().map(order => <tr key={order}><td>{order} {data.priority_impact?.manager_override.some(item => item.order_id === order) && <Badge>PRIORITY REQUEST</Badge>}</td><td>{data.priority_impact?.active?.completion_seconds[order] ?? "No active version"}</td><td>{data.priority_impact?.candidate?.completion_seconds[order]}</td><td>{data.priority_impact?.active?.tardiness_seconds[order] ?? "—"}</td><td>{data.priority_impact?.candidate?.tardiness_seconds[order]}</td></tr>)}</tbody></table></div>
        <p>Makespan: {data.priority_impact.active?.makespan_seconds ?? "No active version"} → {data.priority_impact.candidate.makespan_seconds} s.</p>
      </div>}
      {!data.validation?.candidate_validated && <p className="inline-error">The stored evidence does not currently satisfy every validation gate. Do not promote this version.</p>}
      <div className="table-scroll"><table className="data-table"><thead><tr><th>Operation</th><th>Active → candidate time / resources</th><th>Elapsed timeline (0–{horizon}s)</th></tr></thead><tbody>{data.changes.map(change => <tr key={change.operation_key}><td>{change.operation_key} {change.changed && <Badge>CHANGED</Badge>}</td><td>{change.before ? `${change.before.start_seconds}–${change.before.end_seconds}s · machines ${change.before.resource_configuration.machine_ids.join(",")}` : "Not in active schedule"}<br/>{change.after ? `${change.after.start_seconds}–${change.after.end_seconds}s · machines ${change.after.resource_configuration.machine_ids.join(",")}` : "Removed"}</td><td><svg viewBox="0 0 420 48" width="420" height="48" role="img" aria-label={`Operation ${change.operation_key}: active and candidate intervals`}>
        {change.before && <rect x={change.before.start_seconds/horizon*400+2} y="3" width={Math.max(1,(change.before.end_seconds-change.before.start_seconds)/horizon*400)} height="16" fill="none" stroke="currentColor"/>}
        {change.after && <rect x={change.after.start_seconds/horizon*400+2} y="27" width={Math.max(1,(change.after.end_seconds-change.after.start_seconds)/horizon*400)} height="16" fill="var(--color-primary, #286a90)"/>}
      </svg></td></tr>)}</tbody></table></div>
      {canReview && ["VALIDATED_CANDIDATE", "STALE"].includes(candidate.status) && <form onSubmit={e => {e.preventDefault(); reject.mutate();}}><label>Rejection reason <input required value={reason} onChange={e => setReason(e.target.value)}/></label> <button className="button button-quiet" disabled={!reason.trim() || reject.isPending}>Reject candidate</button></form>}
      {reject.isError && <ErrorBlock message={String(reject.error)}/>}
    </div>}
  </Panel>;
}
