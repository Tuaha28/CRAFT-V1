import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { BarChart3, Cog, Sparkles, Wrench } from "lucide-react";
import { api, erp, floor, type GanttRow, type OrderRow, type Run } from "../lib/api";
import { num, setLabel, stageText } from "../lib/format";
import { useReadyScope, useScope } from "../app/scope";
import { can, useSession } from "../app/session";
import { lineName, useFloorLines } from "../components/Floor";
import { DeliveryCurve } from "../components/DeliveryCurve";
import { DeliveryBoard, LineTimeline } from "../components/HomeBoards";
import { daysLate, isLate, OrderDrawer, useAgo } from "../components/Overview";
import { Loading, Notice, Progress } from "../components/ui";
import { CountUp } from "../components/CountUp";
import { PlanHealth, StatTile } from "../components/HomeFocus";
import { AskCraft } from "../components/AskCraft";

const DAY = 86_400;

/** Plans being made right now, one slim line each. */
function Running({ runs }: { runs: Run[] }) {
  if (!runs.length) return null;
  return (
    <section className="panel running" aria-label="Plans being made">
      {runs.map((run) => (
        <Link key={run.id} to={`/runs/${run.id}`} className="running-row">
          <strong>{run.kind === "CAPTURE" ? "Database capture" : `Planning factory ${run.factory_id}, ${setLabel(run.order_offset, run.orders ?? 0)}`}</strong>
          <Progress value={run.progress} label={`Progress of run ${run.id}`} live={run.status === "RUNNING"} />
          <span className="muted small">{run.status === "QUEUED" ? (run.worker_down ? "Planning service not running" : "Waiting its turn") : stageText(run.stage)}</span>
        </Link>
      ))}
    </section>
  );
}

/** Busiest line and day in the plan, in worker-hours (operation time x crew), over the first two weeks. */
function busiest(rows: GanttRow[]) {
  const load = new Map<string, number>();
  for (const r of rows) {
    for (let d = Math.floor(r.start / DAY); d < Math.min(14, Math.ceil(r.end / DAY)); d++) {
      const overlap = Math.min(r.end, (d + 1) * DAY) - Math.max(r.start, d * DAY);
      if (overlap > 0) load.set(`${r.line}|${d}`, (load.get(`${r.line}|${d}`) ?? 0) + (overlap / 3600) * r.crew);
    }
  }
  let best: { line: string; day: number; hours: number } | null = null;
  for (const [k, hours] of load) {
    const [line, day] = k.split("|");
    if (!best || hours > best.hours) best = { line, day: Number(day), hours };
  }
  return best;
}

function greeting() {
  const h = new Date().getHours();
  return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
}

