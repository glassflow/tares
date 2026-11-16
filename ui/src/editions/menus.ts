import type { Edition } from "../types";

export type MenuEntry = { key: string; label: string; to: string; today: boolean; group?: string };

/** The factory menu: Home and Integrations, then what the crew builds (Work), who builds it (Team: the
 *  crew and the people) and what it knows (Knowledge, with the Second Brain's pages once it is set
 *  up). The names sit small and muted above their entries; Settings is pinned at the bottom, and a
 *  Setup card sits on top until the factory is set up. */
export const WORK_GROUP = "Work";
export const TEAM_GROUP = "Team";
export const KNOWLEDGE_GROUP = "Knowledge";

/** Settings sits alone at the bottom of the menu, apart from the entries above it. */
export const SETTINGS_ENTRY: MenuEntry = { key: "settings", label: "Settings", to: "/settings", today: true };


// `today` says the console has a page for the entry now. The others show once a page exists.
export const MENUS: Record<"factory" | "sre", MenuEntry[]> = {
  factory: [
    { key: "home", label: "Home", to: "/", today: true },
    { key: "integrations", label: "Integrations", to: "/integrations", today: false },
    { key: "projects", label: "Projects", to: "/projects", today: true, group: WORK_GROUP },
    { key: "tickets", label: "Tickets", to: "/tickets", today: false, group: WORK_GROUP },
    { key: "reviews", label: "Reviews", to: "/reviews", today: false, group: WORK_GROUP },
    { key: "crew", label: "Crew", to: "/crew", today: true, group: TEAM_GROUP },
    { key: "people", label: "People", to: "/people", today: false, group: TEAM_GROUP },
    { key: "docs", label: "Docs and memory", to: "/docs", today: false, group: KNOWLEDGE_GROUP },
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

/** The Second Brain's pages, under Knowledge: shown only on a factory cell whose brain is set up. The pages
 * come with their own tickets, so these rows do not wait for `hasPage`. */
export const BRAIN_ENTRIES: MenuEntry[] = [
  { key: "brain-repos", label: "Repos", to: "/brain", today: true, group: KNOWLEDGE_GROUP },
  { key: "brain-decisions", label: "Decisions", to: "/brain/decisions", today: true, group: KNOWLEDGE_GROUP },
  { key: "brain-ask", label: "Ask", to: "/brain/ask", today: true, group: KNOWLEDGE_GROUP },
];

/** The entries to show, in order, without Settings. An entry with no page is left out, not greyed.
 * `brain` says the cell has a brain: only then its pages join Knowledge, after Docs and memory. */
export function visibleEntries(edition: Edition, hasPage: (to: string) => boolean = () => false,
                               brain = false): MenuEntry[] {
  if (edition === "platform") return [];
  const own = MENUS[edition].filter((e) => e.key !== SETTINGS_ENTRY.key && (e.today || hasPage(e.to)));
  return edition === "factory" && brain ? [...own, ...BRAIN_ENTRIES] : own;
}
