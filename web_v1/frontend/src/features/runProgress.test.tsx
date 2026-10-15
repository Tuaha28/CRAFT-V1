import { afterEach, expect, it } from "vitest";
import { cleanup, render, screen } from "@testing-library/react";
import { RunProgress } from "./RunProgress";
import type { Run } from "../services/governance";

afterEach(cleanup);
const base: Run = { id: "run", scope: 1, status: "CHECKPOINTED", cp_status: "REJECTED_BOUNDED_ATTEMPT", created_at: "", input_hash: "hash" };
it("distinguishes a retry checkpoint from approval", () => {
  render(<RunProgress run={{ ...base, result: { checkpoint: { revision: 1, accepted_keys: [], operation_keys: ["a", "b"], deferred: {}, approval_status: "TENTATIVE_NOT_APPROVED", pending_windows: [{keys: ["a"], cycle_number: 1}] } } }}/>);
  expect(screen.getByText("0 accepted · 2 remaining")).toBeInTheDocument();
  expect(screen.getByText(/checkpoint is not manager approval/)).toBeInTheDocument();
  expect(screen.getByText("Next bounded attempt: 1 operations · cycle 1")).toBeInTheDocument();
});
it("shows terminal reasons without undefined cycle values", () => {
  render(<RunProgress run={{ ...base, status: "COMPUTATIONAL_LIMIT", result: { reason: "BOUNDED_LIMIT", progress: {accepted: 0, remaining: 1, deferred: {a: "PREDECESSOR_UNRESOLVED"}} } }}/>);
  expect(screen.getByText("a: PREDECESSOR_UNRESOLVED")).toBeInTheDocument();
  expect(screen.getByText("BOUNDED_LIMIT")).toBeInTheDocument();
  expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
});
it("shows accepted accounting when the final payload omits progress", () => {
  render(<RunProgress run={{ ...base, status: "VALIDATED", result: { checkpoint: {revision: 2, accepted_keys: ["a"], operation_keys: ["a"], deferred: {}, approval_status: "TENTATIVE_NOT_APPROVED", pending_windows: []} } }}/>);
  expect(screen.getByText("1 accepted · 0 remaining")).toBeInTheDocument();
  expect(screen.queryByText(/Next bounded attempt/)).not.toBeInTheDocument();
});
