import { describe, expect, it } from "vitest";
import { ask, understand, type AskContext } from "./ask";
import type { Digest, ErpSummary, GanttRow } from "./api";

const H = 3600, D = 86_400;
const step = (order: string, n: number, line: number, start: number, end: number, crew = 2): GanttRow => ({
  key: `${order}-${n}`, order_id: order, step: n, name: `Sew ${n}`, machine_type: null, line, start, end, crew, pieces: 100,
  machines: [`M${n}`], workers: [`W${n}`, `W${n + 10}`],
});

// Order 1001: on time (due day 5, finishes day 1). Order 1002: late (due day 1, finishes day 3, waits 2 days between steps).
// Order 1003: not finished in the plan.
const digest = {
  factory_id: 4, reference_time: "2026-01-05T00:00:00", orders: 3, operations: 4, improve_seconds: 120, methods: [], workbook: null,
  craft_v3: { kept_start: null, start_policy_used: null, final_score: null, floor_used: null, accepted_moves: 3, improving_moves: 1,
    starts: [{ start: "A", operations: 4, validation: true, chosen: true }, { start: "B", operations: 4, validation: false, chosen: false }] },
  order_rows: [
    { order_id: "1001", pieces: 100, release_day: 0, due_day: 5, steps: 1, methods: { CRAFT_V3: { finish_day: 1, complete: true } } },
    { order_id: "1002", pieces: 200, release_day: 0, due_day: 1, steps: 2, methods: { CRAFT_V3: { finish_day: 3, complete: true } } },
    { order_id: "1003", pieces: 50, release_day: 0, due_day: 4, steps: 2, methods: { CRAFT_V3: { finish_day: null, complete: false } } },
  ],
  gantt: { CRAFT_V3: [
    step("1001", 1, 7, 8 * H, D),
    step("1002", 1, 7, 8 * H, 16 * H),
    step("1002", 2, 8, 2 * D + 16 * H, 3 * D),
    step("1003", 1, 8, 4 * H, 6 * H),
  ] },
} as unknown as Digest;

const ctx: AskContext = { digest, runId: "r1", lineLabel: (line) => `Line ${line - 6}` };

describe("Ask CRAFT", () => {
  it("finds the order, line and day a question names", () => {
    expect(understand("why is order 1002 late?", ctx)).toMatchObject({ topic: "order", order: { order_id: "1002" } });
    expect(understand("who works on 1001", ctx)).toMatchObject({ topic: "crew" });
    expect(understand("what is line 2 doing on day 3?", ctx)).toMatchObject({ topic: "line", line: 8, day: 2 });
    expect(understand("Why is the worst order late?", ctx).order?.order_id).toBe("1003");  // unfinished counts as the worst
    expect(understand("tell me a joke", ctx).topic).toBe("help");
  });

  it("explains a late order from its own steps", () => {
    const a = ask("why is order 1002 late?", ctx);
    expect(a.title).toBe("Order 1002 finishes 2.0 days late");
    expect(a.facts?.find((f) => f.label === "Waiting between steps")?.value).toBe("2.0 days");
    expect(a.facts?.find((f) => f.label === "Time on its steps")?.value).toBe("16.0 h");
    expect(a.items?.[0].text).toMatch(/Waiting .* is what makes it late/);
  });

  it("lists late orders, unfinished first", () => {
    const a = ask("Which orders are late?", ctx);
    expect(a.title).toBe("2 of 3 orders are late in this plan");
    expect(a.items?.map((i) => i.order)).toEqual(["1003", "1002"]);
  });

  it("ranks lines by worker-hours", () => {
    const a = ask("Which line is busiest?", ctx);
    // Line 1 (id 7): 16 h x2 + 8 h x2 = 48; Line 2 (id 8): 8 h x2 + 2 h x2 = 20.
    expect(a.title).toBe("Line 1 is the busiest line");
    expect(a.items?.map((i) => i.sub)).toEqual(["48 worker-hours", "20 worker-hours"]);
  });

  it("answers stock questions from the materials data, and names a shortage", () => {
    const materials = { materials: [
      { material_id: 1, name: "Cotton fabric", material_type: "Fabric", unit: "m", in_stock: 100, required_open_orders: 250, shortage: 150 },
      { material_id: 2, name: "Thread", material_type: "Trim", unit: "cone", in_stock: 900, required_open_orders: 10, shortage: 0 },
    ] };
    expect(ask("Is any material short?", { ...ctx, materials }).title).toBe("1 material short for the open orders");
    const thread = ask("how much thread do we have", { ...ctx, materials });
    expect(thread.title).toBe("Thread");
    expect(thread.facts?.map((f) => f.value)).toEqual(["900 cone", "10", "Nothing"]);
  });

  it("says sales are orders shipped, since the database holds no prices", () => {
    const summary = { shipped: 40, on_time_delivery_pct: 75, orders_total: 60, orders_overdue: 3, orders_by_status: { Shipped: 40, Confirmed: 20 } } as unknown as ErpSummary;
    const a = ask("how much did we sell?", { ...ctx, summary });
    expect(a.title).toBe("40 orders shipped");
    expect(a.text).toMatch(/not prices/);
  });

  it("waits for data it does not have instead of guessing", () => {
    expect(ask("Is any material short?", ctx).missing).toBe("materials");
    expect(ask("Which orders are late?", { lineLabel: String }).missing).toBe("plan");
  });

  it("follows the AI reading but still works every figure out from the plan", () => {
    // A question the built-in reader would not place: the AI names the order and the topic.
    const a = ask("is the hoodie lot for 1002 going to slip?", ctx, { topic: "order", order_id: "1002" });
    expect(a.title).toBe("Order 1002 finishes 2.0 days late");
    expect(ask("x", ctx, { topic: "line", line: 2, day: 3 }).title).toMatch(/^Line 2 on /);
    expect(ask("x", ctx, { topic: "crew" }).title).toMatch(/^Order 1003/);  // no order named: the worst one
    expect(ask("x", ctx, { topic: "order", order_id: "9999" }).title).toBe("Order 9999 is not in this plan");
    expect(ask("x", ctx, { topic: "make coffee" }).topic).toBe("help");
    const materials = { materials: [{ material_id: 2, name: "Thread", material_type: "Trim", unit: "cone", in_stock: 900, required_open_orders: 10, shortage: 0 }] };
    expect(ask("do we have enough stitching yarn", { ...ctx, materials }, { topic: "stock", material: "thread" }).title).toBe("Thread");
  });

  it("uses no research or baseline wording", () => {
    const words = /\b(FIFO|EDD|SPT|CP-SAT|V3|experiments?|paper)\b/i;
    for (const q of ["how was this plan built?", "why is order 1002 late?", "Which line is busiest?", "plan summary"]) {
      expect(JSON.stringify(ask(q, ctx))).not.toMatch(words);
    }
  });
});
