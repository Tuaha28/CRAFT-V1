import { useSyncExternalStore } from "react";

/** Short confirmations ("Plan approved") that appear in a corner and fade out by themselves. */
export interface Toast { id: number; text: string; tone: "ok" | "error" }

let toasts: Toast[] = [];
let next = 1;
const listeners = new Set<() => void>();
const emit = () => listeners.forEach((l) => l());

export function dismiss(id: number) {
  toasts = toasts.filter((t) => t.id !== id);
  emit();
}

export function toast(text: string, tone: Toast["tone"] = "ok") {
  const id = next++;
  toasts = [...toasts.slice(-2), { id, text, tone }];  // at most three at once
  emit();
  window.setTimeout(() => dismiss(id), tone === "error" ? 7000 : 4000);
}

export function useToasts() {
  return useSyncExternalStore((l) => { listeners.add(l); return () => listeners.delete(l); }, () => toasts, () => toasts);
}
