import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import { ArrowRight, ChevronRight } from "lucide-react";
import type { OrderRow } from "../lib/api";
import { num, planDate } from "../lib/format";
import { standing } from "../lib/standing";

/** The plan in one look: how many orders make it, the worst one, and when the last one finishes. */
export function PlanHealth({ rows, reference, runId, approved }: { rows: OrderRow[]; reference: string; runId?: string; approved: boolean }) {
  const items = rows.map((row) => ({ row, ...standing(row) }));
  const late = items.filter((i) => i.kind === "late" || i.kind === "open");
  const risk = items.filter((i) => i.kind === "risk").length;
  const ontime = items.filter((i) => i.kind === "ontime").length;
  const total = Math.max(1, items.length);
  const worst = [...late].sort((a, b) => (a.spare ?? -1e9) - (b.spare ?? -1e9))[0];
  const last = Math.max(0, ...items.map((i) => i.finish ?? 0));
  const tone = late.length ? "late" : risk ? "warn" : "ok";
  return (
    <section className={`panel plan-health ${tone}`} aria-labelledby="ph-title">
      <span className="eyebrow">{approved ? "Approved plan" : "Latest plan, not approved yet"}</span>
      <h2 id="ph-title" className="ph-big">
        {late.length ? <><strong>{late.length}</strong> of {items.length} orders late</> : <><strong>All {items.length}</strong> orders on time</>}
      </h2>
      <p className="ph-sub">
        {worst ? <>Worst: order {worst.row.order_id}, {worst.spare == null ? "does not finish in this plan" : `${num(-worst.spare, 1)} days late`}. </> : null}
        Last order finishes {planDate(reference, last)}.
      </p>
      <div className="ph-bar" role="img" aria-label={`${late.length} late, ${risk} at risk, ${ontime} on time`}>
        {late.length > 0 && <i className="late" style={{ flexGrow: late.length / total }} />}
        {risk > 0 && <i className="risk" style={{ flexGrow: risk / total }} />}
        {ontime > 0 && <i className="ontime" style={{ flexGrow: ontime / total }} />}
      </div>
      <div className="ph-foot">
        <span className="ph-key"><i className="late" />{late.length} late</span>
        <span className="ph-key"><i className="risk" />{risk} at risk</span>
        <span className="ph-key"><i className="ontime" />{ontime} on time</span>
        {runId && <Link className="ph-link" to={`/runs/${runId}`}>Open the plan<ArrowRight size={15} aria-hidden="true" /></Link>}
      </div>
    </section>
  );
}

/** One factory figure: the number, how much of the whole it is, and where to look closer. */
export function StatTile({ label, value, share, note, tone, to }:
  { label: string; value: ReactNode; share: number; note: string; tone?: "late" | "ok"; to: string }) {
  return (
    <Link className={`panel stat-tile${tone ? ` ${tone}` : ""}`} to={to}>
      <span className="stat-label">{label}<ChevronRight size={15} aria-hidden="true" /></span>
      <span className="stat-value">{value}</span>
      <span className="stat-meter" aria-hidden="true"><i style={{ width: `${Math.round(Math.max(0, Math.min(1, share)) * 100)}%` }} /></span>
      <span className="stat-note">{note}</span>
    </Link>
  );
}
