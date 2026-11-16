export interface Session {
  csrf_token: string; authenticated: boolean; username: string | null;
  can_request: boolean; can_approve: boolean; production_activation: boolean; writeback?: boolean;
  role: string | null; role_label: string | null; scope_factory: number | null; scope_line: number | null; perms: string[];
  customer?: { customer_id: string; client_name: string };
}
export interface FactorySummary { factory_id: number; orders: number; operations: number; lines?: number; machines?: number; workers?: number }
export interface Dataset {
  id: number; slug: string; label: string; origin: "SNAPSHOT" | "LIVE_CAPTURE"; database: string;
  load_pct: number | null; reference_time: string; factories: FactorySummary[]; captured_at: string;
}
export interface MethodResult {
  policy: string; scheduled: number; deferred: number; complete: number; partial: number; on_time: number; late: number;
  total_tardiness_days: number | null; mean_tardiness_days: number | null; max_tardiness_days: number | null;
  makespan_days: number | null; mean_flow_days: number | null; workers_used: number | null; worker_pool: number | null;
  util_used_pct: number | null; wall_seconds: number | null;
}
export interface RunSummary {
  operations?: number; reference_time?: string; methods?: MethodResult[]; kept_start?: string | null;
  craft_v3?: Partial<MethodResult>; vs_edd_crew?: { late: number | null; mean_tardiness_days: number | null };
  dataset?: string;
  promise?: { order_id: string; finish_day: number | null; due_day: number | null; complete: boolean | null;
    days_late: number | null; status?: string; reason?: string } | null;
}
export type RunStatus = "QUEUED" | "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED";
export interface Run {
  id: string; kind: "SCHEDULE" | "CAPTURE"; status: RunStatus; stage: string; progress: number; mode: "FULL" | "QUICK";
  dataset: Dataset | null; factory_id: number | null; orders: number | null; order_offset: number;
  requested_by: string; created_at: string; started_at: string | null; finished_at: string | null; heartbeat_at: string | null; worker_down?: boolean;
  error: string; summary: RunSummary; decision: "PENDING" | "APPROVED" | "REJECTED" | "SUPERSEDED"; decided_by: string | null;
  decided_at: string | null; decision_reason: string;
  purpose: "PLAN" | "DISRUPTION" | "PROMISE"; parent_id: string | null;
  /** Why the run exists; source AUTO = started by CRAFT's auto-planning. */
  trigger?: RunTrigger;
  scenario: { machines_down?: number[]; workers_absent?: number[]; lines_stopped?: number[];
    release_after?: Record<string, string>; priority_orders?: string[];
    new_order?: { order_id: number; product_id: number; style_code?: string; quantity: number; release: string; due: string } };
}
export interface RunTrigger {
  source?: "AUTO" | "MANUAL"; reason?: string; kinds?: string[]; issue_ids?: number[];
  priority?: { on: string[]; off: string[] }; new_order_ids?: string[];
}
export interface AutoPlanState { enabled: boolean; email: "SMTP" | "OUTBOX" }
export interface GanttRow {
  key: string; order_id: string; step: number; name: string | null; machine_type: string | null; line: number;
  start: number; end: number; crew: number; pieces: number | null; machines: string[]; workers: string[];
}
export interface OrderRow {
  order_id: string; pieces: number | null; release_day: number; due_day: number | null; steps: number;
  methods: Record<string, { finish_day: number | null; complete: boolean }>; status?: string; reason?: string;
}
export interface Digest {
  factory_id: number; reference_time: string; orders: number; operations: number; improve_seconds: number;
  methods: MethodResult[]; order_rows: OrderRow[]; gantt: Record<string, GanttRow[]>; workbook: string | null;
  craft_v3: { kept_start: string | null; start_policy_used: string | null; final_score: number[] | null;
    floor_used: string | null; accepted_moves: number | null; improving_moves: number | null;
    starts: { start: string; operations: number; validation: boolean; score?: number[]; chosen: boolean }[] | null };
}
export interface RunDetail extends Run {
  log_tail: string; digest: Digest | null;
  events: { action: string; message: string; actor: string | null; created_at: string }[];
  writeback?: { ok: boolean; rows: number; error: string; at: string } | null;
}
export interface OrderBook {
  factory_id: number; reference_time: string; orders_in_capture: number; resources: Record<string, number>;
  eligible: { order_id: string; release: string; due: string; due_day: number | null; pieces: number | null; steps: number; work_hours: number; first_step: string | null }[];
}
export interface ActivityEvent { action: string; message: string; run_id: string | null; actor: string | null; created_at: string }

