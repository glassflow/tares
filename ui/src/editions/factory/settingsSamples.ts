// Sample data for the Settings page, shown at /settings?sample=<name> when the browser has the
// sample flag (see sample.ts). It is invented: project-x, example-org, Alex, Kim, Lee.

import type { Person } from "../../types";
import type { MorningStatus, SettingsGrant, SettingsPage, SettingsTool } from "./FactorySettings.api";

const PACE_OFF = "Pacing is off. The crew keeps starting builders whatever the plan has left.";
const NOTE = "Switching changes the screens, not your data. What only the other edition shows stays reachable under Settings, Advanced.";
const ADV = "/settings/advanced";

const tool = (key: SettingsTool["key"], name: string, detail: string | null, href: string): SettingsTool =>
  ({ key, name, connected: detail !== null, detail: detail ?? "Not connected", href,
     covers: detail === null ? null : COVERS[key] });
const COVERS: Record<SettingsTool["key"], string> = {
  github: "The open projects build in example-org/service-a and example-org/docs",
  linear: "1 open project keeps its tickets in Linear: project-x",
  slack: "No agent posts to a channel yet; the standup asks where to post each time",
  claude_code: "Your sessions and the crew's report here, so their work shows on each ticket",
};
const tools = (gh: string | null, li: string | null, sl: string | null, cc: string | null): SettingsTool[] => [
  tool("github", "GitHub", gh, `${ADV}?tab=github`),
  tool("linear", "Linear", li, `${ADV}?tab=linear`),
  tool("slack", "Slack", sl, `${ADV}?tab=slack`),
  { ...tool("claude_code", "Claude Code", cc, "/sources/claude-code"),
    detail: cc ?? "Not connected. Nothing has reported in 7 days." },
];

const grant = (i: number, text: string, project: string | null, state: string, question?: string): SettingsGrant => ({
  id: `g:${i}`, text, project, scope_text: project ? `In ${project}` : "For every project",
  given: "2026-10-30", state_text: state, ...(question ? { question } : {}),
});

const GRANTS: SettingsGrant[] = [
  grant(0, "Builders may squash-merge any factory/* pull request onto the project-x branch once its checks are green and the reviewer passed the head commit; no approval is needed.", "project-x", "given today"),
  grant(1, "Alex's builders may run `gh pr merge 52 --squash --delete-branch` in project-x", "project-x", "given today"), // words-ok
  grant(2, "Alex's builders may run `gh pr merge 49 --squash --delete-branch` in project-x", "project-x", "given 1 day ago"), // words-ok
  grant(3, "Kim's builders may read the two access keys from the keychain inside their GET calls", "project-x", "given 2 days ago"),
  grant(4, "Builders may push to the project-x branch and move pull request 12 onto it", "project-x", "given 3 days ago"),
  grant(5, "D79 A", "project-x", "given 4 days ago", "Which branch should the next wave of pull requests target?"),
  grant(6, "Lee may delete merged branches in example-org/service-a", null, "given 6 days ago"),
];

const BASE: SettingsPage = {
  can_change: true,
  tools: tools("Connected as the GitHub App on example-org", "Connected to the example-org workspace", null,
               "Reporting: last session 2 minutes ago"),
  crew: {
    how_far: { value: "L4-ship", releases: false, text: "Merges on its own after the crew's review and green checks" },
    release_profile: false,
    builders_max: 4, builders_text: "Up to 4 builders at once",
    challenger: false, pace: false, pace_text: PACE_OFF,
    grants: GRANTS, grants_total: GRANTS.length,
  },
  models: { default_provider: "Anthropic", chosen: true, budget_text: "$38.20 spent so far. Each agent may start up to 50 times a day." },
  people: { keys: { count: 2, text: "2 access keys" }, cloud_team_href: "https://example-org.example.com/team" },
  edition: { value: "factory", label: "Software Factory", can_switch: true,
             options: [{ value: "sre", label: "AI SRE" }], other: { value: "sre", label: "AI SRE" }, note: NOTE },
};

