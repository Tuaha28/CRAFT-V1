import { useMemo, useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, erp, type Digest, type MethodResult, type RunDetail } from "../lib/api";
import { MODE_LABEL, duration, num, planDate, setLabel, eventText, signed, stageText, when } from "../lib/format";
import { useSession } from "../app/session";
import { useReadyScope } from "../app/scope";
import { DeliveryTape } from "../components/DeliveryTape";
import { Gantt } from "../components/Gantt";
import { WhatIf } from "../components/WhatIf";
import { ConfirmButton, Loading, Notice, PageHead, Progress, Status } from "../components/ui";
import { CountUp } from "../components/CountUp";
import { FloorView, hallsFromMachines } from "../components/FloorView";
import { lineName, useFloorLines } from "../components/Floor";

/** Facts about this plan that a manager can rely on before approving; each comes from the plan itself. */
function planChecks(run: RunDetail, d: Digest, v3: MethodResult): { ok: boolean; text: string }[] {
  const chosen = d.craft_v3.starts?.find((st) => st.chosen);
  const lines = new Set((d.gantt.CRAFT_V3 ?? []).map((g) => g.line)).size;
  const out: { ok: boolean; text: string }[] = [];
  if (chosen) out.push({ ok: chosen.validation, text: chosen.validation ? "Every step checked against shifts, machines and crews" : "The plan did not pass the shift, machine and crew check" });
  out.push({ ok: v3.complete === d.orders, text: v3.complete === d.orders ? `All ${d.orders} orders finish within the plan` : `${d.orders - v3.complete} of ${d.orders} orders do not finish within the plan` });
  out.push({ ok: true, text: `${d.operations} steps planned on ${lines} line${lines === 1 ? "" : "s"}` });
  out.push({ ok: run.mode === "FULL", text: run.mode === "FULL" ? "Full plan (the best CRAFT can find)" : "Quick draft: a full plan may deliver earlier" });
  return out;
}

function Decision({ run, d, v3 }: { run: RunDetail; d: Digest; v3: MethodResult }) {
  const session = useSession();
  const client = useQueryClient();
  const [reason, setReason] = useState("");
  const decide = useMutation({
    meta: { success: (x: unknown) => (x === "APPROVED" ? "Plan approved. The shop-floor pages now follow it." : "Plan sent back.") },
    mutationFn: (decision: "APPROVED" | "REJECTED") => api.decide(run.id, decision, reason),
    onSuccess: () => { setReason(""); return client.invalidateQueries({ queryKey: ["run", run.id] }); },
  });
  const checks = planChecks(run, d, v3);
  return (
    <section className="panel section hero decision" id="decision" aria-labelledby="decision-title">
      <h2 id="decision-title">{run.decision === "PENDING" ? "Approve this plan?" : run.decision === "APPROVED" ? "Plan approved" : "Plan sent back"}</h2>
      {run.decision !== "PENDING" ? (
        <><p className="decided"><Status value={run.decision} /> by {run.decided_by}, {when(run.decided_at)}</p>
          {run.decision_reason && <p className="muted small">“{run.decision_reason}”</p>}</>
      ) : <p className="muted small">Once approved, it becomes the plan the shop floor works from. Production activation is off, so the factory database never changes.</p>}
      <ul className="checks">{checks.map((c) => (
        <li key={c.text} className={c.ok ? "ok" : "warn"}><span aria-hidden="true">{c.ok ? "✓" : "!"}</span>{c.text}</li>))}</ul>
      {run.decision === "PENDING" && (session.can_approve ? (
        <>
          <div className="field"><label htmlFor="reason">Reason (kept with the decision)</label>
            <textarea id="reason" rows={3} value={reason} onChange={(e) => setReason(e.target.value)}
              placeholder="For example: fewer late orders with the same workers" /></div>
          {decide.isError && <Notice tone="error">{(decide.error as Error).message}</Notice>}
          <div className="decision-actions">
            <ConfirmButton className="btn primary" label="Approve plan" confirm="Approve this plan? It goes to the shop-floor pages and the decision cannot be changed." disabled={!reason.trim() || decide.isPending} onConfirm={() => decide.mutate("APPROVED")} />
            <ConfirmButton className="btn danger" label="Send back" confirm="Send this plan back? The decision is recorded and cannot be changed." disabled={!reason.trim() || decide.isPending} onConfirm={() => decide.mutate("REJECTED")} />
          </div>
          {!reason.trim() && <p className="muted small">Write a reason to enable the buttons.</p>}
        </>
      ) : <p className="muted small">Only a production manager can approve or send back plans.</p>)}
    </section>
  );
}

