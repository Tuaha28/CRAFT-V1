import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink } from "react-router-dom";
import { AlertCircle, Info } from "lucide-react";
import type { RunStatus } from "../lib/api";

const STATUS_TEXT: Record<string, string> = {
  QUEUED: "Waiting", RUNNING: "Running", SUCCEEDED: "Finished", FAILED: "Failed", CANCELLED: "Cancelled",
  PENDING: "Awaiting decision", APPROVED: "Approved", REJECTED: "Rejected", SUPERSEDED: "Replaced",
};

export function Status({ value }: { value: RunStatus | "PENDING" | "APPROVED" | "REJECTED" | "SUPERSEDED" }) {
  return <span className={`status ${value}`}>{STATUS_TEXT[value] ?? value}</span>;
}

export function Notice({ children, tone }: { children: ReactNode; tone?: "error" }) {
  return (
    <div className={`notice${tone ? ` ${tone}` : ""}`} role={tone === "error" ? "alert" : "status"}>
      {tone === "error" ? <AlertCircle size={17} aria-hidden="true" /> : <Info size={17} aria-hidden="true" />}
      <div className="notice-body">{children}</div>
    </div>
  );
}

export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div className="skeleton" role="status" aria-label={label}>
      <span /><span /><span /><span />
      <p className="muted small">{label}…</p>
    </div>
  );
}

export function Progress({ value, label, live }: { value: number; label: string; live?: boolean }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return (
    <div className={`progress${live ? " live" : ""}`} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label={label}>
      <span style={{ width: `${Math.max(pct, live ? 3 : 0)}%` }} />
    </div>
  );
}

const reducedMotion = () => typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;

/** Counts the leading number of a figure up from zero once, keeping its format ("10/10", "14.0%", "1,234"). */
function CountUp({ value }: { value: string | number }) {
  const text = String(value);
  const match = /^(-?)([\d,]*\.?\d+)(.*)$/.exec(text);
  const target = match ? Number(match[2].replace(/,/g, "")) : NaN;
  const [shown, setShown] = useState(() => (match && !reducedMotion() ? 0 : target));
  const started = useRef(false);
  useEffect(() => {
    if (!match || Number.isNaN(target) || reducedMotion()) { setShown(target); return; }
    if (started.current) { setShown(target); return; }
    started.current = true;
    const t0 = performance.now(), duration = 650;
    let frame = 0;
    const tick = (now: number) => {
      const p = Math.min(1, (now - t0) / duration);
      setShown(target * (1 - Math.pow(1 - p, 3)));
      if (p < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [target]); // eslint-disable-line react-hooks/exhaustive-deps
  if (!match || Number.isNaN(target)) return <>{text}</>;
  const decimals = match[2].includes(".") ? match[2].split(".")[1].length : 0;
  const grouped = match[2].includes(",");
  const number = grouped ? shown.toLocaleString(undefined, { maximumFractionDigits: decimals, minimumFractionDigits: decimals })
    : shown.toFixed(decimals);
  return <>{match[1]}{number}{match[3]}</>;
}

export function Measure({ value, label, tone }: { value: ReactNode; label: ReactNode; tone?: "late" | "ontime" }) {
  return (
    <div className="measure">
      <span className={`figure${tone ? ` ${tone}-text` : ""}`}>
        {typeof value === "string" || typeof value === "number" ? <CountUp value={value} /> : value}
      </span>
      <small>{label}</small>
    </div>
  );
}

/** A button for an action that cannot be undone: the first press asks, the second acts. */
export function ConfirmButton({ label, confirm, onConfirm, disabled, className = "btn danger" }:
  { label: string; confirm: string; onConfirm: () => void; disabled?: boolean; className?: string }) {
  const [asking, setAsking] = useState(false);
  if (!asking) return <button type="button" className={className} disabled={disabled} onClick={() => setAsking(true)}>{label}</button>;
  return (
    <span className="confirm" role="group" aria-label={confirm}>
      <span className="small">{confirm}</span>
      <button type="button" className={className} disabled={disabled} onClick={() => { setAsking(false); onConfirm(); }} autoFocus>Yes, {label.toLowerCase()}</button>
      <button type="button" className="btn" onClick={() => setAsking(false)}>Keep it</button>
    </span>
  );
}

export const RESOURCE_TABS = [["/workforce", "Workers"], ["/machines", "Machines"], ["/materials", "Materials"]] as const;
export const QUALITY_TABS = [["/floor/quality", "Checks on the floor"], ["/quality", "History"]] as const;

/** Links between pages that share one menu entry, shown as tabs under the page title. */
export function PageTabs({ items, label }: { items: readonly (readonly [string, string])[]; label: string }) {
  return (
    <nav className="tabs page-tabs" aria-label={label}>
      {items.map(([to, text]) => <NavLink key={to} to={to} className={({ isActive }) => (isActive ? "on" : undefined)}>{text}</NavLink>)}
    </nav>
  );
}

export function PageHead({ title, lead, children }: { title: string; lead?: ReactNode; children?: ReactNode }) {
  return (
    <header className="page-head">
      <div><h1>{title}</h1>{lead && <p>{lead}</p>}</div>
      {children && <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>{children}</div>}
    </header>
  );
}
