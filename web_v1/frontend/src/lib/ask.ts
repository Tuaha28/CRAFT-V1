import type { AskRoute, Digest, ErpSummary, GanttRow, OrderRow, Row } from "./api";
import { num, planDate, prettyLine } from "./format";
import { standing } from "./standing";

/*
 * Ask CRAFT: answers questions about the current plan and the factory database.
 * Every figure is worked out here from data the app already loaded. Nothing is
 * guessed and nothing is sent anywhere; an unknown question gets a list of what
 * can be asked instead of an invented answer.
 */

const DAY = 86_400;

export interface Fact { label: string; value: string; tone?: "late" | "ok" | "warn" }
export interface Item { text: string; sub?: string; tone?: "late" | "ok" | "warn"; order?: string }
export interface Answer {
  topic: Topic;
  title: string;
  text?: string;
  facts?: Fact[];
  items?: Item[];
  link?: { label: string; to: string };
  /** The data this answer needs but does not have yet (still loading, or not allowed). */
  missing?: Source;
}

export type Source = "plan" | "summary" | "materials" | "quality" | "machines";
export type Topic = "order" | "crew" | "late" | "risk" | "line" | "busiest" | "built" | "plan" | "stock" | "sales"
  | "quality" | "machines" | "people" | "help";

export interface Materials { materials: Row[] }
export interface QualityData { totals: Row; by_line: Row[]; by_type: Row[] }

export interface AskContext {
  digest?: Digest | null;
  runId?: string;
  lineLabel: (line: number) => string;
  summary?: ErpSummary;
  materials?: Materials;
  quality?: QualityData;
  machines?: Row[];
}

export const SUGGESTIONS = [
  "Which orders are late?",
  "Why is the worst order late?",
  "Which line is busiest?",
  "Is any material short?",
  "How many orders have we shipped?",
  "How is quality?",
];

const n = (v: unknown) => (v == null || v === "" ? 0 : Number(v));
const plural = (count: number, one: string, many = `${one}s`) => `${count} ${count === 1 ? one : many}`;
const has = (q: string, ...words: string[]) => words.some((w) => new RegExp(`\\b${w}`, "i").test(q));

/** Which topic a question is about, and the order or line it names. */
export function understand(question: string, ctx: Pick<AskContext, "digest" | "lineLabel">) {
  const q = question.toLowerCase();
  const rows = ctx.digest?.order_rows ?? [];
  const lineMatch = q.match(/\bline\s*0*(\d+)/);
  const rest = q.replace(/\bline\s*\d+/g, "").replace(/\bday\s*\d+/g, "");  // numbers that are not order ids
  const order = rows.find((r) => new RegExp(`(^|[^\\d])${r.order_id}([^\\d]|$)`).test(rest))
    ?? (has(q, "worst", "latest") && has(q, "order") ? worst(rows) : undefined);
  let topic: Topic;
  if (order) topic = has(q, "who", "worker", "machine", "crew", "people") ? "crew" : "order";
  else if (lineMatch) topic = "line";
  else if (has(q, "busiest", "busy", "load", "overload")) topic = "busiest";
  else if (has(q, "stock", "inventor", "material", "fabric", "thread", "button", "zip", "short", "warehouse", "supplier")) topic = "stock";
  else if (has(q, "sell", "sold", "sale", "ship", "deliver", "dispatch", "revenue", "client", "customer")) topic = "sales";
  else if (has(q, "quality", "defect", "dhu", "reject", "inspect")) topic = "quality";
  else if (has(q, "machine", "breakdown", "broken", "maintenance")) topic = "machines";
  else if (has(q, "people", "worker", "staff", "leave", "present", "absent", "operator")) topic = "people";
  else if (has(q, "risk", "tight", "spare")) topic = "risk";
  else if (has(q, "late", "overdue", "delay", "behind")) topic = "late";
  else if (has(q, "how", "built", "made", "work") && has(q, "craft", "plan") && has(q, "built", "made", "work", "decide")) topic = "built";
  else if (has(q, "plan", "summary", "overview", "status", "finish")) topic = "plan";
  else topic = "help";
  return { topic, order, line: lineMatch ? lineOf(Number(lineMatch[1]), ctx) : undefined, day: dayOf(q), unknownOrder: undefined as string | undefined };
}

function worst(rows: OrderRow[]) {
  let best: OrderRow | undefined, most = 0;
  for (const r of rows) {
    const s = standing(r);
    const late = s.kind === "open" ? Infinity : s.kind === "late" ? -(s.spare ?? 0) : 0;
    if (late > most) { most = late; best = r; }
  }
  return best;
}