/** The day in one plain sentence: what is overdue and what waits for this person. */
function headline(overdue: number | undefined, waiting: number, username: string) {
  const parts: string[] = [];
  // Past due in the factory database (not the plan's late count, which the hero card shows).
  if (overdue) parts.push(overdue === 1 ? "One order is past due and not shipped" : `${overdue} orders are past due and not shipped`);
  if (waiting) parts.push(waiting === 1 ? "a plan is waiting for your decision" : `${waiting} plans are waiting for your decision`);
  if (!parts.length) return `${greeting()}, ${username}`;
  const text = parts.join(" and ");
  return `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
}

// rank: lower shows first; only the top three are shown.
interface Insight { icon: ReactNode; tone: "late" | "warn" | "info"; title: string; text: string; to: string; action: string; rank: number }

export default function Dashboard() {
  const session = useSession();
  const ready = useReadyScope();
  const { dataset, factoryId } = useScope();
  const summary = useQuery({ queryKey: ["erp-summary", ready], queryFn: () => erp.summary(ready![0], ready![1]), enabled: Boolean(ready), refetchInterval: 60_000 });
  const plan = useQuery({ queryKey: ["current-plan", ready], queryFn: () => erp.currentPlan(ready![0], ready![1]), enabled: Boolean(ready), refetchInterval: 60_000 });
  const machines = useQuery({ queryKey: ["erp-machines", ready, "all"], enabled: Boolean(ready),
    queryFn: () => erp.machines(ready![0], ready![1], { size: 200 }) });
  const board = useQuery({ queryKey: ["floor-board", ready, undefined], enabled: Boolean(ready) && can(session, "floor.view_floor"),
    queryFn: () => floor.board(ready![0], ready![1]), refetchInterval: 30_000 });
  const lines = useFloorLines(ready?.[0], ready?.[1]);
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs, refetchInterval: 10_000 });
  const [picked, setPicked] = useState<OrderRow | null>(null);
  const [asking, setAsking] = useState(false);
  // The sidebar's Ask CRAFT card opens this page with ?ask=1.
  const [params, setParams] = useSearchParams();
  useEffect(() => {
    if (params.get("ask") !== "1") return;
    setAsking(true);
    setParams((p) => { p.delete("ask"); return p; }, { replace: true });
  }, [params, setParams]);
  const updated = useAgo(plan.dataUpdatedAt || undefined);

  const digest = plan.data?.digest;
  const run = plan.data?.run;
  const orderRows = useMemo(() => digest?.order_rows ?? [], [digest]);
  const gantt = useMemo(() => digest?.gantt.CRAFT_V3 ?? [], [digest]);
  const late = useMemo(() => orderRows.filter((r) => isLate(r)).sort((a, b) => (daysLate(a) ?? 1e9) - (daysLate(b) ?? 1e9)), [orderRows]);
  const allMachines = useMemo(() => machines.data?.items ?? [], [machines.data]);
  const notRunning = allMachines.filter((m) => m.current_status !== "Active");

  const insights: Insight[] = useMemo(() => {
    const out: Insight[] = [];
    const v3 = run?.summary.craft_v3;
    if (digest && run) {
      if (v3?.mean_tardiness_days != null && late.length) out.push({ rank: 3, icon: <BarChart3 size={16} />, tone: "late", to: `/runs/${run.id}`, action: "See the late orders",
        title: `Orders finish ${num(v3.mean_tardiness_days, 1)} days late on average`, text: "Averaged over every order in this plan; orders on time count as zero." });
      const peak = busiest(gantt);
      if (peak) out.push({ rank: 4, icon: <BarChart3 size={16} />, tone: "warn", to: `/runs/${run.id}`, action: "Line load",
        title: `${lineName(lines.data?.lines, Number(peak.line))} is busiest on day ${peak.day + 1}`, text: `${Math.round(peak.hours)} worker-hours planned that day.` });
    }
    if (notRunning.length) out.push({ rank: 2, icon: <Cog size={16} />, tone: "warn", to: "/machines", action: "Machine register",
      title: `${notRunning.length} machine${notRunning.length === 1 ? "" : "s"} not running`,
      text: notRunning.slice(0, 3).map((m) => `Machine ${m.asset_id}: ${String(m.current_status).replace("_", " ").toLowerCase()}`).join("; ") + "." });
    const open = board.data?.open_issues ?? 0;
    if (open) out.push({ rank: 1, icon: <Wrench size={16} />, tone: "info", to: "/floor/issues", action: "Problems",
      title: `${open} open problem${open === 1 ? "" : "s"} on the floor`, text: "Reported from the shop floor and not yet closed." });
    return out.sort((a, b) => a.rank - b.rank).slice(0, 3);
  }, [digest, run, late, gantt, notRunning, board.data, lines.data]);

  if (!ready) return <Loading label="Loading factory data" />;
  const s = summary.data;
  const active = (runs.data ?? []).filter((r) => r.status === "QUEUED" || r.status === "RUNNING");
  const waiting = can(session, "planning.approve_plan") ? (runs.data ?? []).filter((r) => r.kind === "SCHEDULE" && r.status === "SUCCEEDED"
    && r.decision === "PENDING" && r.purpose === "PLAN" && r.factory_id === factoryId) : [];
  const machinesTotal = s ? Object.values(s.machines_by_status).reduce((a, b) => a + b, 0) : 0;
  const present = s ? s.rostered_today - s.on_leave_today : 0, running = s?.machines_by_status.Active ?? 0;
  const refs = lines.data?.lines;
  const pickOrder = (id: string) => setPicked(orderRows.find((r) => r.order_id === id) ?? null);

  return (
    <>
      <header className="cc-head">
        <div>
          <h1>{headline(s?.orders_overdue, waiting.length, session.username ?? "")}</h1>
          <p>Factory {factoryId}, {dataset?.label}. Data as of {s?.reference_date ?? dataset?.reference_time.slice(0, 10)}.</p>
        </div>
        <div className="cc-actions">
          {run && <span className={`live-chip${run.decision === "APPROVED" ? "" : " pending"}`}><i />{run.decision === "APPROVED" ? "Plan approved" : "Latest plan, not approved"}{updated ? `, checked ${updated}` : ""}</span>}
          <button type="button" className="btn" onClick={() => setAsking(true)}><Sparkles size={16} aria-hidden="true" />Ask CRAFT</button>
          {waiting.length > 0 && <Link className="btn primary" to={`/runs/${waiting[0].id}`}>Review the plan</Link>}
          {session.can_request && <Link className={`btn${waiting.length ? "" : " primary"}`} to="/plan">New plan</Link>}
        </div>
      </header>

      <Running runs={active} />

      <section className="focus" aria-label="The plan and the factory at a glance">
        {plan.isPending ? <Loading label="Reading the current plan" /> : digest ? (
          <PlanHealth rows={orderRows} reference={digest.reference_time} runId={run?.id} approved={run?.decision === "APPROVED"} />
        ) : (
          <section className="panel section empty-state">
            <h2>No plan for this factory yet</h2>
            <p className="muted">Once a plan is made, every order's delivery and each line's work show here.</p>
            {session.can_request && <Link className="btn primary" to="/plan">Make a plan</Link>}
          </section>
        )}
        {summary.isError ? <Notice tone="error">{summary.error.message}</Notice> : !s ? <Loading label="Reading the factory database" /> : (
          <div className="stat-grid">
            <StatTile label="Past due, not shipped" value={<CountUp value={s.orders_overdue} />} share={s.orders_overdue / Math.max(1, s.orders_total)}
              note={`of ${s.orders_total} orders in the factory`} tone={s.orders_overdue ? "late" : "ok"} to="/orders?status=OVERDUE" />
            <StatTile label="Due in 7 days, not started" value={<CountUp value={s.orders_due_7_days_not_started} />} share={s.orders_due_7_days_not_started / Math.max(1, s.orders_total)}
              note={`of ${s.orders_total} orders`} tone={s.orders_due_7_days_not_started ? "late" : undefined} to="/orders" />
            <StatTile label="Machines running" value={<><CountUp value={running} /><small>/{machinesTotal}</small></>} share={running / Math.max(1, machinesTotal)}
              note={machinesTotal - running ? `${machinesTotal - running} not running` : "All running"} tone="ok" to="/machines" />
            <StatTile label="People present" value={<><CountUp value={present} /><small>/{s.rostered_today}</small></>} share={present / Math.max(1, s.rostered_today)}
              note={`${s.on_leave_today} on leave today`} to="/workforce" />
          </div>
        )}
      </section>

      <section className="panel section cc-insights" aria-labelledby="ins-title">
        <div className="section-head"><h2 id="ins-title">Needs attention</h2><span className="live-chip small"><i />Live</span></div>
        {!insights.length ? <p className="muted">Nothing needs attention right now.</p> : (
          <ul className="insights attention">{insights.map((i) => (
            <li key={i.title} className={i.tone}>
              <span className="ins-icon" aria-hidden="true">{i.icon}</span>
              <span><strong>{i.title}</strong><small>{i.text}</small><Link to={i.to}>{i.action}</Link></span>
            </li>))}</ul>
        )}
        <button type="button" className="ask-soon ask-open" onClick={() => setAsking(true)}><Sparkles size={16} aria-hidden="true" />Ask CRAFT about this plan, stock, shipments or quality</button>
      </section>

      {digest && (
        <>
          <DeliveryBoard rows={orderRows} reference={digest.reference_time} onPick={setPicked} />
          <LineTimeline rows={gantt} orders={orderRows} reference={digest.reference_time} onPick={pickOrder}
            lineLabel={(line) => lineName(refs, line)} />
          <section className="panel section"><div className="section-head"><h2>Orders delivered over time</h2>{run && <Link to={`/runs/${run.id}`}>Open the plan</Link>}</div>
            <DeliveryCurve rows={digest.order_rows} reference={digest.reference_time} /></section>
        </>
      )}

      <OrderDrawer row={picked} reference={digest?.reference_time ?? ""} runId={run?.id} onClose={() => setPicked(null)}
        steps={picked ? gantt.filter((g) => g.order_id === picked.order_id) : []} />
      <AskCraft open={asking} onClose={() => setAsking(false)} onOrder={(id) => { setAsking(false); pickOrder(id); }} />
    </>
  );
}
