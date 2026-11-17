import type { ConnectorSpec, Source } from "../types";

// What the connections list and a connection's page say about one connection, in one place.

// The words a person reads for each health state (statusTones.ts SourceHealthState). The tone stays keyed
// by the daemon's value.
const STATE_WORDS: Record<string, string> = {
  starting: "starting",
  ok: "healthy",
  error: "failing",
  paused: "paused",
  push: "receives pushes",
};
export const stateOf = (s: Source) => s.health?.status ?? "starting";
export const stateWords = (st: string) => STATE_WORDS[st] ?? st;

// A connection can be in several projects: `projects` lists them all, older ones only name an owner.
export const memberOf = (s: Source) => s.projects ?? (s.owned_by ? [s.owned_by] : []);
export const kindOf = (specs: Record<string, ConnectorSpec> | undefined, id: string) => specs?.[id]?.label ?? id;
