// Embedded mode (TR-1553): the Tares desktop hosts the console and already draws the chrome around it
// (its rail, its strip with the cell's name, its own sign-in and its light or dark theme). It opens
// the console with `?embed=desktop&theme=light|dark`; the console then leaves out its own header,
// theme switch and sign out, and takes the desktop's theme. A plain browser visit has neither param
// and is unchanged.
//
// The params are kept in sessionStorage, so a move inside the console (whose links drop the query)
// keeps the mode for the life of the tab. `?embed=` with any other value leaves embedded mode.
// index.html runs the same reading before first paint, so the page never shows its own theme first.
// The contract is written in AGENTS.md, "Console design".
import type { Theme } from "./theme";

const MODE_KEY = "tares-embed";
const THEME_KEY = "tares-embed-theme";

interface Embed { host: "desktop" | null; theme: Theme | null }

function read(): Embed {
  let host: Embed["host"] = null;
  let theme: Theme | null = null;
  try {
    const q = new URLSearchParams(window.location.search);
    if (q.has("embed")) {
      if (q.get("embed") === "desktop") sessionStorage.setItem(MODE_KEY, "desktop");
      else { sessionStorage.removeItem(MODE_KEY); sessionStorage.removeItem(THEME_KEY); }
    }
    const t = q.get("theme");
    if (t === "light" || t === "dark") sessionStorage.setItem(THEME_KEY, t);
    if (sessionStorage.getItem(MODE_KEY) === "desktop") host = "desktop";
    const saved = sessionStorage.getItem(THEME_KEY);
    if (host && (saved === "light" || saved === "dark")) theme = saved;
  } catch { /* no storage: embedded only while the params are in the address */
    const q = new URLSearchParams(window.location.search);
    if (q.get("embed") === "desktop") {
      host = "desktop";
      const t = q.get("theme");
      if (t === "light" || t === "dark") theme = t;
    }
  }
  return { host, theme };
}

// Read once, when the console loads: the desktop opens a new load to change either value.
const EMBED = read();

/** True when the Tares desktop hosts this console. */
export function embedded(): boolean {
  return EMBED.host === "desktop";
}

/** The theme the desktop asked for, or null when not embedded or it named none. */
export function embedTheme(): Theme | null {
  return EMBED.theme;
}