/** This plan against the plan the floor follows now, when both cover the same orders. */
function AgainstCurrent({ run, d, v3 }: { run: RunDetail; d: Digest; v3: MethodResult }) {
  const dataset = run.dataset?.id, factory = run.factory_id;
  const current = useQuery({ queryKey: ["current-plan", dataset && factory ? [dataset, factory] : null], enabled: Boolean(dataset && factory),
    queryFn: () => erp.currentPlan(dataset!, factory!) });
  const cur = current.data;
  let body: ReactNode;
  if (current.isPending) body = <p className="muted small">Reading the current plan…</p>;
  else if (!cur?.run || !cur.digest) body = <p className="muted small">No plan has been approved for this factory yet.</p>;
  else if (cur.run.id === run.id) body = <p className="muted small">This is the plan the floor follows now.</p>;
  else {
    const same = new Set(cur.digest.order_rows.map((o) => o.order_id));
    const sameOrders = same.size === d.order_rows.length && d.order_rows.every((o) => same.has(o.order_id));
    const old = cur.digest.methods.find((m) => m.policy === "CRAFT_V3");
    if (!sameOrders || !old) body = <p className="muted small">The approved plan covers other orders, so the two are not compared here. <Link to={`/compare?a=${cur.run.id}&b=${run.id}`}>Compare anyway</Link></p>;
    else {
      const rows: [string, string, string, number][] = [
        ["Late orders", String(v3.late), String(old.late), v3.late - old.late],
        ["Days late on average", num(v3.mean_tardiness_days, 1), num(old.mean_tardiness_days, 1), (v3.mean_tardiness_days ?? 0) - (old.mean_tardiness_days ?? 0)],
        ["Last order finishes (day)", num(v3.makespan_days, 0), num(old.makespan_days, 0), (v3.makespan_days ?? 0) - (old.makespan_days ?? 0)],
      ];
      body = (
        <table className="against"><thead><tr><th>Measure</th><th className="num">This plan</th><th className="num">Approved</th></tr></thead>
          <tbody>{rows.map(([label, mine, theirs, diff]) => (
            <tr key={label}><td>{label}</td><td className={`num ${diff < 0 ? "ontime-text" : diff > 0 ? "late-text" : ""}`}>{mine}</td><td className="num muted">{theirs}</td></tr>))}</tbody></table>);
    }
  }
  return <section className="panel section"><h2>Against the current plan</h2>{body}</section>;
}

/** Work each line carries in this plan: total step time in days, and how many orders. */
function WorkPerLine({ d, lineLabel }: { d: Digest; lineLabel: (line: number) => string }) {
  const rows = d.gantt.CRAFT_V3 ?? [];
  const lines = [...new Set(rows.map((r) => r.line))].sort((a, b) => a - b).map((line) => {
    const mine = rows.filter((r) => r.line === line);
    return { line, days: mine.reduce((t, r) => t + (r.end - r.start), 0) / 86_400, orders: new Set(mine.map((r) => r.order_id)).size };
  });
  const most = Math.max(1, ...lines.map((l) => l.days));
  if (!lines.length) return null;
  return (
    <section className="panel section" aria-labelledby="wpl-title">
      <h2 id="wpl-title">Work per line</h2>
      <ul className="line-work">{lines.map((l) => (
        <li key={l.line}>
          <span className="line-work-top"><strong>{lineLabel(l.line)}</strong><span className="muted small">{num(l.days, 1)} work-days · {l.orders} order{l.orders === 1 ? "" : "s"}</span></span>
          <span className="line-work-bar"><i style={{ width: `${(l.days / most) * 100}%` }} /></span>
        </li>))}</ul>
      <p className="muted small">Work-days: the time of every step planned on the line, added up.</p>
    </section>
  );
}

