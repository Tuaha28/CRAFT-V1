import { cleanup, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi, afterEach } from "vitest";
import { DeliveryTape } from "./components/DeliveryTape";
import type { OrderRow, Run } from "./lib/api";
import { AutoBanner } from "./components/AutoPlan";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.resetModules(); });
const freshApp = async () => (await import("./app/App")).App;

describe("CRAFT planner", () => {
  it("asks an anonymous visitor to sign in", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({
      csrf_token: "t", authenticated: false, username: null, can_request: false, can_approve: false, production_activation: false,
    }))));
    const App = await freshApp();
    render(<App />);
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeInTheDocument();
  });

  it("gives a customer sign-in the portal and none of the staff pages", async () => {
    const session = { csrf_token: "t", authenticated: true, username: "acme_buyer", can_request: false, can_approve: false,
      production_activation: false, role: "customer", role_label: "Customer", scope_factory: null, scope_line: null, perms: [],
      customer: { customer_id: "CUST-1", client_name: "Acme" } };
    const order = { order_id: "NC-0477", style_code: "1000208", quantity: 200, stage: "In production", progress_percent: 25,
      estimated_ready_date: "2026-09-20", estimated_arrival_date: null, carrier: null, tracking_reference: null, customer_note: null,
      po_number: "PO-1", product: "NeedleCraft Elite Pant", category: "Pant", ordered_on: "2026-08-01", due_date: "2026-09-25",
      shipped_on: null, ready_from: "plan", late: false };
    vi.stubGlobal("fetch", vi.fn(async (url: string) => new Response(JSON.stringify(
      String(url).includes("/portal/orders") ? [order] : session))));
    const App = await freshApp();
    render(<App />);
    expect(await screen.findByRole("link", { name: "NC-0477" }, { timeout: 5000 })).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "Customer portal" })).toBeInTheDocument();
    for (const staff of ["New plan", "Plans", "Line board", "Users", "Resources"]) {
      expect(screen.queryByRole("link", { name: staff })).not.toBeInTheDocument();
    }
  }, 15_000);

  it("reports an unreachable server plainly", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("network"); }));
    const App = await freshApp();
    render(<App />);
    expect(await screen.findByRole("alert", {}, { timeout: 5000 })).toHaveTextContent("planning server is not reachable");
  }, 15_000);

  it("draws late orders in seam red and on-time orders in green", () => {
    const rows: OrderRow[] = [
      { order_id: "1", pieces: 100, release_day: 0, due_day: 5, steps: 3, methods: { CRAFT_V3: { finish_day: 4, complete: true } } },
      { order_id: "2", pieces: 100, release_day: 0, due_day: 5, steps: 3, methods: { CRAFT_V3: { finish_day: 9, complete: true } } },
    ];
    const { container } = render(<DeliveryTape rows={rows} reference="2026-09-10T00:00:00" />);
    expect(container.querySelectorAll(".finish-ok")).toHaveLength(1);
    expect(container.querySelectorAll(".finish-late")).toHaveLength(1);
  });
});

import { LineLoad } from "./components/LineLoad";

describe("line load", () => {
  it("spreads crew hours over the days an operation spans", () => {
    const row = { key: "k", order_id: "1", step: 1, name: null, machine_type: null, line: 44, crew: 2, pieces: 20, machines: [], workers: [],
      start: 20 * 3600, end: 28 * 3600 }; // 4 h on day 0 and 4 h on day 1, crew of 2
    const { container } = render(<LineLoad rows={[row]} reference="2026-09-10T00:00:00" days={3} />);
    const cells = [...container.querySelectorAll("tbody td")].map((td) => td.textContent);
    expect(cells).toEqual(["8", "8", ""]);
  });
});

import { balance } from "./components/LineBalance";