/** The plan's line id for "line 3": by its shown name first, then by id. */
function lineOf(wanted: number, ctx: Pick<AskContext, "digest" | "lineLabel">) {
  const ids = [...new Set((ctx.digest?.gantt.CRAFT_V3 ?? []).map((g) => g.line))];
  return ids.find((id) => Number(ctx.lineLabel(id).match(/(\d+)\s*$/)?.[1]) === wanted) ?? ids.find((id) => id === wanted) ?? null;
}

/** Plan day (0 = the plan's first day): "today", "tomorrow", "day 3". */
function dayOf(q: string): number | undefined {
  if (/\btoday\b/.test(q)) return 0;
  if (/\btomorrow\b/.test(q)) return 1;
  const d = q.match(/\bday\s*(\d+)/);
  return d ? Math.max(0, Number(d[1]) - 1) : undefined;
}

const TOPICS: Topic[] = ["order", "crew", "late", "risk", "line", "busiest", "built", "plan", "stock", "sales", "quality", "machines", "people", "help"];

/**
 * The AI model's reading of a question (topic plus the order, line, day or
 * material it names), turned into the same form as understand(). An order the
 * plan does not hold, or a topic that needs one but names none, falls back to
 * the worst order, as the built-in reading does.
 */
export function fromRoute(route: AskRoute, ctx: Pick<AskContext, "digest" | "lineLabel">): ReturnType<typeof understand> {
  const rows = ctx.digest?.order_rows ?? [];
  let topic: Topic = TOPICS.includes(route.topic as Topic) ? (route.topic as Topic) : "help";
  let order = route.order_id ? rows.find((r) => r.order_id === route.order_id) : undefined;
  if ((topic === "order" || topic === "crew") && !order) {
    if (route.order_id) return { topic: "help", order: undefined, line: undefined, day: undefined, unknownOrder: route.order_id };
    order = worst(rows);
    if (!order) topic = "late";
  }
  return { topic, order, line: route.line != null ? lineOf(route.line, ctx) : undefined, day: route.day != null ? Math.max(0, route.day - 1) : undefined, unknownOrder: undefined };
}

export function ask(question: string, ctx: AskContext, route?: AskRoute): Answer {
  const parsed = route ? fromRoute(route, ctx) : understand(question, ctx);
  if (parsed.unknownOrder) {
    return { topic: "order", title: `Order ${parsed.unknownOrder} is not in this plan`,
      text: "The plan covers its own set of orders. Open Orders to look up any order in the factory database.", link: { label: "Orders", to: "/orders" } };
  }
  if (route?.material && parsed.topic === "stock") question = route.material;
  const { topic, order, line, day } = parsed;
  const needs: Partial<Record<Topic, Source>> = { order: "plan", crew: "plan", late: "plan", risk: "plan", line: "plan", busiest: "plan",
    built: "plan", plan: "plan", stock: "materials", sales: "summary", quality: "quality", machines: "machines", people: "summary" };
  const source = needs[topic];
  const ready: Record<Source, boolean> = { plan: Boolean(ctx.digest), summary: Boolean(ctx.summary), materials: Boolean(ctx.materials),
    quality: Boolean(ctx.quality), machines: Boolean(ctx.machines) };
  if (source && !ready[source]) return { topic, title: source === "plan" ? "There is no plan for this factory yet" : "Still reading the factory data", missing: source };
  const d = ctx.digest!;
  switch (topic) {
    case "order": return orderAnswer(order!, d, ctx);
    case "crew": return crewAnswer(order!, d, ctx);
    case "late": return lateAnswer(d, ctx, "late");
    case "risk": return lateAnswer(d, ctx, "risk");
    case "line": return lineAnswer(line ?? null, day, d, ctx);
    case "busiest": return busiestAnswer(d, ctx);
    case "built": return builtAnswer(d, ctx);
    case "plan": return planAnswer(d, ctx);
    case "stock": return stockAnswer(question, ctx.materials!);
    case "sales": return salesAnswer(ctx.summary!);
    case "quality": return qualityAnswer(ctx.quality!);
    case "machines": return machinesAnswer(ctx.machines!);
    case "people": return peopleAnswer(ctx.summary!);
    default: return { topic: "help", title: "I can answer questions about this plan and the factory",
      text: "Ask about an order (for example \"why is order 1234 late?\"), a line (\"what is line 2 doing today?\"), late orders, the busiest line, materials in stock, shipments, quality, machines or people." };
  }
}

