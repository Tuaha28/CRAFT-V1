import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Bell } from "lucide-react";
import { api, erp, floor } from "../lib/api";
import { when } from "../lib/format";
import { can, useSession } from "../app/session";
import { useReadyScope } from "../app/scope";

interface Alert { key: string; tone: "bad" | "warn" | "info"; text: string; to: string; at?: string }
const SEEN = "craft.alerts.seen";

function seenKeys(): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(SEEN) ?? "[]")); } catch { return new Set(); }
}

/** One place for what needs attention: plans to decide, finished or failed runs,
 *  and factory problems (overdue orders, machines down, material shortages). */
export function Alerts() {
  const session = useSession();
  const ready = useReadyScope();
  const [open, setOpen] = useState(false);
  const [seen, setSeen] = useState(seenKeys);
  const box = useRef<HTMLDivElement>(null);
  const runs = useQuery({ queryKey: ["runs"], queryFn: api.runs, refetchInterval: 15_000 });
  const summary = useQuery({ queryKey: ["erp-summary", ready], queryFn: () => erp.summary(ready![0], ready![1]), enabled: Boolean(ready) });
  const materials = useQuery({ queryKey: ["erp-materials", ready], queryFn: () => erp.materials(ready![0], ready![1]), enabled: Boolean(ready) });
  const plan = useQuery({ queryKey: ["current-plan", ready], queryFn: () => erp.currentPlan(ready![0], ready![1]), enabled: Boolean(ready) });
  const onFloor = can(session, "floor.view_floor");
  const issues = useQuery({ queryKey: ["floor-issues", ready, true], queryFn: () => floor.issues(ready![0], ready![1], true),
    enabled: Boolean(ready) && onFloor, refetchInterval: 30_000 });
  const board = useQuery({ queryKey: ["floor-board", ready, undefined], queryFn: () => floor.board(ready![0], ready![1]),
    enabled: Boolean(ready) && onFloor, refetchInterval: 30_000 });

  const alerts = useMemo<Alert[]>(() => {
    const list: Alert[] = [];
    const recent = Date.now() - 7 * 86_400_000;
    for (const r of runs.data ?? []) {
      const what = r.kind === "CAPTURE" ? "Database capture" : r.purpose === "PROMISE" ? "New-order check"
        : r.purpose === "DISRUPTION" ? "Disruption re-plan" : `Plan for factory ${r.factory_id}`;
      if (r.status === "SUCCEEDED" && r.kind === "SCHEDULE" && r.decision === "PENDING" && session.can_approve)
        list.push({ key: `decide-${r.id}`, tone: "warn", text: `${what} is waiting for your approval`, to: `/runs/${r.id}`, at: r.finished_at ?? undefined });
      else if (r.status === "SUCCEEDED" && r.finished_at && new Date(r.finished_at).getTime() > recent)
        list.push({ key: `done-${r.id}`, tone: "info", text: `${what} finished`, to: `/runs/${r.id}`, at: r.finished_at });
      if (r.status === "FAILED" && r.finished_at && new Date(r.finished_at).getTime() > recent)
        list.push({ key: `fail-${r.id}`, tone: "bad", text: `${what} failed: ${r.error.slice(0, 80)}`, to: `/runs/${r.id}`, at: r.finished_at });
    }
    const s = summary.data;
    if (s?.orders_overdue) list.push({ key: `overdue-${s.database}-${s.orders_overdue}`, tone: "bad", text: `${s.orders_overdue} order${s.orders_overdue > 1 ? "s are" : " is"} past the due date and not shipped`, to: "/orders?status=OVERDUE" });
    const down = s ? (s.machines_by_status.Breakdown ?? 0) + (s.machines_by_status.Under_Maintenance ?? 0) : 0;
    if (down) list.push({ key: `machines-${s!.database}-${down}`, tone: "warn", text: `${down} machine${down > 1 ? "s are" : " is"} broken down or under maintenance`, to: "/machines" });
    const short = (materials.data?.materials ?? []).filter((m) => Number(m.shortage) > 0);
    if (short.length) list.push({ key: `materials-${short.map((m) => m.material_id).join(".")}`, tone: "warn", text: `${short.length} material${short.length > 1 ? "s are" : " is"} short for open orders (${short.map((m) => m.name).join(", ")})`, to: "/materials" });
    const late = plan.data?.run?.summary.craft_v3?.late;
    if (plan.data?.run && late) list.push({ key: `plan-late-${plan.data.run.id}`, tone: "warn", text: `The current plan has ${late} late orders`, to: `/runs/${plan.data.run.id}` });
    for (const i of issues.data?.issues ?? [])
      list.push({ key: `issue-${i.id}-${i.status}`, tone: i.status === "OPEN" ? "bad" : "warn", to: "/floor/issues", at: i.created_at,
        text: `${i.kind_label}${i.machine_id ? `: machine ${i.machine_id}` : i.worker_id ? `: worker ${i.worker_id}` : i.order_id ? `: order ${i.order_id}` : ""} (${i.status_label.toLowerCase()})` });
    const b = board.data;
    if (b?.plan && b.day === b.today)
      for (const l of b.lines)
        if (l.state === "BEHIND")
          list.push({ key: `behind-${l.line_id}-${b.day}-${l.hours_passed}`, tone: "bad", to: `/floor/line?line=${l.line_id}`,
            text: `Line ${String(l.line_number ?? l.line_id).replace(/^LINE0*/i, "")} is behind plan: ${l.actual_so_far} of ${l.target_so_far} pieces so far` });
    return list;
  }, [runs.data, summary.data, materials.data, plan.data, session.can_approve, issues.data, board.data]);

  const unread = alerts.filter((a) => !seen.has(a.key)).length;
  const markRead = () => {
    const next = new Set([...seen, ...alerts.map((a) => a.key)]);
    setSeen(next);
    try { localStorage.setItem(SEEN, JSON.stringify([...next].slice(-300))); } catch { /* storage may be blocked */ }
  };
  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", close); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", esc); };
  }, [open]);

  return (
    <div className="alerts-box" ref={box}>
      <button type="button" className="bell" aria-expanded={open} aria-label={`Alerts, ${unread} new`} onClick={() => setOpen(!open)}>
        <Bell size={17} aria-hidden="true" />{unread > 0 && <span className="badge">{unread}</span>}
      </button>
      {open && (
        <div className="alerts-panel" role="dialog" aria-label="Alerts">
          <div className="section-head"><h3>Alerts</h3>{alerts.length > 0 && <button type="button" className="link-btn" onClick={markRead}>Mark all as read</button>}</div>
          {!alerts.length ? <p className="muted small">Nothing needs attention.</p> : (
            <ul>{alerts.map((a) => (
              <li key={a.key} className={`${a.tone}${seen.has(a.key) ? " read" : ""}`}>
                <Link to={a.to} onClick={() => setOpen(false)}>{a.text}</Link>
                {a.at && <time>{when(a.at)}</time>}
              </li>))}</ul>)}
        </div>)}
    </div>
  );
}
