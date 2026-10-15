import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import { FactoryProvider } from "../app/FactoryContext";
import { ToastProvider } from "../app/ToastContext";
import DashboardPage from "./DashboardPage";
import SchedulePage from "./SchedulePage";
import OrdersPage from "./OrdersPage";
import DisruptionsPage from "./DisruptionsPage";
import CraftPage from "./CraftPage";

vi.mock("@svar-ui/react-gantt", () => ({ Gantt: ({ tasks = [], init }: { tasks: { id: string; text: string }[]; init?: (api: { on: () => void; intercept: () => void }) => void }) => { init?.({ on: () => undefined, intercept: () => undefined }); return <div data-testid="mock-gantt">{tasks.filter(task => !task.id.startsWith("line-")).map(task => <button key={task.id} onClick={() => undefined}>{task.text}</button>)}</div>; } }));

const order = { id: "PO-1001", factory_id: 1, factory_name: "Factory 1", style: "Rib-knit polo", sku: "SK-POLO-04", quantity: 4550, smv: 10.1, release: "2026-09-20T10:00:00Z", due: "2026-10-12T10:00:00Z", progress: 73, current_operation: "Side seam", priority: "Normal", risk: "On track", status: "Running", line_id: "F1-L01" };
const op = { id: "PO-1001-2", order_id: "PO-1001", factory_id: 1, line_id: "F1-L01", operation: "Sewing assembly", machine_id: "M-1-01-01", workers: ["W-1-01-001"], worker_count: 1, start: "2026-10-07T09:00:00Z", end: "2026-10-07T10:40:00Z", status: "Running", risk: "On track", predecessor: "PO-1001-1", successor: "PO-1001-3", release: order.release, due: order.due, smv: 3.4 };
const factory = { id: 1, name: "Factory 1", region: "North · Lahore", health: "Operational", active_orders: 21, running_operations: 49, available_workers: 259, worker_capacity: 301, available_machines: 82, machine_capacity: 90, utilization: 69.8, risk: "Stable" };
const disruption = { id: "EVT-2408", type: "Machine breakdown", severity: "Critical", occurred_at: "2026-10-07T10:00:00Z", entity: "M-1-01-02", factory_id: 1, line_id: "F1-L01", order_id: "PO-1001", impact: "2 operations exposed", status: "Open", craft_action: "Escalation required", description: "Machine stoppage" };
const run = { id: "CR-ACCEPTED", factory_id: 1, trigger: "Worker calendar change", reference_time: "2026-10-07T10:00:00Z", affected_region: "Factory 1 / Line 01", operations_in_window: 3, controller_status: "ACCEPTED", cp_status: "OPTIMAL", runtime_seconds: 11.6, configurations: 31, regimes: 88, recovery_rounds: 0, changed_operations: [], accepted: true, materialization: "PASS", replay: "PASS", physical_validation: "VALID", scope_validation: "SCOPE_VALID", domain_exhausted: false, physical_infeasibility_proven: false, message: "Validated" };
const computationalRun = { ...run, id: "CR-LIMIT", accepted: false, controller_status: "COMPUTATIONAL_LIMIT", cp_status: "UNKNOWN", materialization: "NOT REACHED", replay: "NOT REACHED", physical_validation: "NOT REACHED", scope_validation: "NOT REACHED", domain_exhausted: false, physical_infeasibility_proven: false, message: "CRAFT reached the configured computational safety limit before producing a fully validated repair. The existing validated schedule remains active and this case requires escalation." };
const summary = { active_orders: 21, orders_at_risk: 3, running_operations: 49, delayed_operations: 2, available_workforce: 259, workforce_capacity: 301, available_machines: 82, machine_capacity: 90, production_health: [{ name: "On track", value: 62 }, { name: "At risk", value: 25 }, { name: "Delayed", value: 13 }], hourly_output: [{ hour: "10:00", target: 400, actual: 390 }], attention: [disruption], recent_runs: [computationalRun, run] };
const lines = [{ id: "F1-L01", factory_id: 1, name: "Line 01", active_order: "PO-1001", operation: "Side seam", target: 445, actual: 415, workers: 25, machines_available: 11, status: "Running" }];
const machine = { id: "M-1-01-01", type: "Overlock", factory_id: 1, line_id: "F1-L01", status: "Running", current_operation: "Seam assembly", availability: 100 };
const worker = { id: "W-1-01-001", display_name: "Operator 101-01", factory_id: 1, line_id: "F1-L01", shift: "Shift B · 14–22", skills: ["Overlock"], availability: "Assigned", current_operation: "Seam assembly" };
const analytics = { factory_id: 1, period: "7d", delivery: [{ period: "W-1", on_time: 90, tardiness: 250 }], utilization: [{ name: "Machine", value: 76 }, { name: "Workforce", value: 83 }], stability: [{ week: "W-1", accepted: 92, escalated: 8 }], summary: { on_time_delivery: 88.4, mean_tardiness_hours: 6.3, schedule_stability: 93.1, repairs_accepted: 27, repairs_escalated: 3 } };

