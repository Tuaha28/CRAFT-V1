import type { Run } from "../services/governance";

export function RunProgress({ run }: { run: Run }) {
  const checkpoint = run.result?.checkpoint;
  const progress = run.result?.progress;
  const deferred = checkpoint?.deferred ?? progress?.deferred ?? progress?.first_failure ?? {};
  const accepted = checkpoint?.accepted_keys.length ?? progress?.accepted;
  const remaining = checkpoint ? checkpoint.operation_keys.length - checkpoint.accepted_keys.length : progress?.remaining;
  const next = checkpoint?.pending_windows?.[0];
  return <div>
    {accepted !== undefined ? <div>{accepted} accepted · {remaining} remaining{progress?.cycle_number !== undefined ? ` · cycle ${progress.cycle_number}` : ""}</div> : "—"}
    {checkpoint && <small>{checkpoint.revision} attempts · checkpoint is not manager approval</small>}
    {next && <div>Next bounded attempt: {next.keys.length} operations · cycle {next.cycle_number}</div>}
    {run.result?.reason && <div>{run.result.reason}</div>}
    {run.result?.exception && <small className="mono">{run.result.exception}</small>}
    {Object.keys(deferred).length > 0 && <details><summary>{Object.keys(deferred).length} explicitly deferred operations</summary>
      <ul>{Object.entries(deferred).map(([key, reason]) => <li key={key}>{key}: {reason}</li>)}</ul>
    </details>}
  </div>;
}
