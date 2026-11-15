import type { ReactNode } from "react";
import { Link } from "react-router-dom";

/** A count with a bar showing its share of the whole. A link, a toggle button (onClick + pressed) or plain. */
export function RingCard({ label, value, note, share, tone, to, onClick, pressed }:
  { label: string; value: ReactNode; note: string; share: number; tone?: "late" | "ok"; to?: string; onClick?: () => void; pressed?: boolean }) {
  const body = (
    <>
      <small className="rc-label">{label}</small>
      <span className={`figure${tone === "late" ? " late-text" : tone === "ok" ? " ontime-text" : ""}`}>{value}</span>
      <span className="stat-meter" aria-hidden="true"><i style={{ width: `${Math.round(Math.max(0, Math.min(1, share)) * 100)}%` }} /></span>
      <small className="rc-note">{note}</small>
    </>
  );
  const cls = `panel ring-card${tone ? ` ${tone}` : ""}`;
  if (to) return <Link className={cls} to={to}>{body}</Link>;
  if (onClick) return <button type="button" className={`${cls}${pressed ? " on" : ""}`} aria-pressed={pressed} onClick={onClick}>{body}</button>;
  return <div className={cls}>{body}</div>;
}
