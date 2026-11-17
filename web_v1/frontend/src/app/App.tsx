import { lazy, Suspense, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { MutationCache, QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { Shell } from "../components/Shell";
import { Loading, Notice } from "../components/ui";
import { Login } from "../pages/Login";
import { can, SessionContext, useSession } from "./session";
import { ScopeProvider } from "./scope";
import { homeOf } from "./home";

import { Activity, Board, Compare, Dashboard, Data, Issues, LineToday, Machines, Materials, NewPlan, OrderPage, Orders, Quality, QualityChecks, RunPage, Runs, StylePage, Styles, Users, Workforce, Customers } from "./pages";
import { toast } from "./toast";
import { PageErrorBoundary } from "../components/PageErrorBoundary";

/** Pages need a permission; the server checks it too, this only explains why a page is empty. */
function Need({ perm, children }: { perm: string | string[]; children: ReactNode }) {
  const session = useSession();
  const perms = Array.isArray(perm) ? perm : [perm];
  if (perms.some((p) => can(session, p))) return <>{children}</>;
  return <Notice>Your role ({session.role_label ?? "no role"}) cannot open this page. Ask an administrator if you need it.</Notice>;
}
const PortalApp = lazy(() => import("../portal/PortalApp"));
const PLANNING = ["planning.request_plan", "planning.approve_plan"];
const PLAN_VIEW = [...PLANNING, "floor.view_plans"];

/** "/" is the factory overview for office roles; floor roles land on their own page. */
function Home() {
  const home = homeOf(useSession().role);
  return home ? <Navigate to={home} replace /> : <Dashboard />;
}

// A mutation can carry meta.success: a message, or a function of its input and result, shown as a toast when it works.
const mutationCache = new MutationCache({
  onSuccess: (data, variables, _context, mutation) => {
    const message = mutation.meta?.success;
    const text = typeof message === "function" ? (message as (v: unknown, d: unknown) => string)(variables, data) : message;
    if (typeof text === "string" && text) toast(text);
  },
});
const queryClient = new QueryClient({ mutationCache, defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false, staleTime: 15_000 } } });

function Gate() {
  const session = useQuery({ queryKey: ["session"], queryFn: api.session });
  if (session.isPending) return <Loading label="Connecting to the planning server" />;
  if (session.isError) return <div className="login"><Notice tone="error">{session.error.message}</Notice></div>;
  if (!session.data.authenticated) return <Login />;
  if (session.data.role === "customer") {  // a customer company's sign-in: the portal, nothing of the staff app
    return (
      <SessionContext.Provider value={session.data}>
        <Suspense fallback={<Loading />}><PortalApp /></Suspense>
      </SessionContext.Provider>
    );
  }
  return (
    <SessionContext.Provider value={session.data}>
      <ScopeProvider>
      <BrowserRouter>
        <Shell>
          <PageErrorBoundary>
          <Suspense fallback={<Loading />}>
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/overview" element={<Need perm="floor.view_overview"><Dashboard /></Need>} />
              <Route path="/orders" element={<Need perm="floor.view_orders"><Orders /></Need>} />
              <Route path="/orders/:id" element={<Need perm="floor.view_orders"><OrderPage /></Need>} />
              <Route path="/styles" element={<Need perm="floor.view_orders"><Styles /></Need>} />
              <Route path="/styles/:id" element={<Need perm="floor.view_orders"><StylePage /></Need>} />
              <Route path="/materials" element={<Need perm={["floor.view_overview", "floor.report_issue"]}><Materials /></Need>} />
              <Route path="/workforce" element={<Need perm="floor.view_workers"><Workforce /></Need>} />
              <Route path="/machines" element={<Need perm={["floor.view_machines", "floor.report_issue"]}><Machines /></Need>} />
              <Route path="/quality" element={<Need perm={["floor.view_overview", "floor.record_defect"]}><Quality /></Need>} />
              <Route path="/plan" element={<Need perm="planning.request_plan"><NewPlan /></Need>} />
              <Route path="/runs" element={<Need perm={PLAN_VIEW}><Runs /></Need>} />
              <Route path="/compare" element={<Need perm={PLAN_VIEW}><Compare /></Need>} />
              <Route path="/runs/:id" element={<Need perm={PLAN_VIEW}><RunPage /></Need>} />
              <Route path="/floor/line" element={<Need perm="floor.view_floor"><LineToday /></Need>} />
              <Route path="/floor/board" element={<Need perm="floor.view_floor"><Board /></Need>} />
              <Route path="/floor/issues" element={<Need perm="floor.view_floor"><Issues /></Need>} />
              <Route path="/floor/quality" element={<Need perm="floor.view_floor"><QualityChecks /></Need>} />
              <Route path="/users" element={<Need perm="floor.manage_users"><Users /></Need>} />
              <Route path="/customers" element={<Need perm={["portal.handle_request", "floor.manage_users"]}><Customers /></Need>} />
              <Route path="/data" element={<Need perm={PLANNING}><Data /></Need>} />
              <Route path="/activity" element={<Need perm="floor.view_activity"><Activity /></Need>} />
              <Route path="*" element={<Navigate to="/" replace />} />
            </Routes>
          </Suspense>
          </PageErrorBoundary>
        </Shell>
      </BrowserRouter>
      </ScopeProvider>
    </SessionContext.Provider>
  );
}

export function App() {
  return <QueryClientProvider client={queryClient}><Gate /></QueryClientProvider>;
}
