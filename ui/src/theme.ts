import { embedded } from "./embed";

export type Theme = "light" | "dark";

const KEY = "tares-theme";

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
