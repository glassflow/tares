import type { Edition } from "../types";

export type MenuEntry = { key: string; label: string; to: string; today: boolean };

/** Settings sits alone at the bottom of the menu, apart from the entries above it. */
export const SETTINGS_ENTRY: MenuEntry = { key: "settings", label: "Settings", to: "/settings", today: true };

// `today` says the console has a page for the entry now. The others show once a page exists.
export const MENUS: Record<"factory" | "sre", MenuEntry[]> = {
  factory: [
    { key: "home", label: "Home", to: "/", today: true },
    { key: "projects", label: "Projects", to: "/projects", today: true },
    { key: "reviews", label: "Reviews", to: "/reviews", today: false },
    { key: "crew", label: "Crew", to: "/crew", today: true },
    { key: "docs", label: "Docs and memory", to: "/docs", today: false },
    SETTINGS_ENTRY,
  ],
  sre: [
    { key: "home", label: "Home", to: "/", today: true },
    { key: "services", label: "Services", to: "/services", today: false },
    { key: "incidents", label: "Incidents", to: "/incidents", today: false },
    { key: "timeline", label: "Timeline", to: "/explore", today: true },   // Explore until the timeline page exists (TR-752)
    { key: "runbooks", label: "Runbooks and memory", to: "/runbooks", today: false },
    SETTINGS_ENTRY,
  ],
};

/** The entries to show, in order, without Settings. An entry with no page is left out, not greyed. */
export function visibleEntries(edition: Edition, hasPage: (to: string) => boolean = () => false): MenuEntry[] {
  if (edition === "platform") return [];
  return MENUS[edition].filter((e) => e.key !== SETTINGS_ENTRY.key && (e.today || hasPage(e.to)));
}
