import { useEffect, useState } from "react";

import { api, type SlackChannels } from "../../api";
import type {
  AgentPreset, ConnectorSpec, McpServer, ModelProvider, Plan, PlanWatch, PlanWake, Project,
  Resources, SetupProblem, Source,
} from "../../types";

// Plan edits the Plan step makes in place, and what it reads from the cell to offer choices.
// Every change here keeps the plan's references whole (a swapped source follows into the wake-ups
// that count it, a renamed agent into the handoffs that name it); what cannot be kept whole (a
// wake-up left with no source) is left for /api/setup/check to flag, never dropped quietly.

let seq = 0;
export const newKey = (prefix: string, taken: string[]) => {
  let k = "";
  do { seq += 1; k = `${prefix}${Date.now().toString(36)}${seq}`; } while (taken.includes(k));
  return k;
};

/** A name not yet used among `taken`: "name", else "name-2", "name-3"... */
export const uniqueName = (name: string, taken: string[]) => {
  if (!taken.includes(name)) return name;
  let n = 2;
  while (taken.includes(`${name}-${n}`)) n += 1;
  return `${name}-${n}`;
};

export const kebab = (s: string) =>
  s.trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-").replace(/-{2,}/g, "-").replace(/^-|-$/g, "");

/** A watch put in place of the one with `key` (or added), and the wake-ups that counted the old
 *  source now counting the new one. */
export function putWatch(plan: Plan, w: PlanWatch, key?: string): Plan {
  const old = key ? plan.watches.find((x) => x.key === key) : undefined;
  const watches = old ? plan.watches.map((x) => (x.key === key ? w : x)) : [...plan.watches, w];
  const wakes = old && old.name !== w.name
    ? plan.wakes.map((k) => ({
        ...k, sources: Array.from(new Set(k.sources.map((s) => (s === old.name ? w.name : s)))),
      }))
    : plan.wakes;
  return { ...plan, watches, wakes };
}

/** The watch gone, and gone from the wake-ups that counted it. A wake-up left with no source
 *  stays, for the check to flag. */
export function removeWatch(plan: Plan, key: string): Plan {
  const old = plan.watches.find((x) => x.key === key);
  if (!old) return plan;
  return {
    ...plan,
    watches: plan.watches.filter((x) => x.key !== key),
    wakes: plan.wakes.map((k) => ({ ...k, sources: k.sources.filter((s) => s !== old.name) })),
  };
}

export function putWake(plan: Plan, w: PlanWake, key?: string): Plan {
  return { ...plan, wakes: key ? plan.wakes.map((x) => (x.key === key ? w : x)) : [...plan.wakes, w] };
}

/** An agent renamed: the handoffs that named it follow. */
export function renameAgent(plan: Plan, from: string, to: string): Plan {
  if (from === to) return plan;
  return {
    ...plan,
    agents: plan.agents.map((a) => ({
      ...a, handoffs: a.handoffs.map((h) => (h.agent === from ? { ...h, agent: to } : h)),
    })),
  };
}

/** Problems keyed by where they belong. */
export function problemsBy(problems: SetupProblem[]): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const p of problems) (out[p.where] ??= []).push(p.message);
  return out;
}

/** The parts /api/setup/check derives (sentences, the summary, knobs from a condition) put onto
 *  the plan the person is editing, by key; everything they typed stays as they typed it. */
export function mergeDerived(draft: Plan, checked: Plan): Plan {
  const byKey = <T extends { key: string }>(xs: T[]) => new Map(xs.map((x) => [x.key, x]));
  const cw = byKey(checked.watches), ck = byKey(checked.wakes), ca = byKey(checked.agents);
  const ct = byKey(checked.tools);
  return {
    ...draft,
    summary: checked.summary,
    watches: draft.watches.map((w) => {
      const c = cw.get(w.key);
      if (!c) return w;
      return { ...w, sentence: w.sentence || c.sentence, needs: w.needs || c.needs,
               connector: w.existing ? c.connector : w.connector };
    }),
    wakes: draft.wakes.map((w) => {
      const c = ck.get(w.key);
      return c ? { ...w, sentence: c.sentence, cooldown_sentence: c.cooldown_sentence, knobs: c.knobs } : w;
    }),
    agents: draft.agents.map((a) => {
      const c = ca.get(a.key);
      // a handoff-only agent whose wake-up went away gets the first one (it never starts it):
      // show what will be saved
      return c ? { ...a, sentence: c.sentence, trigger: a.on_trigger ? a.trigger : c.trigger } : a;
    }),
    tools: draft.tools.map((t) => {
      const c = ct.get(t.key);
      return c && t.existing ? { ...t, url: c.url } : t;
    }),
    own_agent: draft.own_agent && checked.own_agent
      ? { ...draft.own_agent, sentence: checked.own_agent.sentence } : draft.own_agent,
  };
}

