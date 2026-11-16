export const METHOD_LABEL: Record<string, string> = {
  FIFO: "FIFO", EDD: "EDD", SPT: "SPT", EDD_CREW: "EDD-Crew", SAVABLE_FIRST_CREW: "Savable-First-Crew",
  CRAFT_TW: "CP-SAT plan", CRAFT_V3: "CRAFT",
};
export const METHOD_ORDER = ["FIFO", "EDD", "SPT", "EDD_CREW", "SAVABLE_FIRST_CREW", "CRAFT_TW", "CRAFT_V3"];
export const methodLabel = (policy: string | null | undefined) => (policy ? METHOD_LABEL[policy] ?? policy : "—");

export const MODE_LABEL = { FULL: "Full (best plan)", QUICK: "Quick (draft plan)" } as const;

export function num(value: number | null | undefined, digits = 1): string {
  return value === null || value === undefined || Number.isNaN(value) ? "—" : value.toFixed(digits);
}

export function signed(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined) return "—";
  if (Math.abs(value) < 10 ** -digits / 2) return "±0";
  return (value > 0 ? "+" : "−") + Math.abs(value).toFixed(digits);
}

export function when(value: string | null | undefined): string {
  if (!value) return "—";
  const date = new Date(value);
  return date.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function duration(from: string | null, to: string | null): string {
  if (!from) return "—";
  const seconds = Math.max(0, ((to ? new Date(to) : new Date()).getTime() - new Date(from).getTime()) / 1000);
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60);
  return h ? `${h} h ${m} min` : `${m} min`;
}

/** Calendar date of a plan offset (days from the reference time). */
export function planDate(reference: string, day: number): string {
  const date = new Date(new Date(reference).getTime() + day * 86_400_000);
  return date.toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function setLabel(offset: number, size: number): string {
  return `Orders ${offset + 1}–${offset + size}`;
}

/** The worker's stage name in client words: the rule stages are setup for CRAFT. */
const STAGE_TEXT: Record<string, string> = {
  "FIFO rule": "Preparing the plan", "EDD rule": "Preparing the plan", "SPT rule": "Preparing the plan",
  "EDD with crews": "Preparing the plan", "Savable-first with crews": "Preparing the plan",
  "CP-SAT time windows": "Planning a day at a time", "CP-SAT improvement": "Improving the plan",
};
export const stageText = (stage: string) => STAGE_TEXT[stage] ?? stage;

/** History notes are written by the backend; show them in the product's own words. */
export const eventText = (message: string) => message.replace(/CRAFT V3/g, "CRAFT").replace("passed the physical validation", "passed the shift, machine and crew checks");

/** A line's number as people say it: "LINE01" becomes "Line 1". */
export const prettyLine = (line: unknown) => (line == null || line === "" ? "No line" : String(line).replace(/^LINE0*/i, "Line "));

const UNIT: Record<string, string> = { meters: "m", meter: "m", kgs: "kg", kg: "kg", pieces: "pcs", piece: "pcs" };

/** A stock quantity people can read: 1545186.3 Kgs -> "1.55 M kg", 85880.6 Meters -> "85,881 m", 2.5 -> "2.5". */
export function qty(value: number | string | null | undefined, unit: unknown = ""): string {
  const n = Number(value);
  if (value === null || value === undefined || value === "" || Number.isNaN(n)) return "—";
  const raw = String(unit ?? "").trim();
  const u = UNIT[raw.toLowerCase()] ?? raw;
  const abs = Math.abs(n);
  const text = abs >= 1e6 ? `${(n / 1e6).toLocaleString("en-US", { maximumFractionDigits: 2 })} M`
    : abs >= 100 ? Math.round(n).toLocaleString("en-US")
    : n.toLocaleString("en-US", { maximumFractionDigits: 1 });
  return u ? `${text} ${u}` : text;
}

/** Priority orders first (in the list's own order), then the rest unchanged. Returns a new array. */
export function priorityFirst<T extends { order_id: string }>(rows: T[], priority: string[]): T[] {
  const on = new Set(priority);
  return [...rows.filter((r) => on.has(r.order_id)), ...rows.filter((r) => !on.has(r.order_id))];
}
