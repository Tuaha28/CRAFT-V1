import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { floor, type Issue } from "../../lib/api";
import { MODE_LABEL, when } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { useSession } from "../../app/session";
import { lineName, ReportProblem, useFloorLines } from "../../components/Floor";
import { Loading, Notice, PageHead } from "../../components/ui";

const TONE: Record<Issue["status"], string> = { OPEN: "bad", IN_PROGRESS: "warn", RESOLVED: "good" };
const COLUMNS: [Issue["status"], string, string][] = [
  ["OPEN", "Open", "No open problems."], ["IN_PROGRESS", "Being worked on", "Nothing is being worked on."], ["RESOLVED", "Closed", "Nothing closed yet."]];

function Actions({ issue, canHandle, canRepair }: { issue: Issue; canHandle: boolean; canRepair: boolean }) {
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [closing, setClosing] = useState(false);
  const move = useMutation({
    meta: { success: (s: unknown) => (s === "RESOLVED" ? "Problem closed." : "Problem marked as being worked on.") },
    mutationFn: (status: "IN_PROGRESS" | "RESOLVED") => floor.moveIssue(issue.id, status, text),
    onSuccess: async () => { setClosing(false); setText(""); await client.invalidateQueries({ queryKey: ["floor-issues"] });
      await client.invalidateQueries({ queryKey: ["floor-board"] }); await client.invalidateQueries({ queryKey: ["floor-line"] }); },
  });
  const allowed = canHandle || (canRepair && issue.kind === "MACHINE_DOWN");
  if (!allowed || issue.status === "RESOLVED") return null;
  return (
    <div className="issue-actions">
      {issue.status === "OPEN" && <button type="button" className="btn" disabled={move.isPending} onClick={() => move.mutate("IN_PROGRESS")}>
        {issue.kind === "MACHINE_DOWN" ? "Start repair" : "Take it"}</button>}
      {!closing ? <button type="button" className="btn" onClick={() => setClosing(true)}>Resolve</button> : (
        <form onSubmit={(e) => { e.preventDefault(); move.mutate("RESOLVED"); }} className="inline-form">
          <label className="sr-only" htmlFor={`res-${issue.id}`}>How was it resolved</label>
          <input id={`res-${issue.id}`} value={text} onChange={(e) => setText(e.target.value)} placeholder="How it was resolved" required autoFocus />
          <button className="btn primary" disabled={move.isPending}>Close issue</button>
        </form>)}
      {move.isError && <span className="late-text small">{move.error.message}</span>}
    </div>
  );
}

export default function Issues() {
  const session = useSession();
  const ready = useReadyScope();
  const navigate = useNavigate();
  const client = useQueryClient();
  const [allClosed, setAllClosed] = useState(false);
  const [reporting, setReporting] = useState(false);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [mode, setMode] = useState<"QUICK" | "FULL">("QUICK");
  const lines = useFloorLines(ready?.[0], ready?.[1]);
  // The board shows every column, so it always reads closed problems too.
  const list = useQuery({ queryKey: ["floor-issues", ready, false], queryFn: () => floor.issues(ready![0], ready![1], false),
    enabled: Boolean(ready), refetchInterval: 30_000 });
  const replan = useMutation({
    meta: { success: "New plan requested." },
    mutationFn: () => floor.replan(ready![0], ready![1], [...picked], mode),
    onSuccess: async (r) => { await client.invalidateQueries({ queryKey: ["runs"] }); navigate(`/runs/${r.run_id}`); },
  });

  if (!ready || list.isPending) return <Loading label="Loading problems" />;
  if (list.isError) return <Notice tone="error">{list.error.message}</Notice>;
  const data = list.data;
  const replannable = (i: Issue) => i.status !== "RESOLVED" && ["MACHINE_DOWN", "WORKER_ABSENT", "LINE_STOPPED", "MATERIAL_SHORT"].includes(i.kind) && !i.replan_id;
  const toggle = (id: number) => { const next = new Set(picked); if (next.has(id)) next.delete(id); else next.add(id); setPicked(next); };
  return (
    <>
      <PageHead title="Problems on the floor" lead="Problems reported from the lines. Machine and worker problems can be planned around.">
        {data.can_report && <button type="button" className="btn primary" onClick={() => setReporting(!reporting)}>Report a problem</button>}
      </PageHead>
      {reporting && (
        <section className="panel section"><h2>Report a problem</h2>
          <ReportProblem d={ready[0]} f={ready[1]} lineId={session.scope_line} ops={[]} onDone={() => setReporting(false)} /></section>)}
      {data.can_replan && data.issues.some(replannable) && (
        <section className="panel section hero replan-box">
          <div className="section-head"><h2>Plan around these problems</h2>
            <span className="muted small">{data.has_plan ? "Tick machine and worker problems on the board, then re-plan the approved plan's orders without them." : "Approve a plan first; re-planning starts from it."}</span></div>
          <div className="toolbar">
            <div className="field"><label htmlFor="rp-mode">Run</label>
              <select id="rp-mode" value={mode} onChange={(e) => setMode(e.target.value as "QUICK" | "FULL")}>
                <option value="QUICK">{MODE_LABEL.QUICK}</option><option value="FULL">{MODE_LABEL.FULL}</option></select></div>
            <button type="button" className="btn primary" disabled={!picked.size || !data.has_plan || replan.isPending} onClick={() => replan.mutate()}>
              {replan.isPending ? "Starting…" : `Re-plan without ${picked.size || "the ticked"} item${picked.size === 1 ? "" : "s"}`}</button>
          </div>
          {replan.isError && <Notice tone="error">{replan.error.message}</Notice>}
        </section>)}
      <div className="kanban">
        {COLUMNS.map(([status, title, empty]) => {
          const items = data.issues.filter((i) => i.status === status);
          const shown = status === "RESOLVED" && !allClosed ? items.slice(0, 6) : items;
          return (
            <section key={status} className={`kanban-col ${TONE[status]}`} aria-labelledby={`col-${status}`}>
              <h2 id={`col-${status}`}><span className="kanban-dot" aria-hidden="true" />{title}<span className="count-pill">{items.length}</span></h2>
              {!items.length ? <p className="muted small kanban-empty">{empty}</p> : shown.map((i) => (
                <article key={i.id} className="kanban-card">
                  <header>
                    {data.can_replan && replannable(i) && <input type="checkbox" aria-label={`Include problem ${i.id} in the re-plan`} checked={picked.has(i.id)} onChange={() => toggle(i.id)} />}
                    <strong>{i.kind_label}{i.machine_id ? `: machine ${i.machine_id}` : i.worker_id ? `: worker ${i.worker_id}` : i.order_id ? `: order ${i.order_id}` : ""}</strong>
                  </header>
                  {i.note && <p className="small">{i.note}</p>}
                  {i.resolution && <p className="small ontime-text">Resolved: {i.resolution}</p>}
                  {i.replan_id && <p className="small"><Link to={`/runs/${i.replan_id}`}>Re-plan started</Link></p>}
                  <p className="muted small">{lineName(lines.data?.lines, i.line_id)} · reported by {i.reported_by}, {when(i.created_at)}{i.handled_by ? ` · ${i.handled_by}` : ""}</p>
                  <Actions issue={i} canHandle={data.can_handle} canRepair={data.can_repair} />
                </article>))}
              {shown.length < items.length && <button type="button" className="btn ghost" onClick={() => setAllClosed(true)}>Show all {items.length} closed</button>}
            </section>);
        })}
      </div>
    </>
  );
}
