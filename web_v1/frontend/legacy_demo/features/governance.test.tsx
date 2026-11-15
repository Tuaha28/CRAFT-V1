import { afterEach, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import GovernancePage from "./GovernancePage";

function renderPage() { return render(<QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><BrowserRouter><GovernancePage/></BrowserRouter></QueryClientProvider>); }
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
it("does not claim live admission and requires login", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({authenticated:false, csrf_token:"test", live_admission:false}), {status:200})));
  renderPage();
  expect(await screen.findByText("Authorized access")).toBeInTheDocument();
  expect(screen.getByText("ISOLATED TEST")).toBeInTheDocument();
  expect(screen.queryByText("Queue isolated CRAFT proof")).not.toBeInTheDocument();
});
it("shows degraded service without fake candidates", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
  renderPage();
  expect(await screen.findByText(/Integration service unavailable/)).toBeInTheDocument();
  expect(screen.queryByText("Approve and promote TEST schedule")).not.toBeInTheDocument();
});

it("displays candidate comparison without granting a read-only user promotion", async () => {
  const operation = {operation_key:"1_2", start_seconds:0, end_seconds:600, resource_configuration:{line_id:1, machine_ids:[1], worker_ids:["1"]}};
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    const body = path.endsWith("session") ? {authenticated:true, username:"reader", csrf_token:"test", can_request:false, can_promote:false}
      : path.endsWith("scopes") ? {results:[{id:1, factory_external_id:1, environment:"TEST", source_hash:"fixture", priority_state:{}, request_snapshot:{}}]}
      : path.endsWith("candidates") ? {results:[{id:"candidate-1", scope:1, status:"VALIDATED_CANDIDATE", operations:[operation]}]}
      : path.endsWith("compare") ? {active_version:null, candidate_version:"candidate-1", reference_time:"2026-09-10T06:00:00", changed_operation_count:1,
          changes:[{operation_key:"1_2", before:null, after:operation, changed:true}], validation:{scope_status:"SCOPE_VALID", physical_status:"VALID", replay_status:"PASS"}}
      : {results:[]};
    return new Response(JSON.stringify(body), {status:200});
  }));
  renderPage();
  await screen.findByRole("option", {name:/Factory 1/});
  fireEvent.change(screen.getByLabelText("Factory test scope"), {target:{value:"1"}});
  const promote = await screen.findByRole("button", {name:"Approve and promote TEST schedule"});
  expect(promote).toBeDisabled();
  fireEvent.click(await screen.findByRole("button", {name:"Compare active and candidate"}));
  expect(await screen.findByRole("img", {name:"Operation 1_2: active and candidate intervals"})).toBeInTheDocument();
  expect(screen.getByText("SCOPE_VALID")).toBeInTheDocument();
  expect(screen.queryByRole("button", {name:"Reject candidate"})).not.toBeInTheDocument();
});

it("submits the server-advertised simulated scope contract, not a hard-coded fixture", async () => {
  const recipe = { contract: "FACTORY1_PARTIAL_CHAIN_V1", source: "SIMULATED_CONTINUATION",
    scenario_hash: "scenario", source_fingerprint: "source" };
  const fetcher = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const body = path.endsWith("session") ? { authenticated: true, username: "manager", csrf_token: "test", can_request: true, can_promote: true } :
      path.endsWith("scopes") ? { results: [{ id: 7, factory_external_id: 1, environment: "TEST_SIM", request_snapshot: recipe }] } :
      path.endsWith("runs") && init?.method === "POST" ? { id: "run", status: "QUEUED" } : { results: [] };
    return new Response(JSON.stringify(body), { status: path.endsWith("runs") && init?.method === "POST" ? 202 : 200 });
  });
  vi.stubGlobal("fetch", fetcher);
  renderPage();
  await screen.findByRole("option", { name: /TEST_SIM/ });
  fireEvent.change(await screen.findByLabelText("Factory test scope"), { target: { value: "7" } });
  fireEvent.click(screen.getByRole("button", { name: "Queue isolated CRAFT proof" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledWith("/api/v1/governance/runs", expect.objectContaining({
    method: "POST", body: expect.stringContaining('"contract":"FACTORY1_PARTIAL_CHAIN_V1"') })));
});

it("does not mix candidates from another selected TEST scope", async () => {
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    const body = path.endsWith("session") ? {authenticated:true, username:"manager", csrf_token:"test", can_request:true, can_promote:true}
      : path.endsWith("scopes") ? {results:[
          {id:1, factory_external_id:1, environment:"TEST_SIM", source_hash:"one", priority_state:{}, request_snapshot:{}},
          {id:2, factory_external_id:1, environment:"TEST_PRIORITY", source_hash:"two", priority_state:{}, request_snapshot:{}},
        ]}
      : path.endsWith("candidates") ? {results:[
          {id:"source-candidate", scope:1, status:"VALIDATED_CANDIDATE", operations:[]},
          {id:"priority-candidate", scope:2, status:"VALIDATED_CANDIDATE", operations:[]},
        ]}
      : {results:[]};
    return new Response(JSON.stringify(body), {status:200});
  }));
  renderPage();
  await screen.findByRole("option", {name:/TEST_PRIORITY/});
  fireEvent.change(screen.getByLabelText("Factory test scope"), {target:{value:"1"}});
  expect(await screen.findByText("Candidate source-c")).toBeInTheDocument();
  expect(screen.queryByText("Candidate priority")).not.toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Factory test scope"), {target:{value:"2"}});
  expect(await screen.findByText("Candidate priority")).toBeInTheDocument();
  expect(screen.queryByText("Candidate source-c")).not.toBeInTheDocument();
});
