import { Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type Run } from "../lib/api";

/** "Started by CRAFT: …" on a plan that auto-planning made, and the replaced state. Nothing for plans people started. */
export function AutoBanner({ run }: { run: Run }) {
  if (run.trigger?.source !== "AUTO") return null;
  return (
    <section className="panel section auto-banner" aria-label="Started by CRAFT">
      <p><strong>Started by CRAFT: {run.trigger.reason || "changes on the floor"}</strong></p>
      <p className="muted small">CRAFT made this plan by itself after problems on the floor. It is planned from the factory data as if it stays that way for the whole plan. A manager approves every plan.</p>
      {run.decision === "SUPERSEDED" && <p className="warn-text">Replaced by a newer CRAFT plan</p>}
    </section>
  );
}

/** The Auto-planning switch for one factory, with the email status line. */
export function AutoPlanSwitch({ d, f, canChange }: { d: number; f: number; canChange: boolean }) {
  const client = useQueryClient();
  const state = useQuery({ queryKey: ["autoplan", d, f], queryFn: () => api.autoplan(d, f) });
  const flip = useMutation({
    meta: { success: "Auto-planning updated." },
    mutationFn: (on: boolean) => api.setAutoplan(d, f, on),
    onSuccess: (data) => client.setQueryData(["autoplan", d, f], data),
  });
  const all = useMutation({
    meta: { success: (on: unknown) => `Auto-planning ${on ? "on" : "off"} for every factory of this data.` },
    mutationFn: (on: boolean) => api.setAutoplanAll(d, on),
    onSuccess: () => client.invalidateQueries({ queryKey: ["autoplan"] }),
  });
  if (!state.data) return null;
  const on = state.data.enabled;
  return (
    <section className="panel section autoplan" aria-labelledby="autoplan-title">
      <div className="section-head">
        <h2 id="autoplan-title">Auto-planning</h2>
        <label className="switch">
          <input type="checkbox" role="switch" aria-checked={on} checked={on} disabled={!canChange || flip.isPending}
            onChange={(e) => flip.mutate(e.target.checked)} />
          <span>{on ? "On" : "Off"} for factory {f}</span>
        </label>
      </div>
      <p className="muted small">CRAFT makes the factory's first plan, the next plan when the current one is nearly done, and a new plan after problems on the floor, and emails the managers. A manager approves every plan.</p>
      {canChange && (
        <div className="toolbar">
          <button type="button" className="btn" disabled={all.isPending} onClick={() => all.mutate(true)}>Turn on for all factories</button>
          <button type="button" className="btn" disabled={all.isPending} onClick={() => all.mutate(false)}>Turn off for all factories</button>
        </div>)}
      {state.data.email === "OUTBOX" && <p className="muted small">Email is not set up; messages are saved to the outbox.</p>}
    </section>
  );
}

/** Home card: an automatic plan is waiting for review in this factory. */
export function ReviewCard({ d, f }: { d: number; f: number }) {
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs });
  const waiting = (runs.data ?? []).find((r) => r.trigger?.source === "AUTO" && r.status === "SUCCEEDED"
    && r.decision === "PENDING" && r.factory_id === f && r.dataset?.id === d);
  if (!waiting) return null;
  return (
    <section className="panel section hero review-card" aria-label="New plan from CRAFT">
      <h2>CRAFT has a new plan ready for you to review</h2>
      <p className="muted">{waiting.trigger?.reason}</p>
      <Link className="btn primary" to={`/runs/${waiting.id}`}>Review the plan</Link>
    </section>
  );
}
