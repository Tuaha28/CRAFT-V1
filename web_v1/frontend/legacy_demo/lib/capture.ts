import type { CaptureState } from "../services/governance";

/** Next precedence-ordered operations of a captured factory's prepared TEST domain.
 * The server re-checks membership, approval and route predecessors on submit. */
export function nextCaptureKeys(capture: CaptureState, count: number): string[] {
  const approved = new Set(capture.approved_keys);
  return capture.prepared_keys.filter(key => !approved.has(key)).slice(0, Math.max(0, count));
}
