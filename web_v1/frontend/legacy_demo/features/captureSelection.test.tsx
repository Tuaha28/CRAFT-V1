import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CaptureSelection } from "./CaptureSelection";
import { nextCaptureKeys } from "../lib/capture";
import { RunProgress } from "./RunProgress";
import type { Run, Scope } from "../services/governance";

afterEach(cleanup);
const capture = { prepared_keys: ["1_1", "1_2", "2_1", "2_2"], selected_keys: ["1_1", "1_2"], approved_keys: ["1_1", "1_2"],
  window_size: 5, ledger_operations: 94, source_hash: "hash" };
const scope: Scope = { id: 7, factory_external_id: 4, environment: "TEST_CAPTURE", source_hash: "s", active_version: "v",
  priority_state: {}, request_snapshot: {}, capture };

it("proposes the next unapproved operations in prepared domain order", () => {
  expect(nextCaptureKeys(capture, 1)).toEqual(["2_1"]);
  expect(nextCaptureKeys(capture, 10)).toEqual(["2_1", "2_2"]);
  expect(nextCaptureKeys(capture, 0)).toEqual([]);
});

it("labels captured work as simulated TEST and counts approved operations", () => {
  render(<QueryClientProvider client={new QueryClient()}><CaptureSelection scope={scope} canRequest onSelected={() => undefined}/></QueryClientProvider>);
  expect(screen.getByText("SIMULATED TEST")).toBeInTheDocument();
  expect(screen.getByText(/2 approved in TEST · 2 prepared operations not yet approved/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Select next 2 operations" })).toBeEnabled();
});

it("shows a worker failure's exception instead of an unexplained failure", () => {
  const run: Run = { id: "r", scope: 7, status: "FAILED", cp_status: "", created_at: "", input_hash: "h",
    result: { reason: "WORKER_PROCESS_FAILED", exception: "ValueError: min() iterable argument is empty" } };
  render(<RunProgress run={run}/>);
  expect(screen.getByText("ValueError: min() iterable argument is empty")).toBeInTheDocument();
});
