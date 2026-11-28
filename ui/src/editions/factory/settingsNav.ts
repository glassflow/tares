// The factory Settings sidebar: groups, the items in each, and how an address picks an item. An
// address may name a tab of the old All settings page or a section of the old page; those still land,
// and so do the items Tares 2.0 dropped (Edition, Budget, the agents' tabs), on the nearest item left.

export type SettingId =
  | "crew" | "allowed" | "morning"
  | "github" | "linear" | "slack" | "claude_code"
  | "providers"
  | "people" | "keys"
  | "general" | "usage" | "licence"
  | "areas";

export interface SettingItem { id: string; label: string; to?: string }
export interface SettingGroup { id: string; label: string; items: SettingItem[] }

export const SETTING_GROUPS: SettingGroup[] = [
  { id: "crew", label: "Crew", items: [{ id: "crew", label: "How it works" }, { id: "allowed", label: "What you allowed" }, { id: "morning", label: "Morning status" }] },
  { id: "models", label: "Models", items: [{ id: "providers", label: "Providers" }] },
  { id: "team", label: "Team", items: [{ id: "people", label: "People" }, { id: "keys", label: "Access keys" }] },
  { id: "workspace", label: "Workspace", items: [
    { id: "general", label: "General" }, { id: "usage", label: "Usage" }, { id: "licence", label: "Enterprise licence" },
  ] },
  { id: "advanced", label: "Advanced", items: [
    { id: "areas", label: "Areas" },
  ] },
];

export const FIRST_SETTING: SettingId = "crew";

// Budget and the agents' run budget went with the agents: what is spent is on Usage, and the
// model itself on Providers. The edition is fixed: its old address opens Workspace.
const OLD_TABS: Record<string, SettingId> = {
  access: "keys", anthropic: "providers", agents: "providers", budget: "providers", workspace: "general",
  edition: "general", advanced: "areas",
};
const OLD_SECTIONS: Record<string, SettingId> = {
  crew: "crew", models: "providers", people: "people", edition: "general", advanced: "areas",
};

/** Workspace's first item is General on Tares Cloud and Usage on a self-hosted cell: an address of
 *  one opens the other where it is missing. */
export const WORKSPACE_TWIN: Partial<Record<SettingId, SettingId>> = { general: "usage", usage: "general" };
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
