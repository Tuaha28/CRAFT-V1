import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Chip } from "./DataTable";

/** Order ids marked Priority in this factory (empty while loading or without planning access). */
export function usePriority(d: number | undefined, f: number | undefined, enabled = true): string[] {
  const q = useQuery({ queryKey: ["priority", d, f], queryFn: () => api.priority(d!, f!), enabled: Boolean(d && f && enabled), retry: false });
  return q.data?.orders ?? [];
}

export function PriorityChip({ on }: { on: boolean }) {
  return on ? <Chip value="Priority" /> : null;
}

/** Mark or unmark one order; every new plan and re-plan for the factory lets CRAFT schedule it first. */
export function PriorityControl({ d, f, orderId, canChange }: { d: number; f: number; orderId: string; canChange: boolean }) {
  const client = useQueryClient();
  const on = usePriority(d, f).includes(orderId);
  const [reason, setReason] = useState("");
  const flip = useMutation({
    meta: { success: on ? "Priority removed." : "Marked as Priority. The next plan schedules this order first." },
    mutationFn: () => api.setPriority(d, f, orderId, !on, reason.trim()),
    onSuccess: (data) => { client.setQueryData(["priority", d, f], data); setReason(""); },
  });
  return (
    <section className="panel section" aria-labelledby="priority-title">
      <div className="section-head"><h2 id="priority-title">Priority</h2><PriorityChip on={on} /></div>
      <p className="muted small">{on ? "CRAFT schedules this order first in every new plan for this factory. Other orders may finish later."
        : "Mark this order as Priority to have CRAFT schedule it first in the next plan."}</p>
      {canChange ? (
        <>
          <div className="field"><label htmlFor="priority-reason">Reason (optional)</label>
            <input id="priority-reason" value={reason} maxLength={300} onChange={(e) => setReason(e.target.value)}
              placeholder={on ? "Why it no longer needs priority" : "e.g. customer moved the ship date"} /></div>
          <button type="button" className={`btn${on ? "" : " primary"}`} disabled={flip.isPending} onClick={() => flip.mutate()}>
            {on ? "Remove Priority" : "Mark as Priority"}</button>
        </>
      ) : <p className="muted small">Only a planner or production manager can change Priority.</p>}
    </section>
  );
}