export class ApiError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

let csrf = "";
const BASE = "/api/v1";

async function request<T>(path: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      credentials: "same-origin", method: body === undefined ? "GET" : "POST",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("The planning server is not reachable. Start it and try again.", 0);
  }
  const data = await response.json().catch(() => null) as ({ error?: unknown; detail?: unknown } & Record<string, unknown>) | null;
  if (!response.ok) {
    const raw = data?.error ?? data?.detail ?? (data && Object.values(data).flat().join(" "));
    throw new ApiError(typeof raw === "string" && raw ? raw : `Request failed (${response.status})`, response.status);
  }
  return data as T;
}

export const api = {
  async session() { const s = await request<Session>("/planning/session"); csrf = s.csrf_token; return s; },
  async login(username: string, password: string) {
    const r = await request<{ authenticated: boolean; csrf_token: string }>("/governance/login", { username, password });
    csrf = r.csrf_token; return r;
  },
  logout: () => request<{ authenticated: boolean }>("/planning/logout", {}),
  datasets: () => request<Dataset[]>("/planning/datasets"),
  orderBook: (datasetId: number, factoryId: number) => request<OrderBook>(`/planning/datasets/${datasetId}/factories/${factoryId}/orders`),
  runs: () => request<Run[]>("/planning/runs"),
  run: (id: string) => request<RunDetail>(`/planning/runs/${id}`),
  requestPlan: (input: { dataset_id: number; factory_id: number; orders: number; order_offset: number; mode: "FULL" | "QUICK" }) =>
    request<Run>("/planning/runs", input),
  requestCapture: () => request<Run>("/planning/captures", {}),
  cancel: (id: string, reason: string) => request<{ cancelled: boolean }>(`/planning/runs/${id}/cancel`, { reason }),
  decide: (id: string, decision: "APPROVED" | "REJECTED", reason: string) => request<Run>(`/planning/runs/${id}/decision`, { decision, reason }),
  disruption: (id: string, input: { machines_down: number[]; workers_absent: number[]; mode: "FULL" | "QUICK" }) =>
    request<Run>(`/planning/runs/${id}/disruption`, input),
  promise: (id: string, input: { product_id: number; quantity: number; release: string; due: string; mode: "FULL" | "QUICK" }) =>
    request<Run>(`/planning/runs/${id}/promise`, input),
  events: () => request<ActivityEvent[]>("/planning/events"),
  autoplan: (d: number, f: number) => request<AutoPlanState>(`/planning/autoplan?dataset=${d}&factory=${f}`),
  setAutoplan: (d: number, f: number, enabled: boolean) => request<AutoPlanState>(`/planning/autoplan?dataset=${d}&factory=${f}`, { enabled }),
  priority: (d: number, f: number) => request<{ orders: string[] }>(`/planning/priority?dataset=${d}&factory=${f}`),
  setPriority: (d: number, f: number, orderId: string, on: boolean, reason = "") =>
    request<{ orders: string[] }>("/planning/priority", { dataset: d, factory: f, order_id: orderId, on, reason }),
  workbookUrl: (id: string) => `${BASE}/planning/runs/${id}/workbook`,
  /** Ask CRAFT: is the AI reader on, and its reading of one question (only the question is sent). */
  askStatus: () => request<{ ai: boolean }>("/assistant/status"),
  askRoute: (question: string) => request<{ route: AskRoute }>("/assistant/route", { question }),
};

