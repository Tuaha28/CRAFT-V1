import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, type Dataset } from "../lib/api";
import { useSession } from "./session";

/** Which capture (database snapshot) and factory every ERP page and the dashboard show. */
interface Scope { datasets: Dataset[]; dataset: Dataset | null; factoryId: number | null; set: (datasetId: number, factoryId: number) => void }
const ScopeContext = createContext<Scope | null>(null);
const KEY = "craft.scope";

function remembered(): { d: number; f: number } | null {
  try { return JSON.parse(localStorage.getItem(KEY) ?? "null"); } catch { return null; }
}

export function ScopeProvider({ children }: { children: ReactNode }) {
  const datasets = useQuery({ queryKey: ["datasets"], queryFn: api.datasets });
  // A user who works for one factory always sees that factory.
  const own = useSession().scope_factory ?? null;
  const [choice, setChoice] = useState(remembered);
  const list = useMemo(() => datasets.data ?? [], [datasets.data]);

  useEffect(() => {
    if (!list.length) return;
    const valid = choice && (own === null || choice.f === own)
      && list.some((d) => d.id === choice.d && d.factories.some((f) => f.factory_id === choice.f));
    if (!valid) {
      const fits = (x: Dataset) => own === null || x.factories.some((f) => f.factory_id === own);
      const d = list.find((x) => x.load_pct === 30 && fits(x)) ?? list.find(fits) ?? list[0];
      setChoice({ d: d.id, f: (d.factories.find((f) => f.factory_id === (own ?? 4)) ?? d.factories[0]).factory_id });
    }
  }, [list, choice, own]);

  // Until the effect above corrects a remembered choice, expose nothing, so no page queries a factory the user may not see.
  const usable = choice && (own === null || choice.f === own) ? choice : null;
  const value = useMemo<Scope>(() => ({
    datasets: list, dataset: list.find((d) => d.id === usable?.d) ?? null, factoryId: usable?.f ?? null,
    set: (d, f) => { setChoice({ d, f }); try { localStorage.setItem(KEY, JSON.stringify({ d, f })); } catch { /* storage may be blocked */ } },
  }), [list, usable]);
  return <ScopeContext.Provider value={value}>{children}</ScopeContext.Provider>;
}

export function useScope(): Scope {
  const scope = useContext(ScopeContext);
  if (!scope) throw new Error("useScope outside ScopeProvider");
  return scope;
}

/** dataset id and factory id when both are chosen. */
export function useReadyScope(): [number, number] | null {
  const { dataset, factoryId } = useScope();
  return dataset && factoryId ? [dataset.id, factoryId] : null;
}
