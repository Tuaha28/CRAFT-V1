import { describe, expect, it } from "vitest";
import { qty } from "./format";

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
