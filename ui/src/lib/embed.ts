// Embedded in the desktop (TR-1553). The Tares desktop app hosts the console in a window with its own
// rail, so it opens the console with `?embed=desktop&theme=light|dark` and the console drops the chrome
// the desktop already draws: the header, the menu and the drawer (editions/Shell.tsx), and it paints the
// desktop's theme instead of its own saved one, without saving it.
//
// The mode lives in sessionStorage under `tares.embed`, so it holds for the tab while a link inside the
// console drops the query. A query value wins over the stored one. Only `desktop` turns the mode on.
import type { Theme } from "../theme";

export const EMBED_KEY = "tares.embed";

export type Embed = { embed: "desktop"; theme?: Theme };

function stored(): Embed | null {
  try {
    const raw = sessionStorage.getItem(EMBED_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw) as Partial<Embed> | null;
    if (!v || v.embed !== "desktop") return null;
    return v.theme === "light" || v.theme === "dark" ? { embed: "desktop", theme: v.theme } : { embed: "desktop" };
  } catch {
    return null;
  }
}

/** The embed state of this page: from the address when it names `embed=desktop`, else from what an earlier
 *  page of the tab stored. Null when the console is not embedded. Writes the state to sessionStorage. */
export function readEmbed(search: string = window.location.search): Embed | null {
  const q = new URLSearchParams(search);
  const before = stored();
  if (q.get("embed") !== "desktop") return before;
  const t = q.get("theme");
  const theme = t === "light" || t === "dark" ? t : before?.theme;
  const now: Embed = theme ? { embed: "desktop", theme } : { embed: "desktop" };
  try { sessionStorage.setItem(EMBED_KEY, JSON.stringify(now)); } catch { /* blocked: the mode holds for this load only */ }
  return now;
}

export function isEmbedded(): boolean {
  return readEmbed() !== null;
}

/** The theme the desktop asked for, or null when it asked for none (the saved or system theme stands). */
export function embedTheme(): Theme | null {
  return readEmbed()?.theme ?? null;
}

/** Paint the desktop's theme. Never saved: the person's own console theme stays what it was. */
export function applyEmbedTheme(): void {
  const t = embedTheme();
  if (t) document.documentElement.setAttribute("data-theme", t);
}
