import { createContext, useContext, useState } from "react";
import { CheckCircle2, X, XCircle } from "lucide-react";

type Toast = { id: number; title: string; description?: string; tone: "success" | "error" };
const ToastContext = createContext<(title: string, description?: string, tone?: Toast["tone"]) => void>(() => undefined);
export function ToastProvider({ children }: { children: React.ReactNode }) { const [toasts, setToasts] = useState<Toast[]>([]); const notify = (title: string, description?: string, tone: Toast["tone"] = "success") => { const id = Date.now() + Math.random(); setToasts(items => [...items, { id, title, description, tone }]); window.setTimeout(() => setToasts(items => items.filter(item => item.id !== id)), 5000); }; return <ToastContext.Provider value={notify}>{children}<div className="toast-stack" aria-live="polite">{toasts.map(item => <div className={`toast toast-${item.tone}`} key={item.id}><span className="toast-icon">{item.tone === "success" ? <CheckCircle2 size={18}/> : <XCircle size={18}/>}</span><div><strong>{item.title}</strong>{item.description && <p>{item.description}</p>}</div><button className="icon-button" aria-label="Dismiss notification" onClick={() => setToasts(items => items.filter(x => x.id !== item.id))}><X size={16}/></button></div>)}</div></ToastContext.Provider>; }
// A context hook intentionally shares this module with its provider.
// eslint-disable-next-line react-refresh/only-export-components
export const useToast = () => useContext(ToastContext);
