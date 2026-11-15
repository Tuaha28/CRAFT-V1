import { describe, expect, it } from "vitest";
import type { GanttRow } from "./api";
import { clashes, orderEffects } from "./sketch";

const H = 3600;
const op = (key: string, order: string, step: number, start: number, end: number, workers: string[], machines: string[]): GanttRow =>
  ({ key, order_id: order, step, name: null, machine_type: null, line: 1, start: start * H, end: end * H, crew: workers.length, pieces: 10, machines, workers });

const plan = [
  op("a1", "1", 1, 0, 4, ["w1"], ["m1"]),
  op("a2", "1", 2, 4, 8, ["w2"], ["m2"]),
  op("b1", "2", 1, 4, 6, ["w1"], ["m3"]),
  // The plan itself overlaps these two (lot streaming): never reported.
  op("c1", "3", 1, 0, 6, ["w5"], ["m5"]),
  op("c2", "3", 2, 2, 8, ["w6"], ["m6"]),
];

describe("sketch checks", () => {
  it("reports nothing for the untouched plan", () => {
    expect(clashes(plan, {})).toEqual([]);
  });
  it("finds a worker double-booked by a move", () => {
    const found = clashes(plan, { b1: -2 * H });
    expect(found).toContainEqual({ kind: "worker", key: "b1", other: "a1", resource: "w1" });
  });
  it("finds a step moved into its predecessor", () => {
    expect(clashes(plan, { a2: -1 * H })).toEqual([{ kind: "sequence", key: "a1", other: "a2" }]);
  });
  it("ignores overlaps the plan already had", () => {
    expect(clashes(plan, { c2: H })).toEqual([]);
  });
  it("flags a move before the plan start", () => {
    expect(clashes(plan, { a1: -H }).map((c) => c.kind)).toContain("start");
  });
  it("reports the finish change against the due day", () => {
    const [effect] = orderEffects(plan, { a2: 20 * H }, { "1": 0.5 });
    expect(effect.order_id).toBe("1");
    expect(effect.before).toBeCloseTo(8 / 24);
    expect(effect.after).toBeCloseTo(28 / 24);
    expect([effect.lateBefore, effect.lateAfter]).toEqual([false, true]);
  });
});