/** What a what-if run answers, with links back to the plan it came from. */
function WhatIfHeader({ run }: { run: RunDetail }) {
  const parent = useQuery({ queryKey: ["run", run.parent_id], queryFn: () => api.run(run.parent_id!), enabled: Boolean(run.parent_id) });
  if (run.purpose === "PLAN" || !run.parent_id) return null;
  const s = run.scenario;
  const lost = [...(s.machines_down ?? []).map((m) => `machine ${m}`), ...(s.workers_absent ?? []).map((w) => `worker ${w}`)];
  const question = run.purpose === "PROMISE" && s.new_order
    ? `Can we accept ${s.new_order.quantity} pieces of style ${s.new_order.style_code ?? s.new_order.product_id}, released ${s.new_order.release}, due ${s.new_order.due}?`
    : `Re-plan of the same orders with ${lost.join(", ")} unavailable.`;
  const promise = run.summary.promise;
  const ref = run.digest?.reference_time ?? run.summary.reference_time ?? "";
  const parentV3 = parent.data?.summary.craft_v3;
  const mine = run.summary.craft_v3;
  const otherLate = mine?.late != null ? mine.late - (promise && promise.days_late ? 1 : 0) : null;
  return (
    <section className="section">
      <Notice>
        {question}{lost.length && run.purpose === "PROMISE" ? ` Also without ${lost.join(", ")}.` : ""}{" "}
        <Link to={`/runs/${run.parent_id}`}>Original plan</Link>
        {run.status === "SUCCEEDED" && <Link style={{ marginLeft: 14 }} to={`/compare?a=${run.parent_id}&b=${run.id}`}>Compare with the original</Link>}
      </Notice>
      {run.purpose === "PROMISE" && promise && promise.finish_day != null && ref && (
        <div className={`answer${promise.days_late ? " late" : ""}`}>
          <span className="muted small">Earliest delivery CRAFT can plan</span>
          <span className="figure">{planDate(ref, promise.finish_day)}</span>
          <span>{promise.days_late ? `${num(promise.days_late, 1)} days after the requested date (${planDate(ref, promise.due_day ?? 0)}). ${promise.reason ?? ""}`
            : `On time for the requested date (${planDate(ref, promise.due_day ?? 0)}).`}</span>
          {parentV3 && otherLate != null && (
            <span className="small">Effect on the other {run.digest ? run.digest.orders - 1 : ""} orders: {otherLate} late, against {parentV3.late} in the original plan
              ({signed(otherLate - (parentV3.late ?? 0), 0)}).</span>)}
        </div>)}
    </section>
  );
}

function InProgress({ run }: { run: RunDetail }) {
  const session = useSession();
  const client = useQueryClient();
  const [reason, setReason] = useState("");
  const cancel = useMutation({
    meta: { success: "Plan cancelled." },
    mutationFn: () => api.cancel(run.id, reason || "Cancelled by the planner"),
    onSuccess: () => client.invalidateQueries({ queryKey: ["run", run.id] }),
  });
  return (
    <section className="panel section">
      <div className="section-head"><h2>{run.status === "QUEUED" ? (run.worker_down ? "The planning service is not running" : "Waiting its turn") : stageText(run.stage)}</h2>
        <span className="muted small">{run.status === "RUNNING" ? `Running for ${duration(run.started_at, null)}` : `Requested ${when(run.created_at)}`}</span></div>
      <Progress value={run.progress} label="Plan progress" live={run.status === "RUNNING"} />
      {run.status === "QUEUED" && <p className="muted small">{run.worker_down ? "This plan starts as soon as your administrator starts the planning service. You can also cancel it below." : "Plans are made one at a time; this one starts when the plan before it finishes."}</p>}
      {run.log_tail && <details><summary>Planning log</summary><pre className="log">{run.log_tail}</pre></details>}
      {session.can_request && (
        <div style={{ display: "flex", gap: 10, alignItems: "end", flexWrap: "wrap" }}>
          <div className="field" style={{ flex: 1, minWidth: 220 }}><label htmlFor="creason">Reason for cancelling</label>
            <input id="creason" value={reason} onChange={(e) => setReason(e.target.value)} /></div>
          <ConfirmButton label="Cancel run" confirm="Stop this run? Its work so far is lost." disabled={cancel.isPending} onConfirm={() => cancel.mutate()} />
        </div>
      )}
    </section>
  );
}

