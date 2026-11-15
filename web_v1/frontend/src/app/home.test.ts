import { describe, expect, it } from "vitest";
import { homeOf } from "./home";

describe("role home", () => {
  it("sends floor roles to their own page", () => {
    expect(homeOf("supervisor")).toBe("/floor/line");
    expect(homeOf("qc")).toBe("/floor/quality");
    expect(homeOf("maintenance")).toBe("/floor/issues");
  });
  it("keeps office roles on the factory overview", () => {
    for (const role of ["admin", "manager", "planner", "merchandiser", null, undefined]) expect(homeOf(role)).toBeNull();
  });
});