export interface AskRoute { topic: string; order_id?: string; line?: number; day?: number; material?: string }

/* ---------- ERP (read-only factory database) ---------- */
export type Row = Record<string, string | number | null>;
export interface Paged { total: number; items: Row[] }
export interface ErpEnvelope { database: string; reference_date: string }
export interface ErpSummary extends ErpEnvelope {
  orders_by_status: Record<string, number>; orders_total: number; orders_overdue: number; orders_due_7_days_not_started: number;
  shipped: number; shipped_on_time: number; on_time_delivery_pct: number | null; dhu: number | null; pieces_inspected: number;
  machines_by_status: Record<string, number>; people_by_role: Record<string, number>; rostered_today: number; on_leave_today: number;
  wip_orders: number; wip_pieces: number; lines: number; target_efficiency_pct: number | null;
}
export interface OrderDetail extends ErpEnvelope {
  order: Row; routing: Row[]; materials: Row[]; delivery: Row; work_minutes: number;
}
export interface StyleDetail extends ErpEnvelope { product: Row; variants: Row[]; routing: Row[]; bom: Row[] }
export interface CurrentPlan { run: Run | null; digest: Digest | null }
export type Query = Record<string, string | number | undefined>;

const qs = (params: Query = {}) => {
  const values = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => { if (v !== undefined && v !== "") values.set(k, String(v)); });
  return values.size ? `?${values}` : "";
};
const erpPath = (d: number, f: number, path: string, params?: Query) => `/erp/${d}/${f}/${path}${qs(params)}`;

export const erp = {
  summary: (d: number, f: number) => request<ErpSummary>(erpPath(d, f, "summary")),
  orders: (d: number, f: number, p: Query) => request<Paged & ErpEnvelope>(erpPath(d, f, "orders", p)),
  order: (d: number, f: number, id: number) => request<OrderDetail>(erpPath(d, f, `orders/${id}`)),
  styles: (d: number, f: number, p: Query) => request<Paged & ErpEnvelope & { categories: Row[] }>(erpPath(d, f, "styles", p)),
  style: (d: number, f: number, id: number) => request<StyleDetail>(erpPath(d, f, `styles/${id}`)),
  materials: (d: number, f: number) => request<ErpEnvelope & { materials: Row[]; suppliers: Row[]; stock: Row[] }>(erpPath(d, f, "materials")),
  workforce: (d: number, f: number, p: Query) => request<Paged & ErpEnvelope & { skills: Row[]; crews: Row[]; roles: Row[] }>(erpPath(d, f, "workforce", p)),
  machines: (d: number, f: number, p: Query) => request<Paged & ErpEnvelope & { by_line: Row[]; by_type: Row[] }>(erpPath(d, f, "machines", p)),
  quality: (d: number, f: number) => request<ErpEnvelope & { totals: Row; by_line: Row[]; by_operation: Row[]; by_type: Row[]; worst_orders: Row[] }>(erpPath(d, f, "quality")),
  search: (d: number, f: number, q: string) => request<{ orders: Row[]; people: Row[]; machines: Row[]; styles: Row[] }>(erpPath(d, f, "search", { q })),
  currentPlan: (d: number, f: number) => request<CurrentPlan>(`/planning/current/${d}/${f}`),
};

