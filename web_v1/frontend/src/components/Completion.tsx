import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { floor, type OrderAction, type OrderProgress, type OrderStage, type ReworkItem, type StepState, type StepStatus } from "../lib/api";
import { Notice } from "./ui";

const STEP_TEXT: Record<StepState, string> = {
  NOT_STARTED: "Not started", IN_PROGRESS: "In progress", PAUSED: "Paused: machine down", COMPLETE: "Complete", SHORT: "Closed short",
};
const STEP_TONE: Record<StepState, string> = { NOT_STARTED: "", IN_PROGRESS: "", PAUSED: "bad", COMPLETE: "good", SHORT: "warn" };

export function StepChip({ status }: { status?: StepStatus }) {
  if (!status) return null;
  return <span className={`chip ${STEP_TONE[status.state]}`}>{STEP_TEXT[status.state]}</span>;
}

export const STAGE_TEXT: Record<OrderStage, string> = {
  NOT_STARTED: "Not started", IN_PRODUCTION: "In production", SEWN: "Sewn", QC_PASSED: "Inspection passed",
  QC_FAILED: "Inspection failed", PACKED: "Packed", SHIPPED: "Shipped", CLOSED: "Closed",
};
const STAGE_TONE: Record<OrderStage, string> = {
  NOT_STARTED: "", IN_PRODUCTION: "", SEWN: "good", QC_PASSED: "good", QC_FAILED: "bad", PACKED: "good", SHIPPED: "good", CLOSED: "",
};
export function StageChip({ stage }: { stage: OrderStage }) {
  return <span className={`chip ${STAGE_TONE[stage]}`}>{STAGE_TEXT[stage]}</span>;
}

function useRefresh() {
  const client = useQueryClient();
  return () => ["floor-line", "floor-board", "order-progress", "order-statuses", "erp-order"].forEach((k) => client.invalidateQueries({ queryKey: [k] }));
}

/** Mark a step complete: one click when every piece is done (recorded or not); fewer pieces need the real count and a reason. */
export function CompleteStep({ d, f, status, label, onDone }: { d: number; f: number; status: StepStatus; label: string; onDone: () => void }) {
  const refresh = useRefresh();
  const capped = status.limit < status.needed;  // the step before finished fewer pieces
  const [short, setShort] = useState(capped);
  const [pieces, setPieces] = useState(String(capped ? status.limit : status.recorded || ""));
  const [reason, setReason] = useState("");
  const send = useMutation({
    mutationFn: () => floor.completeStep(d, f, status.key, short ? { pieces: Number(pieces), reason } : {}),
    onSuccess: () => { refresh(); onDone(); },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); send.mutate(); };
  const id = `done-${status.key}`;
  return (
    <form className="complete-form" onSubmit={submit} aria-labelledby={`${id}-title`}>
      <p id={`${id}-title`}><strong>Mark {label} complete</strong></p>
      <p className="muted small">
        {status.recorded
          ? `${status.recorded.toLocaleString()} of ${status.needed.toLocaleString()} pieces are recorded.`
          : "No hourly pieces are recorded for this step."}{" "}
        {short ? "Enter how many were really finished and why." : `This records the step as finished with all its pieces${status.recorded < status.needed ? "; the missing pieces are added as one entry" : ""}.`}
      </p>
      {capped && <p className="small">Step {status.previous} finished {status.limit.toLocaleString()} pieces, so this step can have at most {status.limit.toLocaleString()}.</p>}
      {!capped && <label className="check-line"><input type="checkbox" checked={short} onChange={(e) => setShort(e.target.checked)} /> Fewer pieces were finished</label>}
      {short && (
        <div className="form-grid">
          <div className="field"><label htmlFor={`${id}-pieces`}>Pieces finished</label>
            <input id={`${id}-pieces`} type="number" min={status.recorded} max={status.limit} inputMode="numeric" required value={pieces} onChange={(e) => setPieces(e.target.value)} /></div>
          <div className="field wide"><label htmlFor={`${id}-reason`}>Reason</label>
            <input id={`${id}-reason`} required maxLength={2000} value={reason} placeholder="For example: 10 pieces rejected at cutting" onChange={(e) => setReason(e.target.value)} /></div>
        </div>)}
      <div className="form-actions">
        <button className="btn primary" disabled={send.isPending}>{send.isPending ? "Saving…" : short ? "Close step short" : "Mark complete"}</button>
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
      </div>
      {send.isError && <Notice tone="error">{send.error.message}</Notice>}
    </form>
  );
}

