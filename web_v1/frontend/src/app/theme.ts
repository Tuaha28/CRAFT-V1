import { useSyncExternalStore } from "react";

export type Theme = "dark" | "light";

const KEY = "craft-theme";
const META: Record<Theme, string> = { dark: "#0a0c0f", light: "#f4f7f7" };
const listeners = new Set<() => void>();

/** The theme set on <html>; index.html sets it before the first paint so a page never flashes the wrong one. */
export function currentTheme(): Theme {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

export function setTheme(theme: Theme) {
  const apply = () => {
    document.documentElement.dataset.theme = theme;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", META[theme]);
    listeners.forEach((listener) => listener());
  };
  try { localStorage.setItem(KEY, theme); } catch { /* private window: the choice lasts this visit */ }
  const calm = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
  const doc = document as Document & { startViewTransition?: (update: () => void) => unknown };
  if (doc.startViewTransition && !calm) doc.startViewTransition(apply); else apply();
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, currentTheme, () => "dark");
}