describe("line balancing", () => {
  it("gives operators by SAM share and finds the bottleneck", () => {
    const b = balance([{ step: 1, name: "a", sam: 1 }, { step: 2, name: "b", sam: 2 }, { step: 3, name: "c", sam: 3 }], 6, 480, 1)!;
    expect(b.steps.map((s) => s.operators)).toEqual([1, 2, 3]);
    expect(b.takt).toBeCloseTo(1);             // 6 SAM / 6 operators
    expect(b.targetPerDay).toBeCloseTo(480);
    expect(b.efficiency).toBeCloseTo(100);     // perfectly balanced
    const uneven = balance([{ step: 1, name: "a", sam: 1 }, { step: 2, name: "b", sam: 4 }], 3, 480, 0.8)!;
    expect(uneven.steps.map((s) => s.operators)).toEqual([1, 2]);
    expect(uneven.steps[uneven.bottleneck].name).toBe("b"); // 4/2/0.8 = 2.5 min vs 1/1/0.8
    expect(uneven.balancedPerDay).toBeCloseTo(480 / 2.5);
    expect(balance([{ step: 1, name: "a", sam: 1 }, { step: 2, name: "b", sam: 1 }], 1, 480, 1)).toBeNull();
    // the extra operator goes where it shortens the bottleneck most
    const fix = balance([{ step: 1, name: "a", sam: 1.67 }, { step: 2, name: "b", sam: 0.86 }, { step: 3, name: "c", sam: 2.47 }], 9, 480, 1)!;
    expect(fix.steps.map((s) => s.operators)).toEqual([3, 2, 4]);
    expect(Math.max(...fix.steps.map((s) => s.cycle))).toBeCloseTo(0.6175, 3);
  });
});

import { HourBars } from "./components/Floor";

describe("shop floor", () => {
  const session = (role: string, perms: string[], extra: Record<string, unknown> = {}) => ({
    csrf_token: "t", authenticated: true, username: `demo_${role}`, can_request: perms.includes("planning.request_plan"),
    can_approve: perms.includes("planning.approve_plan"), production_activation: false, role, role_label: role,
    scope_factory: 4, scope_line: null, perms, ...extra,
  });
  const serve = (body: unknown) => vi.fn(async (url: string) => new Response(JSON.stringify(
    url.includes("/planning/session") ? body : url.includes("/planning/datasets") ? [] : url.includes("/planning/runs") ? [] : {})));

  it("shows a line supervisor the floor pages and not planning or users", async () => {
    vi.stubGlobal("fetch", serve(session("supervisor", ["floor.view_floor", "floor.report_issue", "floor.enter_output"], { scope_line: 44 })));
    const App = await freshApp();
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByText("My line")).toBeInTheDocument();
    expect(within(nav).getByText("Line board")).toBeInTheDocument();
    expect(within(nav).queryByText("New plan")).toBeNull();
    expect(within(nav).queryByText("Users")).toBeNull();
    expect(screen.getByText(/line 44/)).toBeInTheDocument();
  });

  it("shows an administrator the users page", async () => {
    vi.stubGlobal("fetch", serve(session("admin", ["planning.request_plan", "planning.approve_plan", "floor.manage_users", "floor.view_floor"])));
    const App = await freshApp();
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Main" });
    expect(within(nav).getByText("Users")).toBeInTheDocument();
    expect(within(nav).getByText("New plan")).toBeInTheDocument();
  });

  it("colours an hour red only when it has passed and is below 85% of plan", () => {
    const { container } = render(<HourBars passed={10} hours={[
      { hour: 8, target: 10, actual: 5 }, { hour: 9, target: 10, actual: 9 }, { hour: 11, target: 10, actual: 0 }]} />);
    const bars = [...container.querySelectorAll("rect.actual")];
    expect(bars.map((b) => b.classList.contains("behind"))).toEqual([true, false, false]); // 11:00 has not passed yet
  });
});

describe("auto-planning", () => {
  const base = { id: "r1", kind: "SCHEDULE", status: "SUCCEEDED", purpose: "DISRUPTION", decision: "PENDING" };
  it("renders the Started by CRAFT banner for an automatic run", () => {
    render(<AutoBanner run={{ ...base, trigger: { source: "AUTO", reason: "Machine 12 down" } } as unknown as Run} />);
    expect(screen.getByText("Started by CRAFT: Machine 12 down")).toBeTruthy();
    expect(screen.getByText(/as if it stays that way for the whole plan/)).toBeTruthy();
  });
  it("says when a newer CRAFT plan replaced this one", () => {
    render(<AutoBanner run={{ ...base, decision: "SUPERSEDED", trigger: { source: "AUTO", reason: "Line 86 stopped" } } as unknown as Run} />);
    expect(screen.getByText("Replaced by a newer CRAFT plan")).toBeTruthy();
  });
  it("shows nothing for a plan a person started", () => {
    const { container } = render(<AutoBanner run={{ ...base, trigger: { source: "MANUAL", reason: "x" } } as unknown as Run} />);
    expect(container.textContent).toBe("");
  });
});