export function ReopenStep({ d, f, status, label, onDone }: { d: number; f: number; status: StepStatus; label: string; onDone: () => void }) {
  const refresh = useRefresh();
  const [reason, setReason] = useState("");
  const send = useMutation({ mutationFn: () => floor.reopenStep(d, f, status.key, reason), onSuccess: () => { refresh(); onDone(); } });
  const id = `reopen-${status.key}`;
  return (
    <form className="complete-form" onSubmit={(e) => { e.preventDefault(); send.mutate(); }}>
      <p><strong>Reopen {label}</strong></p>
      <p className="muted small">The step goes back to its recorded pieces{status.marked ? ", and pieces added by Mark complete are removed" : ""}. It stays open until someone marks it complete again, so its hourly pieces can be corrected.</p>
      <div className="field wide"><label htmlFor={id}>Reason</label>
        <input id={id} required maxLength={2000} value={reason} onChange={(e) => setReason(e.target.value)} /></div>
      <div className="form-actions">
        <button className="btn primary" disabled={send.isPending}>{send.isPending ? "Saving…" : "Reopen step"}</button>
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
      </div>
      {send.isError && <Notice tone="error">{send.error.message}</Notice>}
    </form>
  );
}

/** Defective pieces waiting for repair; they count as done again once marked reworked. */
export function ReworkList({ items, labelOf }: { items: ReworkItem[]; labelOf: (key: string) => string }) {
  const refresh = useRefresh();
  const send = useMutation({ mutationFn: (id: number) => floor.rework(id), onSuccess: refresh });
  if (!items.length) return null;
  return (
    <section className="panel section" aria-labelledby="rework-title">
      <h2 id="rework-title">Waiting for rework</h2>
      <p className="muted small">Defective pieces do not count as done until they are repaired.</p>
      <ul className="rework-list">{items.map((c) => (
        <li key={c.id}>
          <span><strong>{labelOf(c.operation_key)}</strong>: {c.pieces} defective piece{c.pieces === 1 ? "" : "s"}{c.defect_type ? ` (${c.defect_type})` : ""}{c.note ? `. ${c.note}` : ""}</span>
          <button type="button" className="btn" disabled={send.isPending} onClick={() => send.mutate(c.id)}>Mark reworked</button>
        </li>))}</ul>
      {send.isError && <Notice tone="error">{send.error.message}</Notice>}
    </section>
  );
}

const ACTION_TEXT: Record<OrderAction, string> = { inspect: "Final inspection", pack: "Record packing", ship: "Record shipment", close: "Close order", reopen: "Reopen order" };

