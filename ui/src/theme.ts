import { embedTheme } from "./lib/embed";

export type Theme = "light" | "dark";

const KEY = "tares-theme";

/** Resolve the active theme: the desktop's when the console is embedded, else the explicit choice, else the OS. */
export function getTheme(): Theme {
  const embedded = embedTheme();
  if (embedded) return embedded;
  const saved = localStorage.getItem(KEY);
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** The theme currently painted (set by the inline boot script in index.html). */
export function currentTheme(): Theme {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

export function applyTheme(t: Theme): void {
  document.documentElement.setAttribute("data-theme", t);
  // an embedded console wears the desktop's theme, which is not the person's choice: never saved
  if (embedTheme()) return;
  localStorage.setItem(KEY, t);
}
