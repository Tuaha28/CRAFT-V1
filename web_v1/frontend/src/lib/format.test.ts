import { describe, expect, it } from "vitest";
import { priorityFirst, qty } from "./format";

describe("qty", () => {
  it("shortens millions, groups thousands and shortens units", () => {
    expect(qty(1545186.3, "Kgs")).toBe("1.55 M kg");
    expect(qty("85880.6", "Meters")).toBe("85,881 m");
    expect(qty(2082.6, "Pieces")).toBe("2,083 pcs");
    expect(qty(0, "Pieces")).toBe("0 pcs");
    expect(qty(2.46)).toBe("2.5");
  });
  it("shows a dash for missing values", () => {
    expect(qty(null, "m")).toBe("—");
    expect(qty("abc")).toBe("—");
  });
});

describe("priorityFirst", () => {
  const rows = [{ order_id: "1" }, { order_id: "2" }, { order_id: "3" }, { order_id: "4" }];
  it("moves priority orders to the front and keeps the rest in their order", () => {
    expect(priorityFirst(rows, ["3", "1"]).map((r) => r.order_id)).toEqual(["1", "3", "2", "4"]);
  });
  it("leaves the list alone with no priority orders and does not change the input", () => {
    expect(priorityFirst(rows, []).map((r) => r.order_id)).toEqual(["1", "2", "3", "4"]);
    priorityFirst(rows, ["4"]);
    expect(rows[0].order_id).toBe("1");
  });
});