function ActionForm({ d, f, order, action, onDone }: { d: number; f: number; order: OrderProgress; action: OrderAction; onDone: () => void }) {
  const refresh = useRefresh();
  const [values, setValues] = useState<Record<string, string>>({ result: "PASS" });
  const set = (k: string) => (e: { target: { value: string } }) => setValues((v) => ({ ...v, [k]: e.target.value }));
  const send = useMutation({
    mutationFn: () => floor.orderAction(d, f, order.order_id, action, Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""))),
    onSuccess: () => { refresh(); onDone(); },
  });
  const id = `oa-${action}`;
  const needsReason = action === "reopen" || (action === "close" && order.stage !== "SHIPPED");
  return (
    <form className="complete-form" onSubmit={(e) => { e.preventDefault(); send.mutate(); }}>
      <p><strong>{ACTION_TEXT[action]}</strong></p>
      {action === "inspect" && (<>
        <fieldset className="radio-row"><legend className="sr-only">Result</legend>
          <label><input type="radio" name={`${id}-r`} value="PASS" checked={values.result === "PASS"} onChange={set("result")} /> Pass</label>
          <label><input type="radio" name={`${id}-r`} value="FAIL" checked={values.result === "FAIL"} onChange={set("result")} /> Fail</label>
        </fieldset>
        <div className="field wide"><label htmlFor={`${id}-note`}>{values.result === "FAIL" ? "What failed (sent to the line for rework)" : "Note (optional)"}</label>
          <input id={`${id}-note`} maxLength={2000} required={values.result === "FAIL"} value={values.note ?? ""} onChange={set("note")} /></div>
      </>)}
      {action === "pack" && (
        <div className="field"><label htmlFor={`${id}-cartons`}>Cartons</label>
          <input id={`${id}-cartons`} type="number" min={1} inputMode="numeric" required value={values.cartons ?? ""} onChange={set("cartons")} /></div>)}
      {action === "ship" && (
        <div className="form-grid">
          <div className="field"><label htmlFor={`${id}-carrier`}>Carrier</label>
            <input id={`${id}-carrier`} required maxLength={100} value={values.carrier ?? ""} onChange={set("carrier")} /></div>
          <div className="field"><label htmlFor={`${id}-tracking`}>Tracking number</label>
            <input id={`${id}-tracking`} maxLength={100} value={values.tracking ?? ""} onChange={set("tracking")} /></div>
          <div className="field"><label htmlFor={`${id}-date`}>Shipped on</label>
            <input id={`${id}-date`} type="date" value={values.shipped_on ?? ""} onChange={set("shipped_on")} /><span className="hint">Empty: today</span></div>
        </div>)}
      {(action === "close" || action === "reopen") && (
        <div className="field wide"><label htmlFor={`${id}-reason`}>{needsReason ? "Reason" : "Reason (optional)"}</label>
          <input id={`${id}-reason`} maxLength={2000} required={needsReason} value={values.reason ?? ""} onChange={set("reason")} /></div>)}
      <div className="form-actions">
        <button className="btn primary" disabled={send.isPending}>{send.isPending ? "Saving…" : ACTION_TEXT[action]}</button>
        <button type="button" className="btn" onClick={onDone}>Cancel</button>
      </div>
      {send.isError && <Notice tone="error">{send.error.message}</Notice>}
    </form>
  );
}

export function useOrderProgress(d: number | undefined, f: number | undefined, orderId: string, enabled = true) {
  return useQuery({ queryKey: ["order-progress", d, f, orderId], queryFn: () => floor.order(d!, f!, orderId), enabled: Boolean(d && f && orderId && enabled) });
}

/** The order's finishing work in the app: stage, step completion and the actions this user may take. */
export function OrderFinishing({ d, f, order }: { d: number; f: number; order: OrderProgress }) {
  const [open, setOpen] = useState<OrderAction | null>(null);
  const s = order.status;
  const done = order.steps.filter((x) => x.state === "COMPLETE" || x.state === "SHORT").length;
  const actions = (Object.keys(ACTION_TEXT) as OrderAction[]).filter((a) => order.can[a]);
  return (
    <section className="panel section" aria-labelledby="finish-title">
      <h2 id="finish-title">Finishing</h2>
      <p><StageChip stage={order.stage} /></p>
      {order.in_plan
        ? <p className="muted small">{done} of {order.steps.length} steps complete.</p>
        : <p className="muted small">This order is not in an approved plan yet.</p>}
      {s && (
        <dl className="facts">
          {s.inspection_note && <><dt>Inspection</dt><dd>{s.inspection_note}</dd></>}
          {s.cartons != null && <><dt>Cartons</dt><dd>{s.cartons}</dd></>}
          {s.carrier && <><dt>Carrier</dt><dd>{s.carrier}</dd></>}
          {s.tracking && <><dt>Tracking</dt><dd>{s.tracking}</dd></>}
          {s.shipped_on && <><dt>Shipped</dt><dd>{s.shipped_on}</dd></>}
          {s.close_reason && <><dt>Closed because</dt><dd>{s.close_reason}</dd></>}
          <dt>Last change</dt><dd>{s.updated_by}, {new Date(s.updated_at).toLocaleString()}</dd>
        </dl>)}
      {open ? <ActionForm d={d} f={f} order={order} action={open} onDone={() => setOpen(null)} /> : actions.length > 0 && (
        <div className="form-actions">{actions.map((a) => (
          <button key={a} type="button" className={`btn${a === "reopen" || a === "close" ? "" : " primary"}`} onClick={() => setOpen(a)}>{ACTION_TEXT[a]}</button>))}
        </div>)}
    </section>
  );
}