/** The four numbers a manager decides on. */
function PlanCards({ v3, orders, reference }: { v3: MethodResult; orders: number; reference: string }) {
  const onTime = orders - v3.late;
  return (
    <section className="plan-cards four" aria-label="Plan results">
      <div className={`panel plan-card${v3.late ? " bad" : " good"}`}>
        <span className="plan-card-top"><small>Late orders</small></span>
        <span className="figure"><CountUp value={v3.late} /><small> of {orders}</small></span>
        <span className="plan-bar"><i style={{ width: `${(v3.late / Math.max(1, orders)) * 100}%` }} /></span>
        <small>{v3.complete < orders ? `${orders - v3.complete} not finished in the plan` : `All ${orders} orders finish in the plan`}</small>
      </div>
      <div className="panel plan-card"><small>On time</small><span className="figure ontime-text"><CountUp value={onTime} /><small> of {orders}</small></span>
        <small>delivered by their due date</small></div>
      <div className="panel plan-card"><small>Days late on average</small><span className="figure">{num(v3.mean_tardiness_days, 1)}</span>
        <small>{v3.max_tardiness_days ? `worst order ${num(v3.max_tardiness_days, 1)} days` : `over all ${orders} orders`}</small></div>
      <div className="panel plan-card"><small>Last order finishes</small><span className="figure">{v3.makespan_days != null ? planDate(reference, v3.makespan_days) : "—"}</span>
        <small>{num(v3.makespan_days, 0)} days after the plan starts</small></div>
    </section>
  );
}

/** How CRAFT built this plan, in its three phases. */
function HowBuilt({ d }: { d: Digest }) {
  const steps = [
    ["Plan a day at a time", "CRAFT plans the orders in 24-hour windows and fixes 8 hours at a time, so every step fits the shifts, machines and crews."],
    ["Improve the plan", `CRAFT then tries moving steps between workers, machines and times: ${d.craft_v3.accepted_moves ?? "—"} changes kept, ${d.craft_v3.improving_moves ?? "—"} of them made deliveries better.`],
    ["Check and keep the best", "The best plan found is checked again against shifts, machines and crews before it is shown here."],
  ];
  return (
    <ol className="phases">{steps.map(([title, text], i) => (
      <li key={title}><span className="phase-n" aria-hidden="true">{i + 1}</span><span><strong>{title}</strong><small>{text}</small></span></li>))}</ol>
  );
}

type TabKey = "orders" | "deliveries" | "floor" | "built" | "whatif" | "history";

/** This plan running on the factory floor. */
function PlanFloor({ r, d }: { r: RunDetail; d: Digest }) {
  const dataset = r.dataset?.id, factory = r.factory_id;
  const machines = useQuery({ queryKey: ["erp-machines", dataset && factory ? [dataset, factory] : null, "all"], enabled: Boolean(dataset && factory),
    queryFn: () => erp.machines(dataset!, factory!, { size: 200 }) });
  const lines = useFloorLines(dataset, factory ?? undefined);
  const halls = useMemo(() => hallsFromMachines(machines.data?.items ?? [], lines.data?.lines ?? []), [machines.data, lines.data]);
  const plan = useMemo(() => ({ rows: d.gantt.CRAFT_V3 ?? [], reference: d.reference_time, orders: d.order_rows }), [d]);
  if (machines.isPending) return <Loading label="Reading machines" />;
  if (machines.isError) return <Notice tone="error">{machines.error.message}</Notice>;
  if (!halls.length) return <p className="muted">No machines with a line in this factory's data.</p>;
  return <FloorView halls={halls} plan={plan} />;
}