const stepsOf = (d: Digest, order: string) => (d.gantt.CRAFT_V3 ?? []).filter((g) => g.order_id === order).sort((a, b) => a.step - b.step);
/** A span of clock time: hours up to two days, days after that. */
const hours = (seconds: number) => (seconds >= 2 * DAY ? `${num(seconds / DAY, 1)} days` : `${num(seconds / 3600, 1)} h`);
const words = (v: unknown) => String(v ?? "").replace(/_/g, " ");
const at = (d: Digest, seconds: number) => `${planDate(d.reference_time, seconds / DAY)} ${new Date(new Date(d.reference_time).getTime() + seconds * 1000)
  .toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" })}`;

function orderAnswer(row: OrderRow, d: Digest, ctx: AskContext): Answer {
  const s = standing(row);
  const steps = stepsOf(d, row.order_id);
  const facts: Fact[] = [
    { label: "Due", value: row.due_day != null ? planDate(d.reference_time, row.due_day) : "No due date" },
    { label: "Planned finish", value: s.finish != null ? planDate(d.reference_time, s.finish) : "Not finished in this plan", tone: s.kind === "late" || s.kind === "open" ? "late" : undefined },
    { label: "Pieces", value: row.pieces?.toLocaleString() ?? "—" },
    { label: "Steps", value: String(row.steps) },
  ];
  let title: string;
  if (s.kind === "open") title = `Order ${row.order_id} is not finished in this plan`;
  else if (s.kind === "late") title = `Order ${row.order_id} finishes ${num(-(s.spare ?? 0), 1)} days late`;
  else if (s.kind === "risk") title = `Order ${row.order_id} is on time, with less than a day to spare`;
  else title = `Order ${row.order_id} is on time${s.spare != null ? `, ${num(s.spare, 1)} days early` : ""}`;
  const items: Item[] = [];
  let text: string | undefined;
  if (steps.length) {
    const work = steps.reduce((t, g) => t + (g.end - g.start), 0);
    let waiting = 0, longestWait = { seconds: 0, before: steps[0] };
    for (let i = 1; i < steps.length; i++) {
      const gap = Math.max(0, steps[i].start - steps[i - 1].end);
      waiting += gap;
      if (gap > longestWait.seconds) longestWait = { seconds: gap, before: steps[i] };
    }
    const startWait = Math.max(0, steps[0].start - row.release_day * DAY);
    const longest = steps.reduce((a, b) => (b.end - b.start > a.end - a.start ? b : a));
    const lines = [...new Set(steps.map((g) => ctx.lineLabel(g.line)))];
    facts.push({ label: "Time on its steps", value: hours(work) }, { label: "Waiting between steps", value: hours(waiting) });
    const parts = [`Released ${planDate(d.reference_time, row.release_day)}, first step starts ${at(d, steps[0].start)}${startWait > DAY ? ` (${num(startWait / DAY, 1)} days after release)` : ""}.`,
      `Its longest step is ${longest.name ?? `step ${longest.step}`} (${hours(longest.end - longest.start)}, crew of ${longest.crew}).`];
    if (longestWait.seconds > 3600) parts.push(`The longest wait is ${hours(longestWait.seconds)} before ${longestWait.before.name ?? `step ${longestWait.before.step}`} on ${ctx.lineLabel(longestWait.before.line)}.`);
    parts.push(`Made on ${lines.join(", ")}.`);
    text = parts.join(" ") + " Step times are clock time, so nights and breaks inside a step count.";
    if (s.kind === "late" && row.due_day != null) {
      const needed = row.release_day * DAY + work;
      if (needed > row.due_day * DAY) items.push({ text: "Even with no waiting at all, its work would end after the due date.", tone: "late" });
      else if (startWait + waiting > 0) items.push({ text: `Waiting (${hours(startWait + waiting)} in all) is what makes it late: its lines and crews are busy with other orders.`, tone: "warn" });
    }
  }
  if (row.reason) items.push({ text: row.reason });
  return { topic: "order", title, text, facts, items, link: ctx.runId ? { label: "Open the plan", to: `/runs/${ctx.runId}` } : undefined };
}

