import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { floor } from "../../lib/api";
import { num, when } from "../../lib/format";
import { useReadyScope } from "../../app/scope";
import { useSession } from "../../app/session";
import { CheckForm, LinePicker, lineName, useFloorLines } from "../../components/Floor";
import { Meter } from "../../components/DataTable";
import { Loading, Measure, Notice, PageHead, PageTabs, QUALITY_TABS } from "../../components/ui";

/** Quality checks recorded on the floor against the approved plan (defects per hundred units, DHU). */
export default function QualityChecks() {
  const session = useSession();
  const ready = useReadyScope();
  const lines = useFloorLines(ready?.[0], ready?.[1]);
  const checks = useQuery({ queryKey: ["floor-checks", ready], queryFn: () => floor.checks(ready![0], ready![1]), enabled: Boolean(ready) });
  const [adding, setAdding] = useState(false);
  const [line, setLine] = useState<number | null>(null);
  const planLines = lines.data?.plan?.lines ?? [];
  const lineId = session.scope_line ?? line ?? planLines[0] ?? null;
  const day = useQuery({ queryKey: ["floor-line", ready, lineId, null], queryFn: () => floor.lineDay(ready![0], ready![1], lineId!),
    enabled: Boolean(adding && ready && lineId) });

  if (!ready || checks.isPending) return <Loading label="Loading quality checks" />;
  if (checks.isError) return <Notice tone="error">{checks.error.message}</Notice>;
  const c = checks.data;
  const checked = c.by_line.reduce((a, r) => a + r.checked, 0), defects = c.by_line.reduce((a, r) => a + r.defects, 0);
  const canCheck = session.perms.includes("floor.record_defect");
  const worst = Math.max(1, ...c.by_type.map((t) => t.defects));
  return (
    <>
      <PageHead title="Quality checks" lead="Defects per 100 pieces checked (DHU). Below 5 is a common target.">
        {canCheck && c.plan && <button type="button" className="btn primary" onClick={() => setAdding(!adding)}>Record a check</button>}
      </PageHead>
      <PageTabs items={QUALITY_TABS} label="Quality" />
      {!c.plan ? <div className="panel empty"><p>No plan has been approved for this factory yet. Checks are recorded against the approved plan's operations.</p></div> : (
        <>
          {adding && lines.data && (
            <section className="panel section"><h2>Record a check</h2>
              <div className="toolbar"><LinePicker lines={lines.data.lines.filter((l) => planLines.includes(l.line_id))} value={lineId}
                locked={session.scope_line !== null} onChange={setLine} /></div>
              {day.isPending ? <Loading /> : day.isError ? <Notice tone="error">{day.error.message}</Notice> : lineId && (
                <>
                  <p className="small muted">Operations planned on {lineName(lines.data.lines, lineId)} on {day.data.date}. Choose another day on the line page.</p>
                  <CheckForm key={`${lineId}-${day.data.day}`} d={ready[0]} f={ready[1]} lineId={lineId} day={day.data.day} ops={day.data.operations}
                    types={day.data.defect_types} onDone={() => setAdding(false)} />
                </>)}
            </section>)}
          <div className="measures">
            <Measure value={checked.toLocaleString()} label="pieces checked" />
            <Measure value={defects.toLocaleString()} label="defects found" />
            <Measure value={checked ? num((100 * defects) / checked, 1) : "—"} label="DHU" tone={checked && (100 * defects) / checked > 5 ? "late" : checked ? "ontime" : undefined} />
          </div>
          <details className="panel section more">
            <summary>Show checks by line and the most common defects</summary>
            <div className="grid-2">
            <section className="section"><h2>By line</h2>
              {!c.by_line.length ? <p className="muted">No checks yet.</p> : (
                <table><thead><tr><th>Line</th><th className="num">Checked</th><th className="num">Defects</th><th className="num">DHU</th></tr></thead>
                  <tbody>{c.by_line.map((r) => (
                    <tr key={r.line_id}><td>{lineName(lines.data?.lines, r.line_id)}</td><td className="num">{r.checked}</td><td className="num">{r.defects}</td>
                      <td className={`num${r.dhu > 5 ? " late-text" : ""}`}>{num(r.dhu, 1)}</td></tr>))}</tbody></table>)}
            </section>
            <section className="section"><h2>Most common defects</h2>
              {!c.by_type.length ? <p className="muted">No defects recorded.</p> : (
                <table><tbody>{c.by_type.map((t) => (
                  <tr key={t.defect_type}><td>{t.defect_type}</td><td style={{ width: "50%" }}><Meter value={t.defects} max={worst} /></td><td className="num">{t.defects}</td></tr>))}</tbody></table>)}
            </section>
            </div>
          </details>
          <section className="panel section"><h2>Recent checks</h2>
            {!c.checks.length ? <p className="muted">No checks recorded yet.</p> : (
              <div className="table-wrap"><table>
                <thead><tr><th>When</th><th>Line</th><th>Order / step</th><th className="num">Checked</th><th className="num">Defects</th><th className="num">DHU</th><th>Main defect</th></tr></thead>
                <tbody>{c.checks.map((k) => (
                  <tr key={k.id}><td className="small">{k.date}<div className="muted">{k.entered_by}, {when(k.created_at)}</div></td><td>{lineName(lines.data?.lines, k.line_id)}</td>
                    <td>{k.order_id} / {k.step}<div className="small muted">{k.operation}</div></td><td className="num">{k.checked}</td><td className="num">{k.defects}</td>
                    <td className={`num${k.dhu > 5 ? " late-text" : ""}`}>{num(k.dhu, 1)}</td><td>{k.defect_type || "—"}{k.note && <div className="small muted">{k.note}</div>}</td></tr>))}</tbody>
              </table></div>)}
          </section>
        </>)}
    </>
  );
}