function History({ r }: { r: RunDetail }) {
  return (
    <ol className="timeline">{r.events.map((e, i) => (
      <li key={i}><time>{when(e.created_at)}</time><span>{eventText(e.message)}{e.actor ? ` (${e.actor})` : ""}</span></li>))}</ol>
  );
}

const isLateRow = (o: Digest["order_rows"][number]) => o.status === "Late" || o.status === "Not fully planned";

/** Orders, latest first: the late ones lead, the rest open with one click. */
function OrdersTable({ d }: { d: Digest }) {
  const [all, setAll] = useState(false);
  const lateness = (o: Digest["order_rows"][number]) => {
    const f = o.methods.CRAFT_V3?.finish_day;
    return o.status === "Not fully planned" ? 1e9 : f != null && o.due_day != null ? f - o.due_day : -1e9;
  };
  const rows = [...d.order_rows].sort((a, b) => lateness(b) - lateness(a));
  const late = rows.filter(isLateRow);
  const shown = all ? rows : late.length ? late : rows.slice(0, 8);
  return (
    <>
      <div className="table-wrap"><table>
        <thead><tr><th>Order</th><th className="num">Pieces</th><th>Due</th><th>Planned finish</th><th>Status and main reason</th></tr></thead>
        <tbody>{shown.map((o) => {
          const mine = o.methods.CRAFT_V3;
          const isLate = isLateRow(o);
          return (
            <tr key={o.order_id}>
              <td className="nowrap"><Link to={`/orders/${o.order_id}`}>Order {o.order_id}</Link></td><td className="num">{o.pieces ?? "—"}</td>
              <td className="date">{o.due_day != null ? planDate(d.reference_time, o.due_day) : "—"}</td>
              <td className={`date ${isLate ? "late-text" : "ontime-text"}`}>{mine?.finish_day != null ? planDate(d.reference_time, mine.finish_day) : "not planned"}</td>
              <td><strong className={isLate ? "late-text" : "ontime-text"}>{o.status ?? "—"}</strong><br /><span className="small muted">{o.reason}</span></td>
            </tr>);
        })}</tbody></table></div>
      {shown.length < rows.length && (
        <button type="button" className="btn show-all" onClick={() => setAll(true)}>Show all {rows.length} orders</button>)}
    </>
  );
}

function PlanTabs({ r, d }: { r: RunDetail; d: Digest }) {
  const session = useSession();
  const [tab, setTab] = useState<TabKey>("orders");
  const late = d.order_rows.filter(isLateRow).length;
  const tabs: [TabKey, string][] = [
    ["orders", late ? `Late orders (${late})` : "Orders"], ["deliveries", "Deliveries"], ["floor", "On the floor"], ["built", "How CRAFT built it"],
    ...(session.can_request ? [["whatif", "What if…"] as [TabKey, string]] : []), ["history", "History"],
  ];
  return (
    <section className="panel section">
      <div className="tabs plan-tabs" role="tablist" aria-label="Plan details">
        {tabs.map(([key, label]) => (
          <button key={key} type="button" role="tab" id={`tab-${key}`} aria-selected={tab === key} aria-controls="plan-tab"
            className={tab === key ? "on" : ""} onClick={() => setTab(key)}>{label}</button>))}
      </div>
      <div id="plan-tab" role="tabpanel" aria-labelledby={`tab-${tab}`} className="section">
        {tab === "orders" && <OrdersTable d={d} />}
        {tab === "deliveries" && (<>
          <p className="muted small">Plan starts {planDate(d.reference_time, 0)}. One lane per order, earliest due first.</p>
          <DeliveryTape rows={d.order_rows} reference={d.reference_time} /></>)}
        {tab === "floor" && <PlanFloor r={r} d={d} />}
        {tab === "built" && <HowBuilt d={d} />}
        {tab === "whatif" && <WhatIf run={r} />}
        {tab === "history" && <History r={r} />}
      </div>
    </section>
  );
}

