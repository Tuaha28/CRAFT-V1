import { Fragment, useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { erp, floor, type FloorOp, type LineDay, type LineState } from "../../lib/api";
import { num } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { can, useSession } from "../../app/session";
import { CheckForm, DayNav, HourBars, LinePicker, lineName, ReportProblem, StateChip, useFloorLines } from "../../components/Floor";
import { FactoryMap, type Hall, type MachineState } from "../../components/FactoryMap";
import { Loading, Notice } from "../../components/ui";
import { CompleteStep, ReopenStep, ReworkList, StepChip } from "../../components/Completion";

/** One cell of the hourly grid: planned pieces, and an input for the recorded pieces. */
function HourCell({ d, f, view, op, hour }: { d: number; f: number; view: LineDay; op: FloorOp; hour: number }) {
  const client = useQueryClient();
  const target = op.hourly_target[String(hour)];
  const recorded = view.summary.actual_by_op[op.key]?.[String(hour)];
  const [value, setValue] = useState(recorded === undefined ? "" : String(recorded));
  useEffect(() => setValue(recorded === undefined ? "" : String(recorded)), [recorded]);
  const save = useMutation({
    mutationFn: (pieces: number) => floor.output(d, f, { line_id: view.line_id, day: view.day, hour, operation_key: op.key, pieces }),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: ["floor-line"] });
      client.invalidateQueries({ queryKey: ["floor-board"] });
    },
  });
  const commit = () => {
    if (value === "" || Number(value) === recorded) return;
    save.mutate(Number(value));
  };
  const late = recorded !== undefined && target !== undefined && recorded < 0.85 * target;
  if (!view.can_enter) {
    return <td className={`num${late ? " late-text" : ""}`}>{recorded ?? "—"}<small>{target !== undefined ? ` / ${Math.round(target)}` : ""}</small></td>;
  }
  return (
    <td className={`hour-cell${save.isError ? " error" : ""}${save.isSuccess ? " saved" : ""}${late ? " behind" : ""}`}>
      <input type="number" min={0} inputMode="numeric" value={value} aria-label={`Pieces of order ${op.order_id} step ${op.step} at ${hour}:00`}
        onChange={(e) => setValue(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") commit(); }}
        aria-invalid={save.isError || undefined} aria-describedby={save.isError ? `err-${op.key}-${hour}` : undefined} />
      <small className="muted">{target !== undefined ? `plan ${Math.round(target)}` : "no plan"}</small>
      {save.isError && <small id={`err-${op.key}-${hour}`} className="late-text cell-error" role="alert">Not saved: {save.error.message}</small>}
    </td>
  );
}

const RING = 2 * Math.PI * 62;
const STATE_HELP: Record<LineState, string> = {
  NO_DATA: "No output entered yet. Type each hour's pieces below; the target follows the plan.",
  ON_TRACK: "The line is keeping up with the plan.",
  BEHIND: "The line is behind the plan. Hours in red are under 85% of their target.",
  NOT_STARTED: "The first planned hour has not started yet.",
  IDLE: "No work is planned on this line on this day.",
};

/** Pieces sewn so far, as a ring filling toward the day's plan. */
function OutputRing({ view }: { view: LineDay }) {
  const s = view.summary;
  const share = s.target_day ? Math.min(1, s.actual_so_far / s.target_day) : 0;
  const stroke = s.state === "BEHIND" ? "var(--late)" : s.state === "ON_TRACK" ? "var(--ontime)" : "var(--accent)";
  return (
    <section className="panel line-ring" aria-label="Pieces sewn so far against the plan">
      <div className="ring-big">
        <svg viewBox="0 0 150 150" aria-hidden="true">
          <circle cx="75" cy="75" r="62" fill="none" stroke="rgba(148,163,184,.16)" strokeWidth="12" />
          <circle className="ring-fill-big" cx="75" cy="75" r="62" fill="none" stroke={stroke} strokeWidth="12" strokeLinecap="round"
            strokeDasharray={RING} strokeDashoffset={RING * (1 - share)} transform="rotate(-90 75 75)" />
        </svg>
        <span><strong>{s.actual_so_far.toLocaleString()}</strong><small>of {s.target_day.toLocaleString()}</small></span>
      </div>
      <div className="line-ring-text">
        <h2>Pieces sewn so far</h2>
        <StateChip state={s.state} />
        <p className="muted">{STATE_HELP[s.state]}</p>
        <dl className="line-facts">
          <div><dt>Planned by now</dt><dd>{s.target_so_far.toLocaleString()}</dd></div>
          <div><dt>Of plan so far</dt><dd className={s.state === "BEHIND" ? "late-text" : undefined}>{s.efficiency_pct === null ? "—" : `${num(s.efficiency_pct, 0)}%`}</dd></div>
          <div><dt>Open problems</dt><dd className={view.issues.length ? "late-text" : undefined}>{view.issues.length}</dd></div>
          <div><dt>Defects per 100 (DHU)</dt><dd className={s.dhu !== null && s.dhu > 5 ? "late-text" : undefined}>{s.dhu === null ? "—" : num(s.dhu, 1)}</dd></div>
        </dl>
      </div>
    </section>
  );
}

export default function LineToday() {
  const session = useSession();
  const ready = useReadyScope();
  const [params, setParams] = useSearchParams();
  const lines = useFloorLines(ready?.[0], ready?.[1]);
  const scope = session.scope_line;
  const planLines = lines.data?.plan?.lines ?? [];
  const lineId = scope ?? (Number(params.get("line")) || planLines[0] || null);
  const dayParam = params.get("day");
  const view = useQuery({
    queryKey: ["floor-line", ready, lineId, dayParam],
    queryFn: () => floor.lineDay(ready![0], ready![1], lineId!, dayParam === null ? undefined : Number(dayParam)),
    enabled: Boolean(ready && lineId && lines.data?.plan), refetchInterval: 60_000,
  });
  // Same query as the dashboard map, so the machines come from one cache.
  const machines = useQuery({ queryKey: ["erp-machines", ready, "all"], enabled: Boolean(ready),
    queryFn: () => erp.machines(ready![0], ready![1], { size: 200 }) });
  const [panel, setPanel] = useState<"none" | "report" | "check">("none");
  const [tab, setTab] = useState<"hours" | "work" | "machines">("hours");
  const [acting, setActing] = useState<{ key: string; kind: "complete" | "reopen" } | null>(null);
  const set = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    Object.entries(changes).forEach(([k, v]) => next.set(k, v));
    setParams(next, { replace: true });
  };

  const v = view.data;
  const lineNumber = lines.data?.lines.find((l) => l.line_id === lineId)?.line_number;
  const hall: Hall | null = useMemo(() => {
    const ms = (machines.data?.items ?? []).filter((m) => lineNumber && String(m.line ?? "") === lineNumber);
    if (!ms.length) return null;
    const inPlan = new Set((v?.operations ?? []).flatMap((o) => o.machines.map(String)));
    const toState = (m: (typeof ms)[number]): MachineState => m.current_status === "Breakdown" ? "down"
      : m.current_status === "Under_Maintenance" ? "fix" : inPlan.has(String(m.asset_id)) ? "run" : "idle";
    return { key: lineNumber!, name: lineNumber!, to: "", tone: "", toneColor: "", figure: "", note: "",
      machines: ms.map((m) => ({ id: String(m.asset_id), state: toState(m) })) };
  }, [machines.data, lineNumber, v]);

  if (!ready || lines.isPending) return <Loading label="Loading the line" />;
  if (lines.isError) return <Notice tone="error">{lines.error.message}</Notice>;
  const name = lineName(lines.data.lines, lineId);
  if (!lines.data.plan) {
    return (
      <>
        <header className="cc-head"><div><h1>{name}</h1></div></header>
        <div className="panel empty"><p>No plan has been approved for this factory yet. When the production manager approves a CRAFT plan, each line's work appears here.</p>
          {session.can_approve && <Link className="btn primary" to="/runs">Choose a plan to approve</Link>}</div>
      </>);
  }
  const hours = v ? [...new Set(v.operations.flatMap((o) => Object.keys(o.hourly_target).map(Number)))].sort((a, b) => a - b) : [];
  const s = v?.summary;
  const count = (state: MachineState) => hall?.machines.filter((m) => m.state === state).length ?? 0;
  return (
    <>
      <header className="cc-head">
        <div>
          <h1>{name}{v && v.day === v.today ? ", today" : ""}</h1>
          <p className="muted">{v ? `${new Date(`${v.date}T00:00:00`).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" })}. ` : ""}
            From the CRAFT plan approved by {lines.data.plan.approved_by ?? "the production manager"}.</p>
        </div>
        <div className="cc-actions no-print">
          {can(session, "floor.report_issue") && <button type="button" className="btn primary" aria-expanded={panel === "report"} onClick={() => setPanel(panel === "report" ? "none" : "report")}>Report a problem</button>}
          {v?.can_check && <button type="button" className="btn" aria-expanded={panel === "check"} onClick={() => setPanel(panel === "check" ? "none" : "check")}>Record a quality check</button>}
          <button type="button" className="btn" onClick={() => window.print()}>Print</button>
        </div>
      </header>

      <div className="toolbar no-print">
        <LinePicker lines={lines.data.lines.filter((l) => planLines.includes(l.line_id))} value={lineId} locked={scope !== null}
          onChange={(id) => set({ line: String(id) })} />
        {v && <DayNav days={v.days} day={v.day} today={v.today} date={v.date} onChange={(d) => set({ day: String(d) })} />}
      </div>

      {panel === "report" && v && ready && (
        <section className="panel section no-print"><h2>Report a problem</h2>
          <ReportProblem d={ready[0]} f={ready[1]} lineId={lineId} ops={v.operations} onDone={() => setPanel("none")} /></section>)}
      {panel === "check" && v && ready && lineId && (
        <section className="panel section no-print"><h2>Quality check</h2>
          <CheckForm d={ready[0]} f={ready[1]} lineId={lineId} day={v.day} ops={v.operations} types={v.defect_types} onDone={() => setPanel("none")} /></section>)}

      {view.isPending ? <Loading label="Loading the line plan" /> : view.isError ? <Notice tone="error">{view.error.message}</Notice> : v && s && (
        <>
          <OutputRing view={v} />

          {v.issues.length > 0 && (
            <ul className="alerts">{v.issues.map((i) => (
              <li key={i.id} className={i.status === "IN_PROGRESS" ? "warn" : undefined}>
                <span>{i.kind_label}{i.machine_id ? `: machine ${i.machine_id}` : i.worker_id ? `: worker ${i.worker_id}` : i.order_id ? `: order ${i.order_id}` : ""}{i.note ? `. ${i.note}` : ""}</span>
                <span className="small muted">{i.status_label}, reported by {i.reported_by}</span></li>))}</ul>)}

          <section className="panel section">
            <div className="tabs plan-tabs" role="tablist" aria-label="Line details">
              {([["hours", "Pieces by hour"], ["work", `Work today (${v.operations.length})`], ["machines", "Machines"]] as const).map(([key, label]) => (
                <button key={key} type="button" role="tab" id={`ltab-${key}`} aria-selected={tab === key} aria-controls="line-tab"
                  className={tab === key ? "on" : ""} onClick={() => setTab(key)}>{label}</button>))}
            </div>
            <div id="line-tab" role="tabpanel" aria-labelledby={`ltab-${tab}`} className="section">
              {tab === "hours" && (<>
                <p className="muted small">{v.can_enter ? "Type the pieces finished in each hour. It saves when you leave the box or press Enter." : "Recorded and planned pieces per hour."}</p>
                <HourBars hours={s.hours} passed={s.hours_passed} />
                {!hours.length ? <p className="muted">No work is planned on this line on this day.</p> : (
                  <div className="table-wrap" tabIndex={0} role="region" aria-label="Pieces by hour"><table className="hour-grid">
                    <thead><tr><th>Order and step</th>{hours.map((h) => <th key={h} className="num">{String(h).padStart(2, "0")}:00</th>)}</tr></thead>
                    <tbody>{v.operations.filter((o) => Object.keys(o.hourly_target).length).map((o) => (
                      <tr key={o.key}><th scope="row">Order {o.order_id}, step {o.step}<div className="small muted">{o.name}</div><StepChip status={o.status} /></th>
                        {hours.map((h) => o.hourly_target[String(h)] === undefined && v.summary.actual_by_op[o.key]?.[String(h)] === undefined
                          ? <td key={h} className="muted num">·</td>
                          : <HourCell key={h} d={ready![0]} f={ready![1]} view={v} op={o} hour={h} />)}
                      </tr>))}</tbody>
                  </table></div>)}
              </>)}
              {tab === "work" && (!v.operations.length ? <p className="muted">No work is planned on this line on this day.</p> : (
                <div className="table-wrap" tabIndex={0} role="region" aria-label="Work today"><table>
                  <thead><tr><th>Time</th><th>Order, step</th><th>Operation</th><th>Crew</th><th className="num">Pieces today</th><th className="num">Done / total</th><th>Status</th></tr></thead>
                  <tbody>{v.operations.map((o) => {
                    const st = o.status, finished = st?.state === "COMPLETE" || st?.state === "SHORT";
                    const label = `order ${o.order_id}, step ${o.step}`;
                    return (<Fragment key={o.key}>
                    <tr>
                      <td>{o.windows.map(([x, y]) => `${x} to ${y}`).join(", ")}</td>
                      <td><Link to={`/orders/${o.order_id}`}>Order {o.order_id}</Link>, step {o.step}</td>
                      <td>{o.name ?? "—"}<div className="small muted">{o.machines.length} machine{o.machines.length === 1 ? "" : "s"}: {o.machines.join(", ")}</div></td>
                      <td>{o.worker_names.join(", ")}</td>
                      <td className="num">{o.planned_today}</td>
                      <td className="num">{st ? `${st.good.toLocaleString()} / ${st.needed.toLocaleString()}` : `${o.recorded_total} / ${o.pieces ?? "—"}`}
                        {st && st.defective > 0 && <div className="small late-text">{st.defective} for rework</div>}</td>
                      <td><StepChip status={st} />
                        {st?.state === "SHORT" && st.reason && <div className="small muted">{st.final} pieces: {st.reason}</div>}
                        {st && finished && st.by && <div className="small muted">by {st.by}</div>}
                        {st && !finished && !st.ready && <div className="small muted">Waits for step {st.previous}</div>}
                        {st && !finished && st.ready && v.can_complete && acting?.key !== o.key && (
                          <div><button type="button" className="btn small-btn" onClick={() => setActing({ key: o.key, kind: "complete" })}>Mark complete</button></div>)}
                        {st && finished && v.can_reopen && acting?.key !== o.key && (
                          <div><button type="button" className="btn small-btn" onClick={() => setActing({ key: o.key, kind: "reopen" })}>Reopen</button></div>)}
                      </td>
                    </tr>
                    {st && acting?.key === o.key && (
                      <tr className="act-row"><td colSpan={7}>{acting.kind === "complete"
                        ? <CompleteStep d={ready![0]} f={ready![1]} status={st} label={label} onDone={() => setActing(null)} />
                        : <ReopenStep d={ready![0]} f={ready![1]} status={st} label={label} onDone={() => setActing(null)} />}</td></tr>)}
                    </Fragment>);
                  })}</tbody>
                </table></div>))}
              {tab === "machines" && (machines.isPending ? <Loading label="Reading machines" /> : !hall ? <p className="muted">No machines are registered to this line.</p> : (
                <>
                  <FactoryMap halls={[hall]} plain />
                  <ul className="map-key">
                    <li><i className="k-run" />{count("run")} in today's plan</li>
                    <li><i className="k-fix" />{count("fix")} maintenance</li>
                    <li><i className="k-down" />{count("down")} breakdown</li>
                    <li><i className="k-idle" />{count("idle")} idle</li>
                  </ul>
                </>))}
            </div>
          </section>

          {(v.can_check || v.can_enter) && <ReworkList items={v.rework ?? []} labelOf={(key) => {
            const o = v.operations.find((x) => x.key === key);
            return o ? `Order ${o.order_id}, step ${o.step}` : key;
          }} />}
        </>)}
    </>
  );
}