function responseFor(path: string, method: string) {
  if (method === "POST" && path.endsWith("/demo/disruptions")) return { demo: true, notice: "Simulation only. No live state changed.", event: disruption };
  if (path.endsWith("/dashboard/summary")) return summary;
  if (path.includes("/factories")) return { count: 1, next: null, previous: null, results: [factory] };
  if (path.includes("/lines")) return { count: 1, next: null, previous: null, results: lines };
  if (path.includes("/orders/PO-1001")) return { ...order, routing: [op], disruptions: [] };
  if (path.includes("/orders")) return { count: 1, next: null, previous: null, results: [order] };
  if (path.includes("/schedule")) return { count: 1, next: null, previous: null, results: [op] };
  if (path.includes("/resources/machines")) return { count: 1, next: null, previous: null, results: [machine] };
  if (path.includes("/resources/workers")) return { count: 1, next: null, previous: null, results: [worker] };
  if (path.includes("/disruptions")) return { count: 1, next: null, previous: null, results: [disruption] };
  if (path.includes("/craft/runs/")) return computationalRun;
  if (path.includes("/craft/runs")) return { count: 2, next: null, previous: null, results: [computationalRun, run] };
  if (path.includes("/analytics")) return analytics;
  if (path.includes("/audit")) return { count: 1, next: null, previous: null, results: [] };
  return {};
}
function renderPage(Page: React.ComponentType) { const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }); return render(<QueryClientProvider client={client}><FactoryProvider><ToastProvider><BrowserRouter><Page/></BrowserRouter></ToastProvider></FactoryProvider></QueryClientProvider>); }

beforeEach(() => { localStorage.clear(); vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => { const url = String(input); const data = responseFor(url, init?.method ?? "GET"); return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json" } }); })); });
afterEach(() => { vi.unstubAllGlobals(); });

describe("operational page smoke coverage", () => {
  it("loads the dashboard summary and factory view", async () => { renderPage(DashboardPage); expect(await screen.findByText("Factory operations")).toBeInTheDocument(); expect(await screen.findByText("Production health")).toBeInTheDocument(); });
  it("loads the schedule Gantt without presenting mock data as production-active", async () => { renderPage(SchedulePage); expect(await screen.findByText("Production schedule")).toBeInTheDocument(); expect(await screen.findByTestId("mock-gantt")).toBeInTheDocument(); expect(screen.getByText(/SIMULATED PLAN/)).toBeInTheDocument(); expect(screen.getByText(/not production-active/)).toBeInTheDocument(); });
  it("opens order details with routing sequence", async () => { renderPage(OrdersPage); fireEvent.click(await screen.findByText("PO-1001")); expect(await screen.findByText("Routing sequence")).toBeInTheDocument(); expect(screen.getByText("Sewing assembly")).toBeInTheDocument(); });
  it("records a disruption as a demo-only action", async () => { renderPage(DisruptionsPage); fireEvent.click(await screen.findByRole("button", { name: /simulate event/i })); fireEvent.change(await screen.findByLabelText("Entity"), { target: { value: "M-1-01-02" } }); fireEvent.change(await screen.findByLabelText("Event description"), { target: { value: "Fixture stoppage" } }); fireEvent.click(screen.getByRole("button", { name: /record demo event/i })); expect(await screen.findByText("Demo disruption recorded")).toBeInTheDocument(); });
  it("presents accepted and computational-limit CRAFT outcomes distinctly", async () => { renderPage(CraftPage); expect(await screen.findByText("CRAFT Control")).toBeInTheDocument(); expect((await screen.findAllByText("COMPUTATIONAL_LIMIT")).length).toBeGreaterThan(0); expect(screen.getAllByText("ACCEPTED").length).toBeGreaterThan(0); expect(screen.getByText(/physical infeasibility proven/i)).toBeInTheDocument(); });
});
