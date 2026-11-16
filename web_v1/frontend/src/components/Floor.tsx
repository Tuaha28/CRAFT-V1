import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { erp, floor, type FloorOp, type HourPoint, type LineRef, type LineState } from "../lib/api";
import { Notice } from "./ui";
import { useReadyScope } from "../app/scope";

export const STATE_TEXT: Record<LineState, string> = {
  ON_TRACK: "On track", BEHIND: "Behind plan", NO_DATA: "No output entered", NOT_STARTED: "Not started yet", IDLE: "No work planned",
};
const STATE_TONE: Record<LineState, string> = { ON_TRACK: "good", BEHIND: "bad", NO_DATA: "warn", NOT_STARTED: "", IDLE: "" };

export function StateChip({ state }: { state: LineState }) {
  return <span className={`chip ${STATE_TONE[state]}`}>{STATE_TEXT[state]}</span>;
}

/** "Line 3"; the factory data reuses some line numbers, so a shared number also shows the line id. */
export const lineName = (lines: LineRef[] | undefined, id: number | null | undefined) => {
  if (id == null) return "No line";
  const found = lines?.find((l) => l.line_id === id);
  if (!found) return `Line ${id}`;
  const shared = (lines ?? []).filter((l) => l.line_number === found.line_number).length > 1;
  return `Line ${found.line_number.replace(/^LINE0*/i, "")}${shared ? ` (#${id})` : ""}`;
};

/** Floor queries keyed by capture and factory. */
export function useFloorLines(d: number | undefined, f: number | undefined) {
  return useQuery({ queryKey: ["floor-lines", d, f], queryFn: () => floor.lines(d!, f!), enabled: d !== undefined && f !== undefined });
}

/** A line's name, for places that only know its id. */
export function LineLabel({ id }: { id: number }) {
  const ready = useReadyScope();
  const lines = useFloorLines(ready?.[0], ready?.[1]);
  return <>{lineName(lines.data?.lines, id)}</>;
}

export function LinePicker({ lines, value, onChange, locked }: { lines: LineRef[]; value: number | null; onChange: (id: number) => void; locked?: boolean }) {
  return (
    <div className="field"><label htmlFor="line-pick">Line</label>
      <select id="line-pick" value={value ?? ""} disabled={locked} onChange={(e) => onChange(Number(e.target.value))}>
        {lines.map((l) => <option key={l.line_id} value={l.line_id}>{lineName(lines, l.line_id)}</option>)}
      </select></div>
  );
}

/** Previous / next working day of the plan, with a jump to today when the plan covers it. */
export function DayNav({ days, day, today, date, onChange }: { days: number[]; day: number; today: number; date: string; onChange: (d: number) => void }) {
  const i = days.indexOf(day);
  const prev = i > 0 ? days[i - 1] : days.filter((d) => d < day).pop();
  const next = i >= 0 ? days[i + 1] : days.find((d) => d > day);
  const label = new Date(`${date}T00:00:00`).toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long", year: "numeric" });
  return (
    <div className="day-nav" role="group" aria-label="Plan day">
      <button type="button" className="btn" disabled={prev === undefined} onClick={() => onChange(prev!)}>Previous day</button>
      <strong>{label}{day === today ? " (today)" : ""}</strong>
      <button type="button" className="btn" disabled={next === undefined} onClick={() => onChange(next!)}>Next day</button>
      {days.includes(today) && day !== today && <button type="button" className="link-btn" onClick={() => onChange(today)}>Go to today</button>}
    </div>
  );
}

/** Planned (outline) and recorded (filled) pieces per hour. */
export function HourBars({ hours, passed, height = 120 }: { hours: HourPoint[]; passed: number; height?: number }) {
  if (!hours.length) return <p className="muted small">No work planned on this day.</p>;
  const max = Math.max(1, ...hours.map((h) => Math.max(h.target, h.actual)));
  const w = 34, gap = 8, top = 16, base = height - 18;
  const scale = (v: number) => (v / max) * (base - top);
  return (
    <div className="hour-bars">
      <svg width={hours.length * (w + gap)} height={height} role="img"
        aria-label={`Planned and recorded pieces per hour: ${hours.map((h) => `${h.hour}:00 ${Math.round(h.target)} planned, ${h.actual} recorded`).join("; ")}`}>
        {hours.map((h, i) => {
          const x = i * (w + gap);
          const behind = h.hour < passed && h.target > 0 && h.actual < 0.85 * h.target;
          return (
            <g key={h.hour}>
              <rect className="target" x={x} y={base - scale(h.target)} width={w} height={scale(h.target)} rx={2} />
              <rect className={`actual${behind ? " behind" : ""}`} x={x + 6} y={base - scale(h.actual)} width={w - 12} height={scale(h.actual)} rx={2}
                style={{ animationDelay: `${i * 40}ms` }} />
              {h.actual > 0 && <text x={x + w / 2} y={base - scale(h.actual) - 4} textAnchor="middle">{h.actual}</text>}
              <text className="hour" x={x + w / 2} y={height - 4} textAnchor="middle">{String(h.hour).padStart(2, "0")}</text>
            </g>);
        })}
      </svg>
      <div className="legend"><span><i className="leg-target" />Planned</span><span><i className="leg-actual" />Recorded</span></div>
    </div>
  );
}

const KINDS = [["MACHINE_DOWN", "Machine down"], ["WORKER_ABSENT", "Worker absent"], ["LINE_STOPPED", "Line stopped"], ["MATERIAL_SHORT", "Material short"],
  ["QUALITY", "Quality problem"], ["OTHER", "Other"]] as const;

/** Report a problem on the floor. Machine and worker lists come from the operations planned on the line. */
export function ReportProblem({ d, f, lineId, ops, onDone }: { d: number; f: number; lineId: number | null; ops: FloorOp[]; onDone?: () => void }) {
  const client = useQueryClient();
  const [kind, setKind] = useState<string>("MACHINE_DOWN");
  const [machine, setMachine] = useState(""), [worker, setWorker] = useState(""), [order, setOrder] = useState(""), [note, setNote] = useState("");
  const [material, setMaterial] = useState(""), [arrival, setArrival] = useState("");
  const materials = useQuery({ queryKey: ["erp-materials", d, f], queryFn: () => erp.materials(d, f), enabled: kind === "MATERIAL_SHORT" });
  const machines = [...new Map(ops.flatMap((o) => o.machines.map((m, i) => [m, o.machine_models[i] ?? ""] as const))).entries()];
  const workers = [...new Map(ops.flatMap((o) => o.workers.map((w, i) => [w, o.worker_names[i] ?? ""] as const))).entries()];
  const orders = [...new Set(ops.map((o) => o.order_id))];
  const send = useMutation({
    meta: { success: "Problem reported." },
    mutationFn: () => floor.report(d, f, {
      kind, line_id: lineId, note,
      machine_id: kind === "MACHINE_DOWN" ? Number(machine) || null : null,
      worker_id: kind === "WORKER_ABSENT" ? Number(worker) || null : null,
      order_id: ["MATERIAL_SHORT", "QUALITY", "OTHER"].includes(kind) ? order : "",
      material_id: kind === "MATERIAL_SHORT" ? Number(material) || null : null,
      arrival_date: kind === "MATERIAL_SHORT" ? arrival || null : null,
    }),
    onSuccess: async () => {
      setMachine(""); setWorker(""); setOrder(""); setNote(""); setMaterial(""); setArrival("");
      await client.invalidateQueries({ queryKey: ["floor-issues"] });
      await client.invalidateQueries({ queryKey: ["floor-line"] });
      await client.invalidateQueries({ queryKey: ["floor-board"] });
      onDone?.();
    },
  });
  const submit = (e: FormEvent) => { e.preventDefault(); send.mutate(); };
  return (
    <form className="form-grid" onSubmit={submit}>
      <div className="field"><label htmlFor="rp-kind">Problem</label>
        <select id="rp-kind" value={kind} onChange={(e) => setKind(e.target.value)}>
          {KINDS.map(([k, label]) => <option key={k} value={k}>{label}</option>)}
        </select></div>
      {kind === "MACHINE_DOWN" && (
        <div className="field"><label htmlFor="rp-machine">Machine</label>
          <input id="rp-machine" list="rp-machines" inputMode="numeric" value={machine} onChange={(e) => setMachine(e.target.value.replace(/\D/g, ""))} placeholder="Machine number" required />
          <datalist id="rp-machines">{machines.map(([id, model]) => <option key={id} value={id}>{model}</option>)}</datalist>
          <span className="hint">Machines planned on this line are suggested; any machine of the factory can be entered.</span></div>)}
      {kind === "WORKER_ABSENT" && (
        <div className="field"><label htmlFor="rp-worker">Worker</label>
          <input id="rp-worker" list="rp-workers" inputMode="numeric" value={worker} onChange={(e) => setWorker(e.target.value.replace(/\D/g, ""))} placeholder="Employee number" required />
          <datalist id="rp-workers">{workers.map(([id, name]) => <option key={id} value={id}>{name}</option>)}</datalist></div>)}
      {kind === "LINE_STOPPED" && (
        <p className="field hint">{lineId ? `Reports this line (${lineId}) as stopped. CRAFT can re-plan without it.` : "Open this from a line's page to report that line as stopped."}</p>)}
      {kind === "MATERIAL_SHORT" && (<>
        <div className="field"><label htmlFor="rp-material">Material</label>
          <select id="rp-material" value={material} onChange={(e) => setMaterial(e.target.value)} required>
            <option value="">{materials.isPending ? "Loading materials…" : "Choose the material"}</option>
            {(materials.data?.materials ?? []).map((m) => <option key={String(m.material_id)} value={String(m.material_id)}>{String(m.name)}</option>)}
          </select></div>
        <div className="field"><label htmlFor="rp-arrival">Expected to arrive</label>
          <input id="rp-arrival" type="date" value={arrival} onChange={(e) => setArrival(e.target.value)} required /></div>
      </>)}
      {kind !== "MACHINE_DOWN" && kind !== "WORKER_ABSENT" && kind !== "LINE_STOPPED" && (
        <div className="field"><label htmlFor="rp-order">Order</label>
          <input id="rp-order" list="rp-orders" value={order} onChange={(e) => setOrder(e.target.value.replace(/\D/g, ""))} placeholder={kind === "MATERIAL_SHORT" ? "Order number (optional)" : "Order number"} />
          <datalist id="rp-orders">{orders.map((o) => <option key={o} value={o} />)}</datalist></div>)}
      <div className="field wide"><label htmlFor="rp-note">Details</label>
        <textarea id="rp-note" rows={2} value={note} onChange={(e) => setNote(e.target.value)} placeholder="What happened, and since when" /></div>
      <div className="form-actions">
        <button className="btn primary" disabled={send.isPending || (kind === "LINE_STOPPED" && !lineId)}>{send.isPending ? "Reporting…" : "Report problem"}</button>
        {send.isSuccess && <span className="ontime-text small" role="status">Reported. The planner and the production manager can see it now.</span>}
        {send.data?.warning && <span className="warn-text small" role="status">{send.data.warning}</span>}
      </div>
      {send.isError && <Notice tone="error">{send.error.message}</Notice>}
    </form>
  );
}

/** Record a quality check (pieces checked, defects found) for an operation planned on a line and day. */
export function CheckForm({ d, f, lineId, day, ops, types, onDone }: { d: number; f: number; lineId: number; day: number; ops: FloorOp[]; types: string[]; onDone?: () => void }) {
  const client = useQueryClient();
  const [op, setOp] = useState(ops[0]?.key ?? "");
  const [checked, setChecked] = useState(""), [defects, setDefects] = useState("0"), [type, setType] = useState(""), [note, setNote] = useState("");
  const send = useMutation({
    meta: { success: "Quality check saved." },
    mutationFn: () => floor.addCheck(d, f, { line_id: lineId, day, operation_key: op, checked: Number(checked), defects: Number(defects), defect_type: type, note }),
    onSuccess: async () => {
      setChecked(""); setDefects("0"); setType(""); setNote("");
      await client.invalidateQueries({ queryKey: ["floor-checks"] });
      await client.invalidateQueries({ queryKey: ["floor-line"] });
      await client.invalidateQueries({ queryKey: ["floor-board"] });
      onDone?.();
    },
  });
  if (!ops.length) return <p className="muted small">No operation is planned on this line on this day.</p>;
  const dhu = Number(checked) > 0 ? (100 * Number(defects)) / Number(checked) : null;
  return (
    <form className="form-grid" onSubmit={(e) => { e.preventDefault(); send.mutate(); }}>
      <div className="field wide"><label htmlFor="qc-op">Operation</label>
        <select id="qc-op" value={op} onChange={(e) => setOp(e.target.value)}>
          {ops.map((o) => <option key={o.key} value={o.key}>Order {o.order_id}, step {o.step}: {o.name ?? o.machine_type}</option>)}
        </select></div>
      <div className="field"><label htmlFor="qc-checked">Pieces checked</label>
        <input id="qc-checked" type="number" min={1} value={checked} onChange={(e) => setChecked(e.target.value)} required /></div>
      <div className="field"><label htmlFor="qc-defects">Defects found</label>
        <input id="qc-defects" type="number" min={0} value={defects} onChange={(e) => setDefects(e.target.value)} required />
        {dhu !== null && <span className="hint">{dhu.toFixed(1)} defects per 100 pieces</span>}</div>
      <div className="field"><label htmlFor="qc-type">Main defect</label>
        <select id="qc-type" value={type} onChange={(e) => setType(e.target.value)} required={Number(defects) > 0}>
          <option value="">{Number(defects) > 0 ? "Choose" : "None"}</option>
          {types.map((t) => <option key={t} value={t}>{t}</option>)}
        </select></div>
      <div className="field wide"><label htmlFor="qc-note">Note</label>
        <input id="qc-note" value={note} onChange={(e) => setNote(e.target.value)} /></div>
      <div className="form-actions">
        <button className="btn primary" disabled={send.isPending || !op}>{send.isPending ? "Saving…" : "Save check"}</button>
        {send.isSuccess && <span className="ontime-text small" role="status">Check saved.</span>}
      </div>
      {send.isError && <Notice tone="error">{send.error.message}</Notice>}
    </form>
  );
}
