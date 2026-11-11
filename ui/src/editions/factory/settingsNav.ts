// The factory Settings sidebar: groups, the items in each, and how an address picks an item. An
// address may name a tab of the old All settings page or a section of the old page; those still land.

export type SettingId =
  | "crew" | "allowed" | "morning"
  | "github" | "linear" | "slack" | "claude_code"
  | "providers" | "budget"
  | "people" | "keys"
  | "general" | "usage" | "edition" | "licence"
  | "agent_tools" | "observability" | "areas";

export interface SettingItem { id: string; label: string; to?: string }
export interface SettingGroup { id: string; label: string; items: SettingItem[] }

export const SETTING_GROUPS: SettingGroup[] = [
  { id: "crew", label: "Crew", items: [{ id: "crew", label: "How it works" }, { id: "allowed", label: "What you allowed" }, { id: "morning", label: "Morning status" }] },
  { id: "tools", label: "Tools", items: [
    { id: "github", label: "GitHub" }, { id: "linear", label: "Linear" }, { id: "slack", label: "Slack" }, { id: "claude_code", label: "Claude Code" },
  ] },
  { id: "models", label: "Models", items: [{ id: "providers", label: "Providers" }, { id: "budget", label: "Budget" }] },
  { id: "team", label: "Team", items: [{ id: "people", label: "People" }, { id: "keys", label: "Access keys" }] },
  { id: "workspace", label: "Workspace", items: [
    { id: "general", label: "General" }, { id: "usage", label: "Usage" }, { id: "edition", label: "Edition" }, { id: "licence", label: "Enterprise licence" },
  ] },
  { id: "advanced", label: "Advanced", items: [
    { id: "agent_tools", label: "Agent tools" }, { id: "observability", label: "Observability" }, { id: "areas", label: "Areas" },
    { id: "connections", label: "Connections", to: "/sources" }, { id: "explore", label: "Explore your data", to: "/explore" },
    { id: "resources", label: "All resources", to: "/resources" }, { id: "connect", label: "Connect an agent", to: "/connect" },
    { id: "reads", label: "Reads", to: "/reads" },
  ] },
];

export const FIRST_SETTING: SettingId = "crew";

const OLD_TABS: Record<string, SettingId> = {
  access: "keys", anthropic: "providers", agents: "budget", workspace: "general", advanced: "agent_tools",
};
const OLD_SECTIONS: Record<string, SettingId> = {
  crew: "crew", tools: "github", models: "providers", people: "people", edition: "edition", advanced: "agent_tools",
};
const PAGES = new Set(SETTING_GROUPS.flatMap((g) => g.items.filter((i) => !i.to).map((i) => i.id)));

/** The item an address names: `?tab=` (new or old name), else an old `#section`. Null: it names none. */
export function settingFrom(search: string, hash: string): SettingId | null {
  const tab = new URLSearchParams(search).get("tab");
  if (tab) {
    const id = OLD_TABS[tab] ?? tab;
    return PAGES.has(id) ? (id as SettingId) : null;
  }
  return OLD_SECTIONS[hash.replace(/^#/, "")] ?? null;
}

/** Only what this cell has: `off` lists the items it does not. A group left empty goes too. */
export function shownGroups(off: Set<string>): SettingGroup[] {
  return SETTING_GROUPS.map((g) => ({ ...g, items: g.items.filter((i) => !off.has(i.id)) })).filter((g) => g.items.length > 0);
}
