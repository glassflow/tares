import { embedded, embedTheme } from "./embed";

export type Theme = "light" | "dark";

const KEY = "tares-theme";

/** Resolve the active theme: the hosting desktop's wins (embed.ts), then an explicit choice, else
 *  the OS. Embedded with no theme named, the console follows the OS as the desktop does, not a
 *  choice saved in a browser visit. */
export function getTheme(): Theme {
  const host = embedTheme();
  if (host) return host;
  const saved = embedded() ? null : localStorage.getItem(KEY);
  if (saved === "light" || saved === "dark") return saved;
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

/** The theme currently painted (set by the inline boot script in index.html). */
export function currentTheme(): Theme {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

/** Paints a theme and keeps it as the browser's choice. Embedded, it is painted only: the desktop
 *  owns the theme, so nothing is kept for a later browser visit. */
export function applyTheme(t: Theme): void {
  document.documentElement.setAttribute("data-theme", t);
  if (!embedded()) localStorage.setItem(KEY, t);
}