function crewAnswer(row: OrderRow, d: Digest, ctx: AskContext): Answer {
  const steps = stepsOf(d, row.order_id);
  if (!steps.length) return { topic: "crew", title: `Order ${row.order_id} has no steps in this plan` };
  const workers = new Set(steps.flatMap((g) => g.workers)), machines = new Set(steps.flatMap((g) => g.machines));
  return {
    topic: "crew", title: `Order ${row.order_id}: ${plural(workers.size, "worker")} and ${plural(machines.size, "machine")}`,
    items: steps.map((g) => ({ text: `${g.name ?? `Step ${g.step}`}: crew of ${g.crew} on ${ctx.lineLabel(g.line)}`,
      sub: `${at(d, g.start)} to ${at(d, g.end)}. Workers ${g.workers.join(", ") || "—"}; machines ${g.machines.join(", ") || "—"}` })),
    link: { label: "Order record", to: `/orders/${row.order_id}` },
  };
}

function lateAnswer(d: Digest, ctx: AskContext, kind: "late" | "risk"): Answer {
  const rows = d.order_rows.map((r) => ({ r, s: standing(r) }));
  const picked = rows.filter(({ s }) => (kind === "late" ? s.kind === "late" || s.kind === "open" : s.kind === "risk"))
    .sort((a, b) => (a.s.spare ?? -Infinity) - (b.s.spare ?? -Infinity));
  const title = kind === "late"
    ? (picked.length ? `${picked.length} of ${d.order_rows.length} orders are late in this plan` : "No order is late in this plan")
    : (picked.length ? `${plural(picked.length, "order")} finish with less than a day to spare` : "No order is cutting it close");
  return {
    topic: kind, title,
    items: picked.slice(0, 8).map(({ r, s }) => ({ order: r.order_id, tone: kind === "late" ? "late" : "warn",
      text: `Order ${r.order_id}`, sub: s.kind === "open" ? "Not finished in this plan" : kind === "late"
        ? `${num(-(s.spare ?? 0), 1)} days late, due ${r.due_day != null ? planDate(d.reference_time, r.due_day) : "—"}`
        : `${num((s.spare ?? 0) * 24, 0)} h to spare` })),
    text: picked.length > 8 ? `Showing the 8 latest of ${picked.length}.` : undefined,
    link: ctx.runId ? { label: "Open the plan", to: `/runs/${ctx.runId}` } : undefined,
  };
}

function lineAnswer(line: number | null, day: number | undefined, d: Digest, ctx: AskContext): Answer {
  if (line == null) return { topic: "line", title: "That line has no work in this plan" };
  const rows = (d.gantt.CRAFT_V3 ?? []).filter((g) => g.line === line);
  const first = rows.length ? Math.floor(Math.min(...rows.map((g) => g.start)) / DAY) : 0;
  const which = day ?? Math.max(0, first);
  const dayRows = rows.filter((g) => g.start < (which + 1) * DAY && g.end > which * DAY).sort((a, b) => a.start - b.start);
  const name = ctx.lineLabel(line), date = planDate(d.reference_time, which);
  if (!dayRows.length) return { topic: "line", title: `${name} has no work planned on ${date}`,
    text: rows.length ? `Its first work starts ${at(d, Math.min(...rows.map((g) => g.start)))}.` : undefined };
  const workHours = dayRows.reduce((t, g) => t + (Math.min(g.end, (which + 1) * DAY) - Math.max(g.start, which * DAY)) / 3600 * g.crew, 0);
  return {
    topic: "line", title: `${name} on ${date}: ${plural(dayRows.length, "step")}`,
    facts: [{ label: "Worker-hours", value: num(workHours, 0) }, { label: "Orders", value: String(new Set(dayRows.map((g) => g.order_id)).size) }],
    items: dayRows.slice(0, 10).map((g) => ({ order: g.order_id, text: `Order ${g.order_id}: ${g.name ?? `step ${g.step}`}`,
      sub: `${at(d, g.start)} to ${at(d, g.end)}, crew of ${g.crew}` })),
    text: dayRows.length > 10 ? `Showing the first 10 of ${dayRows.length}.` : undefined,
  };
}

/** Worker-hours (time x crew) per line over the plan's first two weeks. */
export function lineLoad(rows: GanttRow[]) {
  const total = new Map<number, number>(), byDay = new Map<string, number>();
  for (const g of rows) {
    for (let day = Math.floor(g.start / DAY); day < Math.min(14, Math.ceil(g.end / DAY)); day++) {
      const overlap = Math.min(g.end, (day + 1) * DAY) - Math.max(g.start, day * DAY);
      if (overlap <= 0) continue;
      const h = (overlap / 3600) * g.crew;
      total.set(g.line, (total.get(g.line) ?? 0) + h);
      byDay.set(`${g.line}|${day}`, (byDay.get(`${g.line}|${day}`) ?? 0) + h);
    }
  }
  return { total, byDay };
}

