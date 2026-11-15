import type { OrderRow } from "./api";

/** An order counts as at risk when it finishes on time but with less than this many days to spare. */
export const RISK_DAYS = 1;

export type Standing = "late" | "risk" | "ontime" | "open";

export function standing(row: OrderRow): { kind: Standing; finish: number | null; spare: number | null } {
  const m = row.methods.CRAFT_V3;
  if (!m || !m.complete || m.finish_day == null) return { kind: "open", finish: m?.finish_day ?? null, spare: null };
  if (row.due_day == null) return { kind: "ontime", finish: m.finish_day, spare: null };
  const spare = row.due_day - m.finish_day;
  return { kind: spare < -1e-6 ? "late" : spare < RISK_DAYS ? "risk" : "ontime", finish: m.finish_day, spare };
}
