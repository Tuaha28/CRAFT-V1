import { lazy, type ComponentType } from "react";

/**
 * Pages load as separate files. Each loader is remembered, so a page's file can be fetched
 * early (when the pointer rests on its menu link, or when the browser is idle) and the page
 * then opens without a loading flash.
 */
type Loader = () => Promise<{ default: ComponentType }>;
const loaders = new Map<string, Loader>();

function page(paths: string[], load: Loader) {
  let started: ReturnType<Loader> | null = null;
  const once: Loader = () => (started ??= load().catch((e) => { started = null; throw e; }));
  for (const p of paths) loaders.set(p, once);
  return lazy(once);
}

export const Dashboard = page(["/", "/overview"], () => import("../pages/Dashboard"));
export const Orders = page(["/orders"], () => import("../pages/erp/Orders"));
export const OrderPage = page(["@order"], () => import("../pages/erp/OrderPage"));
export const Styles = page(["/styles"], () => import("../pages/erp/Styles"));
export const StylePage = page(["@style"], () => import("../pages/erp/Styles").then((m) => ({ default: m.StylePage })));
export const Materials = page(["/materials"], () => import("../pages/erp/Materials"));
export const Workforce = page(["/workforce"], () => import("../pages/erp/Workforce"));
export const Machines = page(["/machines"], () => import("../pages/erp/Machines"));
export const Quality = page(["/quality"], () => import("../pages/erp/Quality"));
export const NewPlan = page(["/plan"], () => import("../pages/NewPlan"));
export const Runs = page(["/runs"], () => import("../pages/Runs"));
export const Compare = page(["/compare"], () => import("../pages/Compare"));
export const RunPage = page(["@run"], () => import("../pages/RunPage"));
export const Data = page(["/data"], () => import("../pages/Data"));
export const Activity = page(["/activity"], () => import("../pages/Activity"));
export const LineToday = page(["/floor/line"], () => import("../pages/floor/LineToday"));
export const Board = page(["/floor/board"], () => import("../pages/floor/Board"));
export const Issues = page(["/floor/issues"], () => import("../pages/floor/Issues"));
export const QualityChecks = page(["/floor/quality"], () => import("../pages/floor/QualityChecks"));
export const Users = page(["/users"], () => import("../pages/Users"));

/** Starts fetching the page file for a menu address; harmless if it is already loaded. */
export function preload(path: string) {
  loaders.get(path)?.().catch(() => {});
}

/** Fetches the given pages one after another while the browser has nothing else to do. */
export function preloadWhenIdle(paths: string[]) {
  const queue = [...paths];
  const idle = (cb: () => void) => { if (typeof window.requestIdleCallback === "function") window.requestIdleCallback(cb, { timeout: 4000 }); else setTimeout(cb, 600); };
  const step = () => {
    const path = queue.shift();
    if (!path) return;
    const load = loaders.get(path);
    (load ? load().catch(() => {}) : Promise.resolve()).then(() => idle(step));
  };
  window.setTimeout(() => idle(step), 1500);
}