/** "3 minutes ago", for when a source last received an event. */
export function agoWords(ts: string | null | undefined): string | null {
  if (!ts) return null;
  const s = Math.max(0, (Date.now() - new Date(ts).getTime()) / 1000);
  const n = (v: number, unit: string) => `${v} ${unit}${v === 1 ? "" : "s"} ago`;
  if (s < 60) return "just now";
  if (s < 3600) return n(Math.round(s / 60), "minute");
  if (s < 86400) return n(Math.round(s / 3600), "hour");
  return n(Math.round(s / 86400), "day");
}

export const offeredConnector = (id: string, spec: ConnectorSpec) =>
  !spec.internal && !spec.credential_managed && id !== "claude_code" && id !== "memory";

/** A source counts as a kind's when its connector is that kind, or is the managed variant of it:
 *  the GitHub App source (`github_app`) is a GitHub source, so the person sees one GitHub. */
export const ofSameKind = (connector: string, kind: string) =>
  connector === kind || (kind === "github" && connector === "github_app");

/** What a new source needs from the person: sending to it (push), a credential (a polled source
 *  whose connector takes a secret and has none yet), or nothing. The backend's needs_for. */
export function needsFor(spec: ConnectorSpec | undefined, config: Record<string, unknown>): PlanWatch["needs"] {
  if (!spec) return "none";
  if (spec.mode === "push") return "send";
  const secrets = spec.fields.filter((f) => f.secret).map((f) => f.name);
  // a stored credential named on the source (GitHub's `credential`) stands in for its secret
  return secrets.length && !config.credential && !secrets.some((n) => !!config[n]) ? "credential" : "none";
}

/** What the plan offers to pick from, read from the cell once: its sources, connectors, MCP
 *  servers, projects, the agent presets and the model providers. Each piece is undefined until
 *  it arrives and stays so if the read fails (the editor then says so). */
export interface CellData {
  sources?: Source[];
  connectors?: Record<string, ConnectorSpec>;
  servers?: McpServer[];
  projects?: Project[];
  presets?: AgentPreset[];
  providers?: ModelProvider[];
  models?: string[];
  defaultModel?: string;
  defaultProvider?: string | null;
  defaultModels?: Record<string, string>;
  /** The Slack channels the cell's bot can post to (reason says why there are none). */
  slack?: SlackChannels;
  /** Every part on the cell with the projects that use it (the All resources read). */
  resources?: Resources;
}

export function useCellData(): CellData {
  const [d, setD] = useState<CellData>({});
  useEffect(() => {
    let live = true;
    const put = (patch: Partial<CellData>) => { if (live) setD((cur) => ({ ...cur, ...patch })); };
    api.sources().then((sources) => put({ sources })).catch(() => {});
    api.connectors().then((connectors) => put({ connectors })).catch(() => {});
    api.mcpServers().then((r) => put({ servers: r.servers })).catch(() => {});
    api.projects().then((r) => put({ projects: r.projects })).catch(() => {});
    api.resources().then((resources) => put({ resources })).catch(() => {});
    api.slackChannels().then((slack) => put({ slack }))
      .catch(() => put({ slack: { channels: [], reason: "error" } }));
    api.builtinAgents().then((r) => put({
      presets: r.presets, providers: r.providers, models: r.models, defaultModel: r.default_model,
      defaultProvider: r.default_provider, defaultModels: r.default_models,
    })).catch(() => {});
    return () => { live = false; };
  }, []);
  return d;
}

/** The fields a wake-up can check or count per, for the sources it watches: an existing source's
 *  field profile, a new source's labels and example event. `numeric` are the ones that look like
 *  numbers (what an average can be taken of); `labels` the ones a wake-up can count per. */
export interface FieldChoices { all: string[]; numeric: string[]; labels: string[]; primary: string | null }

const profiles = new Map<string, Promise<FieldChoices>>();

function fromProfile(name: string): Promise<FieldChoices> {
  let p = profiles.get(name);
  if (!p) {
    p = api.sourceFields(name).then((prof) => {
      const labels = (prof.labels ?? []).map((l) => l.name);
      const all = Array.from(new Set([...labels, ...prof.fields.map((f) => f.name)]));
      const numeric = prof.fields
        .filter((f) => f.values.length > 0 && f.values.every((v) => v.value.trim() !== "" && Number.isFinite(Number(v.value))))
        .map((f) => f.name);
      const primary = (prof.labels ?? []).find((l) => l.is_key)?.name ?? null;
      return { all, numeric, labels, primary };
    }).catch(() => ({ all: [], numeric: [], labels: [], primary: null }));
    profiles.set(name, p);
  }
  return p;
}