/* ---------- Shop floor (records kept by this app; the factory database stays read-only) ---------- */
export interface FloorPlanInfo { run_id: string; approved_by: string | null; approved_at: string | null; purpose: string; reference_time: string; lines: number[] }
export interface LineRef { line_id: number; line_number: string }
export interface HourPoint { hour: number; target: number; actual: number }
export type LineState = "ON_TRACK" | "BEHIND" | "NO_DATA" | "NOT_STARTED" | "IDLE";
export interface LineDaySummary {
  hours: HourPoint[]; target_day: number; target_so_far: number; actual_so_far: number; actual_day: number; hours_passed: number;
  efficiency_pct: number | null; state: LineState; checked: number; defects: number; dhu: number | null;
}
export interface FloorOp {
  key: string; order_id: string; step: number; name: string | null; machine_type: string | null; crew: number; pieces: number | null;
  workers: string[]; machines: string[]; worker_names: string[]; machine_models: string[]; windows: [string, string][];
  planned_today: number; planned_before: number; due_day: number | null; hourly_target: Record<string, number>; recorded_total: number;
}
export interface Issue {
  id: number; kind: string; kind_label: string; status: "OPEN" | "IN_PROGRESS" | "RESOLVED"; status_label: string; line_id: number | null;
  machine_id: number | null; worker_id: number | null; order_id: string; note: string; reported_by: string; handled_by: string | null;
  resolution: string; created_at: string; resolved_at: string | null; replan_id: string | null;
}
export interface LineDay {
  plan: FloorPlanInfo; line_id: number; day: number; date: string; days: number[]; today: number; operations: FloorOp[];
  summary: LineDaySummary & { actual_by_op: Record<string, Record<string, number>> }; issues: Issue[];
  can_enter: boolean; can_check: boolean; defect_types: string[];
}
export interface Board {
  plan: FloorPlanInfo | null; day?: number; date?: string; days?: number[]; today?: number; open_issues: number;
  lines: (LineDaySummary & { line_id: number; line_number: string | null; open_issues: number; operations: number })[];
}
export interface IssueList { issues: Issue[]; can_report: boolean; can_handle: boolean; can_repair: boolean; can_replan: boolean; has_plan: boolean }
export interface QualityCheck {
  id: number; line_id: number; day: number; date: string; order_id: string | null; step: number | null; operation: string | null;
  checked: number; defects: number; dhu: number; defect_type: string; note: string; entered_by: string; created_at: string;
}
export interface Checks {
  plan: FloorPlanInfo | null; checks: QualityCheck[];
  by_line: { line_id: number; checked: number; defects: number; dhu: number }[]; by_type: { defect_type: string; defects: number }[];
}
export interface AppUser {
  id: number; username: string; role: string | null; role_label: string | null; factory_id: number | null; line_id: number | null;
  active: boolean; last_login: string | null; date_joined: string; email: string;
}
export interface RoleInfo { key: string; label: string; line_required: boolean }
export interface UserInput { role: string; factory_id: number | null; line_id: number | null; dataset_id: number | null; email?: string }

const floorPath = (d: number, f: number, path: string) => `/floor/${d}/${f}/${path}`;

export const floor = {
  lines: (d: number, f: number) => request<{ lines: LineRef[]; plan: FloorPlanInfo | null; scope_line: number | null }>(floorPath(d, f, "lines")),
  lineDay: (d: number, f: number, line: number, day?: number) =>
    request<LineDay>(floorPath(d, f, `lines/${line}${day === undefined ? "" : `?day=${day}`}`)),
  board: (d: number, f: number, day?: number) => request<Board>(floorPath(d, f, `board${day === undefined ? "" : `?day=${day}`}`)),
  output: (d: number, f: number, input: { line_id: number; day: number; hour: number; operation_key: string; pieces: number }) =>
    request<{ pieces: number }>(floorPath(d, f, "output"), input),
  checks: (d: number, f: number) => request<Checks>(floorPath(d, f, "checks")),
  addCheck: (d: number, f: number, input: { line_id: number; day: number; operation_key: string; checked: number; defects: number; defect_type: string; note: string }) =>
    request<{ id: number }>(floorPath(d, f, "checks"), input),
  issues: (d: number, f: number, open = false) => request<IssueList>(floorPath(d, f, `issues${open ? "?open=1" : ""}`)),
  report: (d: number, f: number, input: { kind: string; machine_id?: number | null; worker_id?: number | null; order_id?: string; line_id?: number | null; note?: string;
    material_id?: number | null; arrival_date?: string | null }) =>
    request<Issue & { warning?: string }>(floorPath(d, f, "issues"), input),
  moveIssue: (id: number, status: "IN_PROGRESS" | "RESOLVED", resolution = "") =>
    request<Issue>(`/floor/issues/${id}/status`, { status, resolution }),
  replan: (d: number, f: number, issue_ids: number[], mode: "QUICK" | "FULL") =>
    request<{ run_id: string }>(floorPath(d, f, "replan"), { issue_ids, mode }),
  users: () => request<{ users: AppUser[]; roles: RoleInfo[] }>("/floor/users"),
  createUser: (input: UserInput & { username: string }) => request<AppUser & { password: string }>("/floor/users", input),
  changeUser: (id: number, input: UserInput & { active: boolean }) => request<AppUser>(`/floor/users/${id}`, input),
  resetPassword: (id: number) => request<{ username: string; password: string }>(`/floor/users/${id}/password`, {}),
};