function busiestAnswer(d: Digest, ctx: AskContext): Answer {
  const { total, byDay } = lineLoad(d.gantt.CRAFT_V3 ?? []);
  if (!total.size) return { topic: "busiest", title: "No line has work in the first two weeks of this plan" };
  const ranked = [...total].sort((a, b) => b[1] - a[1]);
  const [line] = ranked[0];
  const peak = [...byDay].filter(([k]) => Number(k.split("|")[0]) === line).sort((a, b) => b[1] - a[1])[0];
  return {
    topic: "busiest", title: `${ctx.lineLabel(line)} is the busiest line`,
    text: `Over the plan's first two weeks. Its heaviest day is ${planDate(d.reference_time, Number(peak[0].split("|")[1]))} with ${num(peak[1], 0)} worker-hours.`,
    items: ranked.slice(0, 6).map(([id, h]) => ({ text: ctx.lineLabel(id), sub: `${num(h, 0)} worker-hours` })),
  };
}

function builtAnswer(d: Digest, ctx: AskContext): Answer {
  const c = d.craft_v3;
  const starts = c.starts ?? [];
  const passed = starts.filter((s) => s.validation).length;
  const facts: Fact[] = [{ label: "Orders", value: String(d.orders) }, { label: "Steps placed", value: String(d.operations) }];
  if (starts.length) facts.push({ label: "Starting plans checked", value: `${passed} of ${starts.length} passed` });
  if (c.accepted_moves != null) facts.push({ label: "Changes kept while improving", value: String(c.accepted_moves) });
  return {
    topic: "built", title: "How CRAFT made this plan", facts,
    text: "CRAFT builds several complete starting plans, checks each one against shifts, machines and crews, keeps the strongest (fewest late orders first, then fewest days late), and then keeps testing changes, keeping only those that pass the same checks and make deliveries better.",
    link: ctx.runId ? { label: "Open the plan", to: `/runs/${ctx.runId}` } : undefined,
  };
}

function planAnswer(d: Digest, ctx: AskContext): Answer {
  const st = d.order_rows.map(standing);
  const count = (k: string) => st.filter((s) => s.kind === k).length;
  const finishes = st.map((s) => s.finish).filter((f): f is number => f != null);
  const late = count("late") + count("open");
  return {
    topic: "plan", title: late ? `${late} of ${d.order_rows.length} orders are late in this plan` : `All ${d.order_rows.length} orders are on time`,
    facts: [{ label: "On time", value: String(count("ontime")), tone: "ok" }, { label: "Under a day to spare", value: String(count("risk")), tone: count("risk") ? "warn" : undefined },
      { label: "Late", value: String(late), tone: late ? "late" : undefined },
      { label: "Last order finishes", value: finishes.length ? planDate(d.reference_time, Math.max(...finishes)) : "—" }],
    link: ctx.runId ? { label: "Open the plan", to: `/runs/${ctx.runId}` } : undefined,
  };
}

