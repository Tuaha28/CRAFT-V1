import type { ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import type { Row } from "../lib/api";

export interface Column { key: string; label: string; num?: boolean; render?: (row: Row) => ReactNode }

export function DataTable({ columns, rows, onRow, selected, empty = "Nothing to show." }:
  { columns: Column[]; rows: Row[]; onRow?: (row: Row) => void; selected?: (row: Row) => boolean; empty?: string }) {
  if (!rows.length) return <p className="muted" style={{ padding: "12px 0" }}>{empty}</p>;
  return (
    <div className="table-wrap">
      <table>
        <thead><tr>{columns.map((c) => <th key={c.key} className={c.num ? "num" : undefined}>{c.label}</th>)}</tr></thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} className={[onRow && "clickable", selected?.(row) && "picked"].filter(Boolean).join(" ") || undefined}
              aria-current={selected?.(row) || undefined} onClick={onRow ? () => onRow(row) : undefined}
              tabIndex={onRow ? 0 : undefined} onKeyDown={onRow ? (e) => { if (e.key === "Enter") onRow(row); } : undefined}>
              {columns.map((c) => (
                <td key={c.key} className={c.num ? "num" : undefined}>{c.render ? c.render(row) : (row[c.key] ?? "—")}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Page, search and filters kept in the URL so a view can be shared or reloaded. */
export function useListParams(defaults: Record<string, string> = {}) {
  const [params, setParams] = useSearchParams();
  const get = (key: string) => params.get(key) ?? defaults[key] ?? "";
  const set = (changes: Record<string, string>) => {
    const next = new URLSearchParams(params);
    Object.entries(changes).forEach(([k, v]) => (v ? next.set(k, v) : next.delete(k)));
    if (!("page" in changes)) next.delete("page");
    setParams(next, { replace: true });
  };
  return { get, set, page: Number(params.get("page") ?? 1) };
}

export function Pager({ page, size, total, onPage }: { page: number; size: number; total: number; onPage: (p: number) => void }) {
  const pages = Math.max(1, Math.ceil(total / size));
  return (
    <div className="pager">
      <span className="muted small">{total} total, page {page} of {pages}</span>
      <button className="btn" disabled={page <= 1} onClick={() => onPage(page - 1)}>Previous</button>
      <button className="btn" disabled={page >= pages} onClick={() => onPage(page + 1)}>Next</button>
    </div>
  );
}

export function Chip({ value }: { value: string | number | null | undefined }) {
  const text = String(value ?? "—");
  const tone = /breakdown|overdue|late|shortage|short by/i.test(text) ? "bad"
    : /priority|maintenance|idle|packing|in_production|processing|pending|awaiting/i.test(text) ? "warn"
    : /active|shipped|completed|in_transit|on time|covered/i.test(text) ? "good" : "";
  return <span className={`chip ${tone}`}>{text.replace(/_/g, " ")}</span>;
}

export function Meter({ value, max, bad }: { value: number; max: number; bad?: boolean }) {
  const pct = max > 0 ? Math.min(100, (100 * value) / max) : 0;
  return <span className="bar-cell"><span className="track"><span className={`fill${bad ? " bad" : ""}`} style={{ width: `${pct}%` }} /></span></span>;
}
