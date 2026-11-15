import { useQuery } from "@tanstack/react-query";
import { api } from "./api";

export const keys = { factories: ["factories"] as const, dashboard: (f?: number) => ["dashboard", f] as const, lines: (f?: number) => ["lines", f] as const, orders: (f?: number) => ["orders", f] as const, order: (id: string) => ["order", id] as const, schedule: (f?: number) => ["schedule", f] as const, machines: (f?: number) => ["machines", f] as const, workers: (f?: number) => ["workers", f] as const, disruptions: (f?: number) => ["disruptions", f] as const, runs: (f?: number) => ["runs", f] as const, analytics: (f?: number, period = "7d") => ["analytics", f, period] as const, audit: (f?: number) => ["audit", f] as const };
export const useFactories = () => useQuery({ queryKey: keys.factories, queryFn: api.factories, staleTime: 60_000 });
export const useDashboard = (factory?: number) => useQuery({ queryKey: keys.dashboard(factory), queryFn: () => api.dashboard(factory), refetchInterval: 30_000 });
export const useLines = (factory?: number) => useQuery({ queryKey: keys.lines(factory), queryFn: () => api.lines(factory) });
export const useOrders = (factory?: number) => useQuery({ queryKey: keys.orders(factory), queryFn: () => api.orders({ factory_id: factory }) });
export const useSchedule = (factory?: number) => useQuery({ queryKey: keys.schedule(factory), queryFn: () => api.schedule({ factory_id: factory }) });
export const useResources = (factory?: number) => ({ machines: useQuery({ queryKey: keys.machines(factory), queryFn: () => api.machines(factory) }), workers: useQuery({ queryKey: keys.workers(factory), queryFn: () => api.workers(factory) }) });
export const useDisruptions = (factory?: number) => useQuery({ queryKey: keys.disruptions(factory), queryFn: () => api.disruptions(factory) });
export const useRuns = (factory?: number) => useQuery({ queryKey: keys.runs(factory), queryFn: () => api.runs(factory) });
export const useAnalytics = (factory?: number, period = "7d") => useQuery({ queryKey: keys.analytics(factory, period), queryFn: () => api.analytics(factory, period) });
export const useAudit = (factory?: number) => useQuery({ queryKey: keys.audit(factory), queryFn: () => api.audit(factory) });
