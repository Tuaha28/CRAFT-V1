import type { AnalyticsData, AuditEvent, CraftRun, DashboardSummary, Disruption, Factory, Line, Machine, Operation, Order, Page, Worker } from "../types/contracts";

const API = import.meta.env.VITE_API_BASE_URL ?? "/api/v1";
export class ApiError extends Error { constructor(message: string, readonly status: number, readonly details?: unknown) { super(message); } }
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try { response = await fetch(`${API}${path}`, { ...init, headers: { "Content-Type": "application/json", ...init?.headers } }); }
  catch { throw new ApiError("The demo API is not reachable. Start the Django backend and retry.", 0); }
  const data = await response.json();
  if (!response.ok) throw new ApiError(data?.error?.message ?? "The request could not be completed.", response.status, data?.error?.details);
  return data as T;
}
const qs = (params: Record<string, string | number | undefined>) => { const values = new URLSearchParams(); Object.entries(params).forEach(([key, value]) => { if (value !== undefined && value !== "all" && value !== "") values.set(key, String(value)); }); return values.size ? `?${values}` : ""; };
export const api = {
  health: () => request<{ status: string; mode: string; database: string; scheduler: string }>("/health"),
  factories: () => request<Page<Factory> | Factory[]>("/factories?page_size=100"),
  lines: (factoryId?: number) => request<Page<Line> | Line[]>(`/lines${qs({ factory_id: factoryId, page_size: 100 })}`),
  dashboard: (factoryId?: number) => request<DashboardSummary>(`/dashboard/summary${qs({ factory_id: factoryId })}`),
  orders: (params: Record<string, string | number | undefined> = {}) => request<Page<Order>>(`/orders${qs({ page_size: 100, ...params })}`),
  order: (id: string) => request<Order & { routing: Operation[]; disruptions: Disruption[] }>(`/orders/${encodeURIComponent(id)}`),
  machines: (factoryId?: number) => request<Page<Machine>>(`/resources/machines${qs({ factory_id: factoryId, page_size: 100 })}`),
  workers: (factoryId?: number) => request<Page<Worker>>(`/resources/workers${qs({ factory_id: factoryId, page_size: 100 })}`),
  schedule: (params: Record<string, string | number | undefined> = {}) => request<Page<Operation>>(`/schedule${qs({ page_size: 100, ...params })}`),
  disruptions: (factoryId?: number) => request<Page<Disruption>>(`/disruptions${qs({ factory_id: factoryId, page_size: 100 })}`),
  runs: (factoryId?: number) => request<Page<CraftRun>>(`/craft/runs${qs({ factory_id: factoryId, page_size: 100 })}`),
  run: (id: string) => request<CraftRun>(`/craft/runs/${encodeURIComponent(id)}`),
  analytics: (factoryId?: number, period = "7d") => request<AnalyticsData>(`/analytics${qs({ factory_id: factoryId, period })}`),
  audit: (factoryId?: number) => request<Page<AuditEvent>>(`/audit${qs({ factory_id: factoryId, page_size: 100 })}`),
  demoDisruption: (input: Pick<Disruption, "type" | "severity" | "entity" | "factory_id" | "line_id" | "order_id" | "description">) => request<{ demo: true; notice: string; event: Disruption }>("/demo/disruptions", { method: "POST", body: JSON.stringify(input) }),
  demoPriority: (id: string, priority: string) => request<{ demo: true; notice: string; order: Order }>(`/demo/orders/${encodeURIComponent(id)}/priority`, { method: "POST", body: JSON.stringify({ priority }) }),
  demoRun: (factoryId: number, trigger: string) => request<{ demo: true; notice: string; run: CraftRun }>("/demo/craft/run", { method: "POST", body: JSON.stringify({ factory_id: factoryId, trigger }) }),
};
export function rows<T>(data: Page<T> | T[] | undefined): T[] { return Array.isArray(data) ? data : data?.results ?? []; }