// ---------- customer portal ----------
export interface PortalOrder {
  order_id: string; style_code: string; quantity: number; stage: string; progress_percent: number;
  estimated_ready_date: string | null; estimated_arrival_date: string | null; carrier: string | null; tracking_reference: string | null;
  customer_note: string | null; po_number: string | null; product: string | null; category: string | null; ordered_on: string | null;
  due_date: string | null; shipped_on: string | null; ready_from: "plan" | "schedule"; late: boolean;
}
export interface PortalProduct { style_code: string; name: string; fabric: string; gsm: number; base_price_usd: number; moq: number; sizes: string }
export interface PortalRequest {
  id: number; request_id: string; kind: "quote" | "sample" | "order_change" | "support"; kind_label: string; description: string;
  order_id: string | null; style_code: string | null; quantity: number | null; colour: string | null; destination: string | null;
  status: "PENDING" | "ACCEPTED" | "DECLINED"; staff_note: string; created_at: string; decided_at: string | null;
  client_name?: string; customer?: string; decided_by?: string | null;
}
export interface PortalRequestInput { kind: string; description: string; order_id?: string; style_code?: string; quantity?: number | null; colour?: string; destination?: string; key?: string }
export interface PendingAction { action_id: string; details: Record<string, unknown>; expires_at: string; status: string }
export interface ChatReply { answer: string; session_id: string; sources: { title?: string; section_id?: string }[]; pending_actions: PendingAction[]; mode: string }
export interface CustomerAccountRow { id: number; customer_id: string; username: string; client_name: string; data: string; active: boolean; created_at: string }

export const portal = {
  me: () => request<{ customer_id: string; client_name: string; username: string; data: string }>("/portal/me"),
  orders: () => request<PortalOrder[]>("/portal/orders"),
  order: (ref: string) => request<PortalOrder>(`/portal/orders/${encodeURIComponent(ref)}`),
  products: () => request<PortalProduct[]>("/portal/products"),
  requests: () => request<PortalRequest[]>("/portal/requests"),
  sendRequest: (input: PortalRequestInput) => request<PortalRequest>("/portal/requests", input),
  chat: (message: string, session_id: string | null) => request<ChatReply>("/portal/chat", { message, session_id }),
  confirm: (actionId: string) => request<{ request_id: string; status: string }>(`/portal/actions/${actionId}/confirm`, {}),
  staffRequests: () => request<PortalRequest[]>("/portal/staff/requests"),
  decide: (id: number, decision: "ACCEPTED" | "DECLINED", note: string) => request<PortalRequest>(`/portal/staff/requests/${id}`, { decision, note }),
  accounts: () => request<CustomerAccountRow[]>("/portal/staff/accounts"),
  createAccount: (input: { username: string; client_name: string; dataset_id: number; email?: string }) =>
    request<CustomerAccountRow & { password: string }>("/portal/staff/accounts", input),
  clients: (dataset: number, q: string) => request<{ client_name: string; orders: number }[]>(`/portal/staff/clients?dataset=${dataset}&q=${encodeURIComponent(q)}`),
};
