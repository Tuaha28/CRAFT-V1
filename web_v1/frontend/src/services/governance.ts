export interface Session { authenticated: boolean; username: string | null; csrf_token: string; can_request: boolean; can_promote: boolean; can_change_priority: boolean; environment: string; live_admission: boolean }
export interface PriorityState { revision?: number; selected_order_id?: string; tier?: string; target_due_seconds?: number; reason?: string; recorded_at?: string; override?: { order_id: string; tier: string; target_due_at: number; override_id: string } | null }
export interface CaptureState { prepared_keys: string[]; selected_keys: string[]; approved_keys: string[]; window_size: number | null; ledger_operations: number; source_hash: string | null }
export interface Scope { id: number; factory_external_id: number; environment: string; source_hash: string; active_version: string | null; priority_state: PriorityState; request_snapshot: Record<string, unknown>; capture?: CaptureState | null }
export interface Run { id: string; scope: number; status: string; cp_status: string; created_at: string; input_hash: string; result?: {
  reason?: string;
  exception?: string | null;
  exit_code?: number;
  controller_status?: string;
  checkpoint?: { revision: number; accepted_keys: string[]; operation_keys: string[]; deferred: Record<string, string>; approval_status: string; pending_windows?: { keys: string[]; cycle_number: number }[] };
  progress?: { accepted: number; remaining: number; cycle_number?: number; window_number?: number; first_failure?: Record<string, string> | null; deferred?: Record<string, string> };
} }
export interface Candidate { id: string; scope: number; run: string; status: string; payload_hash: string; evidence?: { source?: string }; operations: { operation_key: string; start_seconds: number; end_seconds: number; resource_configuration: { line_id: number; machine_ids: number[]; worker_ids: string[] } }[] }
export type ScheduleRow = Candidate["operations"][number];
interface PriorityMetrics { completion_seconds: Record<string, number>; tardiness_seconds: Record<string, number>; makespan_seconds: number }
export interface Comparison { active_version: string | null; candidate_version: string; reference_time: string | null; changed_operation_count: number; changes: {operation_key: string; before: ScheduleRow | null; after: ScheduleRow | null; changed: boolean}[]; validation: {scope_status: string; physical_status: string; replay_status: string; quantity_status: string; materialization_mismatch_count: number | null; candidate_validated: boolean}; priority_impact?: { active: PriorityMetrics | null; candidate: PriorityMetrics | null; customer_deadline_seconds: Record<string, number>; manager_override: {order_id: string; tier: string; target_due_at: number}[] } | null }
export interface GovernanceEvent { id: number; scope: number; actor: string | null; action: string; entity_id: string; reason: string; created_at: string; details: Record<string, unknown> }
interface Page<T> { results: T[] }
let csrf = "";
async function request<T>(path: string, body?: unknown): Promise<T> {
  const response = await fetch(`/api/v1/governance/${path}`, {
    credentials: "same-origin", method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    const detail = await response.json().catch(() => null) as {error?: unknown} | null;
    throw new Error(response.status === 403 ? "Factory-scoped permission required." :
      `Request rejected (${response.status}): ${detail?.error ? JSON.stringify(detail.error) : "check source state and input"}. Active schedule unchanged.`);
  }
  return response.json() as Promise<T>;
}
export const governance = {
  session: async () => { const value = await request<Session>("session"); csrf = value.csrf_token; return value; },
  login: async (username: string, password: string) => { const value = await request<{ csrf_token: string }>("login", { username, password }); csrf = value.csrf_token; },
  scopes: () => request<Page<Scope>>("scopes"),
  events: () => request<Page<GovernanceEvent>>("events"),
  runs: () => request<Page<Run>>("runs"),
  candidates: () => request<Page<Candidate>>("candidates"),
  start: (scope: Scope, idempotency_key: string) => request<Run>("runs", { scope: scope.id, idempotency_key, snapshot: scope.request_snapshot }),
  changePriority: (scope: Scope, order_id: string, tier: string, target_due_seconds: number, reason: string) =>
    request<Scope>(`scopes/${scope.id}/priority`, { order_id, tier, target_due_seconds, reason,
      expected_source_hash: scope.source_hash }),
  selectCapture: (scope: Scope, operation_keys: string[], window_size: number) =>
    request<Scope>(`scopes/${scope.id}/capture-selection`, { operation_keys, window_size }),
  promote: (id: string, reason: string) => request<Candidate>(`candidates/${id}/promote`, { reason }),
  reject: (id: string, reason: string) => request<Candidate>(`candidates/${id}/reject`, { reason }),
  compare: (id: string) => request<Comparison>(`candidates/${id}/compare`),
  cancel: (id: string, reason: string) => request(`runs/${id}/cancel`, { reason }),
};
