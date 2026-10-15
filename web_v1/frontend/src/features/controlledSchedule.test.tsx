import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import ControlledSchedule from "./ControlledSchedule";

vi.mock("@svar-ui/react-gantt", () => ({ Gantt: ({ tasks }: { tasks: { id: string; status?: string }[] }) =>
  <div data-testid="rendered-gantt">{tasks.map(task => <span key={task.id}>{task.id} {task.status}</span>)}</div> }));

function renderPage() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
    <BrowserRouter><ControlledSchedule /></BrowserRouter>
  </QueryClientProvider>);
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("never shows an unvalidated result as a controlled schedule", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(
    String(input).endsWith("session") ? { authenticated: true, csrf_token: "test" } :
      { results: [{ id: "failed", status: "FAILED", operations: [] }] }), { status: 200 })));
  renderPage();
  expect(await screen.findByText("No validated TEST schedule")).toBeInTheDocument();
  expect(screen.queryByTestId("controlled-schedule-gantt")).not.toBeInTheDocument();
});

it("renders persisted validated candidate intervals with explicit TEST status", async () => {
  const operation = { operation_key: "1_2", start_seconds: 0, end_seconds: 600,
    resource_configuration: { line_id: 1, machine_ids: [7], worker_ids: ["9"] } };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    const body = path.endsWith("session") ? { authenticated: true, csrf_token: "test" } :
      path.endsWith("candidates") ? { results: [{ id: "candidate-1", status: "VALIDATED_CANDIDATE", evidence: { source: "SIMULATED_FACTORY1_PARTIAL_CHAIN" }, operations: [operation] }] } :
      { active_version: null, candidate_version: "candidate-1", reference_time: "2026-09-10T06:00:00",
        changed_operation_count: 1, validation: { candidate_validated: true }, changes: [{ operation_key: "1_2", before: null, after: operation, changed: true }] };
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  renderPage();
  expect(await screen.findByTestId("rendered-gantt")).toHaveTextContent("candidate-1_2 CANDIDATE");
  expect(screen.getByText("TEST ONLY")).toBeInTheDocument();
  expect(screen.getByText(/FACTORY-1 SIMULATED CONTINUATION/i)).toBeInTheDocument();
  expect(screen.getByText(/not observed production execution/i)).toBeInTheDocument();
});

it("withholds operation bars when stored validation evidence is not accepted", async () => {
  const operation = { operation_key: "1_2", start_seconds: 0, end_seconds: 600,
    resource_configuration: { line_id: 1, machine_ids: [7], worker_ids: ["9"] } };
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    const body = path.endsWith("session") ? { authenticated: true, csrf_token: "test" } :
      path.endsWith("candidates") ? { results: [{ id: "candidate-1", status: "VALIDATED_CANDIDATE", operations: [operation] }] } :
      { active_version: null, candidate_version: "candidate-1", reference_time: "2026-09-10T06:00:00",
        changed_operation_count: 1, validation: { candidate_validated: false },
        changes: [{ operation_key: "1_2", before: null, after: operation, changed: true }] };
    return new Response(JSON.stringify(body), { status: 200 });
  }));
  renderPage();
  expect(await screen.findByText(/validation evidence is incomplete or invalid/i)).toBeInTheDocument();
  expect(screen.queryByTestId("rendered-gantt")).not.toBeInTheDocument();
});
