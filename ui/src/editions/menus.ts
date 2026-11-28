export type MenuEntry = { key: string; label: string; to: string; group?: string };

/** The factory menu: Home and Integrations, then what the crew builds (Work), who builds it (Team: the
 *  crew and the people) and what it knows (Knowledge, with the Second Brain's pages once it is set
 *  up). The names sit small and muted above their entries; Settings is pinned at the bottom, and a
 *  Setup card sits on top until the factory is set up. */
export const WORK_GROUP = "Work";
export const TEAM_GROUP = "Team";
export const KNOWLEDGE_GROUP = "Knowledge";

/** Settings sits alone at the bottom of the menu, apart from the entries above it. */
export const SETTINGS_ENTRY: MenuEntry = { key: "settings", label: "Settings", to: "/settings" };

/** The entries above Settings, in order. */
export const MENU: MenuEntry[] = [
  { key: "home", label: "Home", to: "/" },
  { key: "integrations", label: "Integrations", to: "/integrations" },
  { key: "projects", label: "Projects", to: "/projects", group: WORK_GROUP },
  { key: "tickets", label: "Tickets", to: "/tickets", group: WORK_GROUP },
  { key: "reviews", label: "Reviews", to: "/reviews", group: WORK_GROUP },
  { key: "report", label: "Report", to: "/report", group: WORK_GROUP },
  { key: "crew", label: "Crew", to: "/crew", group: TEAM_GROUP },
  { key: "people", label: "People", to: "/people", group: TEAM_GROUP },
  { key: "docs", label: "Docs and memory", to: "/docs", group: KNOWLEDGE_GROUP },
];

/** The Second Brain's pages, under Knowledge: shown only on a cell whose brain is set up. */
export const BRAIN_ENTRIES: MenuEntry[] = [
  { key: "brain-repos", label: "Repos", to: "/brain", group: KNOWLEDGE_GROUP },
  { key: "brain-decisions", label: "Decisions", to: "/brain/decisions", group: KNOWLEDGE_GROUP },
  { key: "brain-ask", label: "Ask", to: "/brain/ask", group: KNOWLEDGE_GROUP },
];

/** The entries to show, in order, without Settings. `brain` says the cell has a brain: only then its
 *  pages join Knowledge, after Docs and memory. */
export function visibleEntries(brain = false): MenuEntry[] {
  return brain ? [...MENU, ...BRAIN_ENTRIES] : MENU;
}