function stockAnswer(question: string, data: Materials): Answer {
  const q = question.toLowerCase();
  const all = data.materials;
  const named = all.filter((m) => {
    const name = String(m.name ?? "").toLowerCase(), type = String(m.material_type ?? "").toLowerCase();
    return (name.length > 2 && q.includes(name)) || (type.length > 2 && new RegExp(`\\b${type}s?\\b`).test(q));
  });
  const fmt = (m: Row) => `${n(m.in_stock).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${m.unit ?? ""} in stock`;
  const need = (m: Row) => `open orders need ${n(m.required_open_orders).toLocaleString(undefined, { maximumFractionDigits: 1 })}`;
  const qty = (v: unknown) => n(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
  if (named.length === 1) {
    const m = named[0];
    return { topic: "stock", title: String(m.name),
      facts: [{ label: "In stock", value: `${qty(m.in_stock)} ${m.unit ?? ""}`.trim() },
        { label: "Open orders need", value: qty(m.required_open_orders) },
        { label: "Short by", value: n(m.shortage) > 0 ? qty(m.shortage) : "Nothing", tone: n(m.shortage) > 0 ? "late" : "ok" }],
      link: { label: "Materials", to: "/materials" } };
  }
  if (named.length) {
    return { topic: "stock", title: `${named.length} matching materials`,
      items: named.slice(0, 10).map((m) => ({ text: String(m.name), sub: `${fmt(m)}; ${need(m)}${n(m.shortage) > 0 ? `; short by ${n(m.shortage).toLocaleString(undefined, { maximumFractionDigits: 1 })}` : ""}`,
        tone: n(m.shortage) > 0 ? "late" : "ok" })), link: { label: "Materials", to: "/materials" } };
  }
  const short = all.filter((m) => n(m.shortage) > 0).sort((a, b) => n(b.shortage) - n(a.shortage));
  return {
    topic: "stock", title: short.length ? `${plural(short.length, "material")} short for the open orders` : `Stock covers every open order (${plural(all.length, "material")})`,
    text: "Stock is company-wide; the need is what this factory's open orders still require, wastage included.",
    items: (short.length ? short : [...all].sort((a, b) => n(a.in_stock) - n(b.in_stock))).slice(0, 8).map((m) => ({
      text: String(m.name), sub: `${fmt(m)}; ${need(m)}${n(m.shortage) > 0 ? `; short by ${n(m.shortage).toLocaleString(undefined, { maximumFractionDigits: 1 })}` : ""}`,
      tone: n(m.shortage) > 0 ? "late" : undefined })),
    link: { label: "Materials", to: "/materials" },
  };
}

function salesAnswer(s: ErpSummary): Answer {
  return {
    topic: "sales", title: `${s.shipped.toLocaleString()} orders shipped`,
    facts: [{ label: "Shipped on time", value: s.on_time_delivery_pct != null ? `${num(s.on_time_delivery_pct, 1)}%` : "—",
        tone: s.on_time_delivery_pct == null ? undefined : s.on_time_delivery_pct >= 90 ? "ok" : s.on_time_delivery_pct >= 70 ? "warn" : "late" },
      { label: "Orders in the factory", value: s.orders_total.toLocaleString() },
      { label: "Past due, not shipped", value: s.orders_overdue.toLocaleString(), tone: s.orders_overdue ? "late" : undefined }],
    items: Object.entries(s.orders_by_status).sort((a, b) => b[1] - a[1]).map(([status, count]) => ({ text: words(status), sub: plural(count, "order") })),
    text: "The factory database records orders, pieces and shipments, not prices, so sales are shown as orders shipped rather than money.",
    link: { label: "Orders", to: "/orders" },
  };
}

function qualityAnswer(qd: QualityData): Answer {
  const t = qd.totals;
  const worstLine = [...qd.by_line].sort((a, b) => n(b.dhu) - n(a.dhu))[0];
  const facts: Fact[] = [{ label: "Defects per hundred", value: t.dhu != null ? `${num(n(t.dhu), 2)}` : "—" },
    { label: "Pieces inspected", value: n(t.inspected).toLocaleString() }, { label: "Defective", value: n(t.defective).toLocaleString() }];
  if (worstLine) facts.push({ label: "Weakest line", value: `${prettyLine(worstLine.line)} (${num(n(worstLine.dhu), 2)})`, tone: "warn" });
  return {
    topic: "quality", title: t.dhu != null ? `${num(n(t.dhu), 2)} defects per hundred pieces` : "No quality records yet", facts,
    items: qd.by_type.slice(0, 5).map((r) => ({ text: String(r.defect), sub: `${n(r.defective).toLocaleString()} defective pieces` })),
    link: { label: "Quality", to: "/quality" },
  };
}

function machinesAnswer(machines: Row[]): Answer {
  const down = machines.filter((m) => m.current_status !== "Active");
  return {
    topic: "machines", title: down.length ? `${down.length} of ${machines.length} machines are not running` : `All ${machines.length} machines are running`,
    items: down.slice(0, 10).map((m) => ({ text: `Machine ${m.asset_id}${m.machine_model ? ` (${m.machine_model})` : ""}`,
      sub: String(m.current_status ?? "").replace(/_/g, " ").toLowerCase(), tone: "warn" })),
    link: { label: "Machines", to: "/machines" },
  };
}

function peopleAnswer(s: ErpSummary): Answer {
  const present = s.rostered_today - s.on_leave_today;
  return {
    topic: "people", title: `${present} of ${s.rostered_today} rostered people are present today`,
    facts: [{ label: "On leave", value: String(s.on_leave_today), tone: s.on_leave_today ? "warn" : undefined }],
    items: Object.entries(s.people_by_role).sort((a, b) => b[1] - a[1]).map(([role, count]) => ({ text: words(role), sub: plural(count, "person", "people") })),
    link: { label: "Workers", to: "/workforce" },
  };
}
