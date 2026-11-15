import { lazy, Suspense } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { FactoryProvider } from "./FactoryContext";
import { ToastProvider } from "./ToastContext";
import { AppShell } from "../components/AppShell";
import { LoadingBlock } from "../components/Primitives";

const Dashboard = lazy(() => import("../features/DashboardPage"));
const Schedule = lazy(() => import("../features/SchedulePage"));
const Orders = lazy(() => import("../features/OrdersPage"));
const Resources = lazy(() => import("../features/ResourcesPage"));
const Disruptions = lazy(() => import("../features/DisruptionsPage"));
const Craft = lazy(() => import("../features/CraftPage"));
const Analytics = lazy(() => import("../features/AnalyticsPage"));
const Audit = lazy(() => import("../features/AuditPage"));
const Governance = lazy(() => import("../features/GovernancePage"));
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 20_000 } } });
export function App() { return <QueryClientProvider client={queryClient}><FactoryProvider><ToastProvider><BrowserRouter><AppShell><Suspense fallback={<LoadingBlock/>}><Routes><Route path="/" element={<Navigate to="/dashboard" replace/>}/><Route path="/dashboard" element={<Dashboard/>}/><Route path="/schedule" element={<Schedule/>}/><Route path="/orders" element={<Orders/>}/><Route path="/resources" element={<Resources/>}/><Route path="/disruptions" element={<Disruptions/>}/><Route path="/craft" element={<Craft/>}/><Route path="/craft/governance" element={<Governance/>}/><Route path="/analytics" element={<Analytics/>}/><Route path="/history" element={<Audit/>}/><Route path="*" element={<Navigate to="/dashboard" replace/>}/></Routes></Suspense></AppShell></BrowserRouter></ToastProvider></FactoryProvider></QueryClientProvider>; }