function fromConfig(w: PlanWatch, spec?: ConnectorSpec): FieldChoices {
  const specs = (Array.isArray(w.config?.labels) ? w.config!.labels : []) as { name?: string; type?: string; primary?: boolean }[];
  const labels = specs.map((l) => String(l.name ?? "")).filter(Boolean);
  const provided = (spec?.provides ?? []).map((p) => p.name);
  const sample = w.sample ?? {};
  const all = Array.from(new Set([...labels, ...provided, ...Object.keys(sample)]));
  const numeric = Array.from(new Set([
    ...specs.filter((l) => l.type === "number").map((l) => String(l.name)),
    ...Object.entries(sample).filter(([, v]) => typeof v === "number").map(([k]) => k),
  ]));
  const primary = specs.find((l) => l.primary)?.name ?? spec?.provides?.find((p) => p.primary)?.name ?? null;
  return { all, numeric, labels: Array.from(new Set([...labels, ...provided])), primary };
}

export function useFieldChoices(watches: PlanWatch[], connectors?: Record<string, ConnectorSpec>): FieldChoices | undefined {
  const sig = watches.map((w) => `${w.existing ? "e" : "n"}:${w.name}:${JSON.stringify(w.config ?? null)}`).join("|");
  const [out, setOut] = useState<FieldChoices>();
  useEffect(() => {
    let live = true;
    Promise.all(watches.map((w) => (w.existing ? fromProfile(w.name)
      : Promise.resolve(fromConfig(w, connectors?.[w.connector]))))).then((xs) => {
      if (!live) return;
      const u = (f: (x: FieldChoices) => string[]) => Array.from(new Set(xs.flatMap(f)));
      setOut({ all: u((x) => x.all), numeric: u((x) => x.numeric), labels: u((x) => x.labels),
               primary: xs.find((x) => x.primary)?.primary ?? null });
    });
    return () => { live = false; };
  }, [sig, connectors]);   // eslint-disable-line react-hooks/exhaustive-deps
  return out;
}

/** Focus the element with this id once React has drawn it (closing an editor hands focus back to
 *  the button that opened it). */
export const focusSoon = (id: string) =>
  window.requestAnimationFrame(() => document.getElementById(id)?.focus());

/** A name a person knows for an MCP server's address: GitHub for api.githubcopilot.com, else
 *  the host. */
const KNOWN_HOSTS: [RegExp, string][] = [
  [/githubcopilot\.com$|github\.com$/, "GitHub"], [/slack\.com$/, "Slack"], [/linear\.app$/, "Linear"],
  [/atlassian\.(com|net)$/, "Atlassian"], [/sentry\.io$/, "Sentry"], [/notion\.(so|com)$/, "Notion"],
];
export function toolTitle(url: string): { title: string; host: string } {
  let host = url;
  try { host = new URL(url).hostname; } catch { /* not a URL: show it as it is */ }
  const known = KNOWN_HOSTS.find(([re]) => re.test(host));
  return { title: known ? known[1] : host, host };
}

/** The MCP servers on the cell grouped by address: servers at the same URL are one tool (made
 *  once per project before parts were shared). The first of a group is the one to attach: one
 *  with credentials set, else the first by name. */
export interface ToolGroup { url: string; title: string; host: string; servers: string[]; pick: string; usedBy: string[] }
export function toolGroups(cell: CellData, exclude: Set<string>): ToolGroup[] {
  const used = new Map((cell.resources?.tools ?? []).map((t) => [t.name, t]));
  const groups = new Map<string, ToolGroup>();
  for (const m of [...(cell.servers ?? [])].sort((a, b) => a.name.localeCompare(b.name))) {
    if (exclude.has(m.name)) continue;
    const url = m.url.trim().replace(/\/+$/, "").toLowerCase();
    const g = groups.get(url) ?? { url: m.url, ...toolTitle(m.url), servers: [], pick: m.name, usedBy: [] };
    g.servers.push(m.name);
    const row = used.get(m.name);
    if (row?.state === "set" && used.get(g.pick)?.state !== "set") g.pick = m.name;
    for (const u of row?.used_by ?? []) if (!g.usedBy.includes(u.name)) g.usedBy.push(u.name);
    groups.set(url, g);
  }
  return [...groups.values()];
}
