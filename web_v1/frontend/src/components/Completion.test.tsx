import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import type { OrderProgress, StepStatus } from "../lib/api";
import { CompleteStep, OrderFinishing } from "./Completion";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const wrap = (ui: ReactNode) => render(<QueryClientProvider client={new QueryClient()}>{ui}</QueryClientProvider>);
const step: StepStatus = { key: "1_10", order_id: "1", step: 1, line: 44, needed: 200, recorded: 0, defective: 0, good: 0,
  state: "NOT_STARTED", final: 0, marked: false, reason: "", by: null, finished_at: null, planned_end: "2026-09-12T10:00:00", previous: null, ready: true, limit: 200, reopened: false };
const posted = () => {
  const fetch = vi.fn(async (_url: string, _init?: RequestInit) => new Response(JSON.stringify({ ...step, state: "COMPLETE" })));
  vi.stubGlobal("fetch", fetch);
  return fetch;
};
const body = (fetch: ReturnType<typeof posted>) => JSON.parse(String(fetch.mock.calls[0][1]?.body ?? "{}"));

describe("completing work", () => {
  it("marks a step complete in one click, even with no pieces recorded", async () => {
    const fetch = posted();
    const done = vi.fn();
    wrap(<CompleteStep d={1} f={4} status={step} label="order 1, step 1" onDone={done} />);
    expect(screen.getByText(/No hourly pieces are recorded/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Mark complete" }));
    await waitFor(() => expect(done).toHaveBeenCalled());
    expect(String(fetch.mock.calls[0][0])).toContain("/floor/1/4/steps/1_10/complete");
    expect(body(fetch)).toEqual({});
  });

  it("closes a step short only with the real count and a reason", async () => {
    const fetch = posted();
    wrap(<CompleteStep d={1} f={4} status={{ ...step, recorded: 150, good: 150, state: "IN_PROGRESS" }} label="order 1, step 1" onDone={() => {}} />);
    fireEvent.click(screen.getByLabelText("Fewer pieces were finished"));
    fireEvent.change(screen.getByLabelText("Pieces finished"), { target: { value: "190" } });
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "10 rejected at cutting" } });
    fireEvent.click(screen.getByRole("button", { name: "Close step short" }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(body(fetch)).toEqual({ pieces: 190, reason: "10 rejected at cutting" });
  });

  it("starts closed short at the count the step before finished", async () => {
    const fetch = posted();
    wrap(<CompleteStep d={1} f={4} status={{ ...step, previous: 1, limit: 150 }} label="order 1, step 2" onDone={() => {}} />);
    expect(screen.getByText(/Step 1 finished 150 pieces/)).toBeInTheDocument();
    expect(screen.getByLabelText("Pieces finished")).toHaveValue(150);
    fireEvent.change(screen.getByLabelText("Reason"), { target: { value: "step 1 closed short" } });
    fireEvent.click(screen.getByRole("button", { name: "Close step short" }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(body(fetch)).toEqual({ pieces: 150, reason: "step 1 closed short" });
  });

  it("offers only the order actions this person may take, and asks for the shipment details", () => {
    vi.stubGlobal("fetch", vi.fn());
    const order: OrderProgress = { order_id: "1", stage: "PACKED", in_plan: true, steps: [{ ...step, state: "COMPLETE" }],
      status: { order_id: "1", stage: "PACKED", stage_label: "Packed", sewn_at: null, inspection_note: "", cartons: 4, carrier: "", tracking: "",
        shipped_on: null, close_reason: "", updated_by: "demo_merchandiser", updated_at: "2026-11-23T10:00:00Z" },
      can: { inspect: false, pack: false, ship: true, close: false, reopen: false } };
    wrap(<OrderFinishing d={1} f={4} order={order} />);
    expect(screen.getByText("1 of 1 steps complete.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Final inspection" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Record shipment" }));
    expect(screen.getByLabelText("Carrier")).toBeRequired();
    expect(screen.getByLabelText("Tracking number")).toBeInTheDocument();
  });
});
