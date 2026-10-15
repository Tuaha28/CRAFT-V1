import { createContext, useContext, useState } from "react";

type FactoryContextValue = { factoryId: number | undefined; setFactoryId: (value: number | undefined) => void };
const FactoryContext = createContext<FactoryContextValue | null>(null);
export function FactoryProvider({ children }: { children: React.ReactNode }) { const [factoryId, setFactoryId] = useState<number | undefined>(() => { const value = localStorage.getItem("craft.factory"); return value && value !== "all" ? Number(value) : undefined; }); const update = (value: number | undefined) => { setFactoryId(value); localStorage.setItem("craft.factory", value === undefined ? "all" : String(value)); }; return <FactoryContext.Provider value={{ factoryId, setFactoryId: update }}>{children}</FactoryContext.Provider>; }
// A context hook intentionally shares this module with its provider.
// eslint-disable-next-line react-refresh/only-export-components
export function useFactory() { const value = useContext(FactoryContext); if (!value) throw new Error("FactoryProvider missing"); return value; }
