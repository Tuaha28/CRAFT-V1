import { CheckCircle2, X, XCircle } from "lucide-react";
import { dismiss, useToasts } from "../app/toast";

export function Toaster() {
  const toasts = useToasts();
  return (
    <div className="toaster" role="status" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast ${t.tone}`}>
          {t.tone === "ok" ? <CheckCircle2 size={18} aria-hidden="true" /> : <XCircle size={18} aria-hidden="true" />}
          <span>{t.text}</span>
          <button type="button" onClick={() => dismiss(t.id)} aria-label="Dismiss"><X size={16} aria-hidden="true" /></button>
        </div>
      ))}
    </div>
  );
}