const LONG = "customer-connectors-for-the-enterprise-edition-2026-organisation";
const MANY: SettingsGrant[] = [
  ...GRANTS,
  ...[1, 2, 3, 4, 5].map((i) => grant(10 + i, "Builders may squash-merge factory/* pull requests once checks are green and the reviewer passed them", "project-x", `given ${i + 6} days ago`)),
];

const MORNING: MorningStatus = {
  enabled: true, to_slack: true, to_email: true, channel: "factory-status", recipients: ["alex@example.com", "kim@example.com"],
  time: "09:00", timezone: "Europe/Berlin", email_available: true,
  last_result: "Last sent Tuesday 09:00 to Slack and 2 people.",
};
const SLACK_ON = tools("Connected as the GitHub App on example-org", "Connected to the example-org workspace", "Connected to the example-org Slack", "Reporting: last session 2 minutes ago");
const SLACK_OFF = tools("Connected as the GitHub App on example-org", "Connected to the example-org workspace", null, "Reporting: last session 2 minutes ago");

const person = (id: string, name: string, email: string | null): Person =>
  ({ id, name, email, emails: email ? [email] : [], github: null, is_admin: false, created_via: "console" });

/** The workspace's people, for the samples of the Morning status page. Lee has no email. */
export const SAMPLE_PEOPLE: Person[] = [
  person("p1", "Alex", "alex@example.com"), person("p2", "Kim", "kim@example.com"),
  person("p3", "Lee", null), person("p4", "Sam", "sam@example.com"),
];

const SAMPLES: Record<string, SettingsPage> = {
  busy: { ...BASE, morning: MORNING },
  morning: { ...BASE, tools: SLACK_ON, morning: MORNING },
  morningoff: { ...BASE, tools: SLACK_ON, morning: { ...MORNING, enabled: false, to_slack: false, to_email: false, channel: "", recipients: [], last_result: "" } },
  morningemail: { ...BASE, tools: SLACK_OFF, morning: { ...MORNING, to_slack: false, channel: "", last_result: "" } },
  morningself: { ...BASE, tools: SLACK_ON, morning: { ...MORNING, to_email: false, recipients: [], email_available: false, last_result: "" } },
  morningblocked: { ...BASE, tools: SLACK_ON, morning: { ...MORNING, to_slack: false, channel: "", to_email: true, recipients: [], last_result: "" } },
  newc: {
    ...BASE,
    tools: tools(null, null, null, null),
    crew: { ...BASE.crew, how_far: { value: "L3-review", releases: false, text: "Up to merge" }, builders_max: 3,
            builders_text: "Up to 3 builders at once", grants: [], grants_total: 0 },
    models: { default_provider: "Not chosen yet", chosen: false, budget_text: "No budget set. Each agent may start up to 50 times a day." },
    people: { keys: { count: 0, text: "No access keys yet" }, cloud_team_href: null },
  },
  ro: { ...BASE, can_change: false, edition: { ...BASE.edition, can_switch: false } },
  rel: {
    ...BASE,
    crew: { ...BASE.crew, how_far: { value: "L3-review", releases: true, text: "Up to merge" }, release_profile: true,
            challenger: true, pace: true,
            pace_text: "Pacing is on, and the 5-hour window is at 91%, so no new builder starts." },
  },
  long: {
    ...BASE,
    tools: tools(`Connected as the GitHub App on ${LONG}`, `Connected to the ${LONG} workspace`, null, "Reporting: last session 2 minutes ago"),
    crew: { ...BASE.crew,
            grants: [grant(0, `${GRANTS[0].text} ${GRANTS[0].text} <b>bold</b> <img src=x onerror=alert(1)>`, LONG, "given today"), ...GRANTS.slice(1, 3)],
            grants_total: 3 },
  },
  many: { ...BASE, crew: { ...BASE.crew, grants: MANY, grants_total: MANY.length } },
  load: BASE,
  err: BASE,
};

export const isSample = (name: string | null): name is string => !!name && name in SAMPLES;

/** `load` never answers (the page shows its loading state); `err` fails as a cell that is down. */
export function sampleLoad(name: string): Promise<SettingsPage> {
  if (name === "load") return new Promise(() => {});
  if (name === "err") return Promise.reject(new Error("The cell did not answer."));
  return Promise.resolve(SAMPLES[name]);
}