export default function RunPage() {
  const { id = "" } = useParams();
  const scope = useReadyScope();
  const lineRefs = useFloorLines(scope?.[0], scope?.[1]);
  const run = useQuery({
    queryKey: ["run", id], queryFn: () => api.run(id),
    refetchInterval: (q) => (q.state.data && ["QUEUED", "RUNNING"].includes(q.state.data.status) ? 8_000 : false),
  });
  if (run.isPending) return <Loading />;
  if (run.isError) return <Notice tone="error">{run.error.message}</Notice>;
  const r = run.data;
  const d = r.digest;

  if (r.kind === "CAPTURE") {
    return (
      <>
        <PageHead title="Database capture" lead={`Read-only capture requested by ${r.requested_by}, ${when(r.created_at)}`}><Status value={r.status} /></PageHead>
        {["QUEUED", "RUNNING"].includes(r.status) ? <InProgress run={r} /> : r.status === "SUCCEEDED"
          ? <Notice>Captured as {r.summary.dataset}. It is now listed under <Link to="/data">Factory data</Link>.</Notice>
          : <Notice tone="error">{r.error || "The capture did not finish."}</Notice>}
        {r.log_tail && r.status !== "RUNNING" && <pre className="log">{r.log_tail}</pre>}
      </>
    );
  }

  const v3 = d?.methods.find((m) => m.policy === "CRAFT_V3");
  return (
    <>
      <PageHead title={r.purpose === "PROMISE" ? `New-order check: factory ${r.factory_id}` : r.purpose === "DISRUPTION" ? `Disruption re-plan: factory ${r.factory_id}` : `Factory ${r.factory_id}: ${setLabel(r.order_offset, r.orders ?? 0)}`}
        lead={`${r.dataset?.label}, ${MODE_LABEL[r.mode]}, requested by ${r.requested_by} ${when(r.created_at)}`}>
        <Status value={r.status} />
        {r.purpose === "PLAN" && r.decision === "PENDING" && r.status === "SUCCEEDED" && <span className="live-chip pending"><i />Waiting for approval</span>}
        {d && <Link className="btn" to={`/compare?a=${r.id}`}>Compare with another plan</Link>}
        {d?.workbook && <a className="btn" href={api.workbookUrl(r.id)}>Download Excel plan</a>}
      </PageHead>
      <WhatIfHeader run={r} />
      {r.mode === "QUICK" && <Notice>Quick plan: shorter thinking time, so deliveries can be later than in a full plan.</Notice>}
      {["QUEUED", "RUNNING"].includes(r.status) && <InProgress run={r} />}
      {r.status === "FAILED" && <><Notice tone="error">{r.error || "The run failed."}</Notice>{r.log_tail && <pre className="log">{r.log_tail}</pre>}</>}
      {r.status === "CANCELLED" && <Notice>This run was cancelled.</Notice>}

      {d && v3 && (
        <div className="plan-layout">
          <div className="plan-main">
            <PlanCards v3={v3} orders={d.orders} reference={d.reference_time} />
            <section className="panel section">
              <h2>Schedule</h2>
              <Gantt rows={d.gantt.CRAFT_V3 ?? []} reference={d.reference_time} initialGroup="order" lineLabel={(line) => lineName(lineRefs.data?.lines, line)}
                dueDay={Object.fromEntries(d.order_rows.map((o) => [o.order_id, o.due_day]))} />
            </section>
            <PlanTabs r={r} d={d} />
          </div>
          <aside className="plan-side" aria-label="Decision and summary">
            {r.purpose === "PLAN" && <Decision run={r} d={d} v3={v3} />}
            <WorkPerLine d={d} lineLabel={(line) => lineName(lineRefs.data?.lines, line)} />
            {r.purpose === "PLAN" && <AgainstCurrent run={r} d={d} v3={v3} />}
          </aside>
        </div>
      )}
      {!d && r.events.length > 0 && <section className="panel section"><h2>History</h2><History r={r} /></section>}
    </>
  );
}
