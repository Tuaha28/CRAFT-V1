import type { GanttRow } from "./api";

/**
 * Manual "what if I move this" sketches on a finished plan. Moves only shift an
 * operation in time (same crew, same machines, same length). The checks compare
 * the sketch with the plan it came from, so anything the plan itself already
 * did is never reported:
 * - a worker or machine now doing two operations at once,
 * - two steps of one order now overlapping where the plan ran them one after the other,
 * - an operation pulled before the plan start.
 * Shift hours, setup and changeover are not checked here; CRAFT checks them when it re-plans.
 */
export type Offsets = Record<string, number>;

export interface Clash {
  kind: "worker" | "machine" | "sequence" | "start";
  key: string;
  other?: string;
  resource?: string;
}

export interface OrderEffect {
  order_id: string; before: number; after: number; due: number | null;
  lateBefore: boolean; lateAfter: boolean;
}

export const HOUR = 3600;
const DAY = 86_400;

export function applyOffsets(rows: GanttRow[], offsets: Offsets): GanttRow[] {
  return rows.map((r) => (offsets[r.key] ? { ...r, start: r.start + offsets[r.key], end: r.end + offsets[r.key] } : r));
}

const overlaps = (a: GanttRow, b: GanttRow) => a.start < b.end && b.start < a.end;

export function clashes(base: GanttRow[], offsets: Offsets): Clash[] {
  const moved = applyOffsets(base, offsets);
  const before = new Map(base.map((r) => [r.key, r]));
  const out: Clash[] = [];
  const seen = new Set<string>();
  for (const a of moved) {
    if (!offsets[a.key]) continue;
    if (a.start < 0) out.push({ kind: "start", key: a.key });
    for (const b of moved) {
      if (b.key === a.key) continue;
      const pair = [a.key, b.key].sort().join("|");
      const a0 = before.get(a.key)!, b0 = before.get(b.key)!;
      for (const [kind, list] of [["worker", a.workers], ["machine", a.machines]] as const) {
        const other = kind === "worker" ? b.workers : b.machines;
        for (const id of list) {
          if (!other.includes(id) || !overlaps(a, b) || overlaps(a0, b0)) continue;
          const tag = `${kind}:${id}:${pair}`;
          if (!seen.has(tag)) { seen.add(tag); out.push({ kind, key: a.key, other: b.key, resource: id }); }
        }
      }
      if (a.order_id === b.order_id) {
        const first = a0.end <= b0.start ? [a, b] : b0.end <= a0.start ? [b, a] : null;
        if (first && first[0].end > first[1].start) {
          const tag = `sequence:${pair}`;
          if (!seen.has(tag)) { seen.add(tag); out.push({ kind: "sequence", key: first[0].key, other: first[1].key }); }
        }
      }
    }
  }
  return out;
}

/** Finish of every order touched by a move, before and after, against its due day. */
export function orderEffects(base: GanttRow[], offsets: Offsets, dueDay: Record<string, number | null>): OrderEffect[] {
  const moved = applyOffsets(base, offsets);
  const touched = new Set(base.filter((r) => offsets[r.key]).map((r) => r.order_id));
  const finish = (rows: GanttRow[], order: string) => Math.max(...rows.filter((r) => r.order_id === order).map((r) => r.end));
  return [...touched].sort((a, b) => Number(a) - Number(b)).map((order) => {
    const before = finish(base, order) / DAY, after = finish(moved, order) / DAY, due = dueDay[order] ?? null;
    return { order_id: order, before, after, due,
      lateBefore: due != null && before > due + 1e-6, lateAfter: due != null && after > due + 1e-6 };
  });
}
