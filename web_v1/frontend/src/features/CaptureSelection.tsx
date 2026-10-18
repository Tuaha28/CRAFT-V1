import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { governance, type Scope } from "../services/governance";
import { Badge, ErrorBlock, Panel } from "../components/Primitives";
import { nextCaptureKeys } from "../lib/capture";

export function CaptureSelection({ scope, canRequest, onSelected }: { scope: Scope; canRequest: boolean; onSelected: () => void }) {
  const capture = scope.capture;
  const [count, setCount] = useState(25);
  const [windowSize, setWindowSize] = useState(5);
  const select = useMutation({
    mutationFn: () => {
      if (!capture) throw new Error("Select a captured TEST scope.");
      return governance.selectCapture(scope, nextCaptureKeys(capture, count), windowSize);
    },
    onSuccess: onSelected,
  });
  if (!capture) return null;
  const remaining = capture.prepared_keys.length - capture.approved_keys.length;
  const proposed = nextCaptureKeys(capture, count);
  return <Panel title="Captured factory work selection" subtitle={`Factory ${scope.factory_external_id}: ${capture.ledger_operations.toLocaleString()} captured operations; ${capture.prepared_keys.length} source-admissible operations prepared for this TEST cycle.`}>
    <div className="panel-body">
      <div className="craft-demo-notice"><Badge tone="warning">SIMULATED TEST</Badge><span>Pinned read-only capture. One-worker / one-machine execution rules are TEST assumptions, not factory policy. Partial work stays blocked.</span></div>
      <p>{capture.approved_keys.length} approved in TEST · {remaining} prepared operations not yet approved · current selection {capture.selected_keys.length}.</p>
      <label>Operations to schedule next <input aria-label="Operations to schedule next" type="number" min={1} max={Math.min(200, Math.max(1, remaining))} value={count} onChange={event => setCount(Number(event.target.value))}/></label>
      <label>Operations per CP-SAT window <input aria-label="Operations per window" type="number" min={1} max={25} value={windowSize} onChange={event => setWindowSize(Number(event.target.value))}/></label>
      <button className="button" disabled={!canRequest || select.isPending || proposed.length === 0 || windowSize < 1 || windowSize > 25} onClick={() => select.mutate()}>Select next {proposed.length} operations</button>
      <small>Selection only changes this TEST scope's request. Queue a run afterwards; the active TEST schedule stays unchanged until a manager approves a validated candidate.</small>
      {select.error && <ErrorBlock message={String(select.error)}/>}
    </div>
  </Panel>;
}
