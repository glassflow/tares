import type { Edition } from "../types";

export type MenuEntry = { key: string; label: string; to: string; today: boolean; group?: string };

/** The factory menu has two groups (TR-1203). The names sit small and muted above their entries. */
export const FACTORY_GROUP = "Factory";
export const BRAIN_GROUP = "Second Brain";

/** Settings sits alone at the bottom of the menu, apart from the entries above it. */
export const SETTINGS_ENTRY: MenuEntry = { key: "settings", label: "Settings", to: "/settings", today: true };

// `today` says the console has a page for the entry now. The others show once a page exists.
export const MENUS: Record<"factory" | "sre", MenuEntry[]> = {
  factory: [
    { key: "home", label: "Home", to: "/", today: true, group: FACTORY_GROUP },
    { key: "projects", label: "Projects", to: "/projects", today: true, group: FACTORY_GROUP },
    { key: "tickets", label: "Tickets", to: "/tickets", today: false, group: FACTORY_GROUP },
    { key: "reviews", label: "Reviews", to: "/reviews", today: false, group: FACTORY_GROUP },
    { key: "crew", label: "Crew", to: "/crew", today: true, group: FACTORY_GROUP },
    { key: "docs", label: "Docs and memory", to: "/docs", today: false, group: FACTORY_GROUP },
    { key: "people", label: "People", to: "/people", today: false, group: FACTORY_GROUP },
    SETTINGS_ENTRY,
  ],
  sre: [
    { key: "home", label: "Home", to: "/", today: true },
    { key: "services", label: "Services", to: "/services", today: false },
    { key: "incidents", label: "Incidents", to: "/incidents", today: false },
    { key: "timeline", label: "Timeline", to: "/timeline", today: true },
    { key: "runbooks", label: "Runbooks and memory", to: "/runbooks", today: false },
    SETTINGS_ENTRY,
  ],
};

/** The Second Brain group: shown only on a factory cell whose brain is set up. The pages come with their own
 * tickets, so these rows do not wait for `hasPage`. */
export const BRAIN_ENTRIES: MenuEntry[] = [
  { key: "brain-repos", label: "Repos", to: "/brain", today: true, group: BRAIN_GROUP },
  { key: "brain-decisions", label: "Decisions", to: "/brain/decisions", today: true, group: BRAIN_GROUP },
  { key: "brain-ask", label: "Ask", to: "/brain/ask", today: true, group: BRAIN_GROUP },
];

/** The entries to show, in order, without Settings. An entry with no page is left out, not greyed.
 * `brain` says the cell has a brain: only then the Second Brain group follows the Factory group. */
export function visibleEntries(edition: Edition, hasPage: (to: string) => boolean = () => false,
                               brain = false): MenuEntry[] {
  if (edition === "platform") return [];
  const own = MENUS[edition].filter((e) => e.key !== SETTINGS_ENTRY.key && (e.today || hasPage(e.to)));
  return edition === "factory" && brain ? [...own, ...BRAIN_ENTRIES] : own;
}
