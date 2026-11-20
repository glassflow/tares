// The shell's one read of GET /api/brain, shared with the Ask page (TR-1206): the shell needs `configured` for
// the Second Brain menu group, the page needs the repo rows, and a screen loads from one read. `reload` reads
// again (the page's Retry and Check again).

import { createContext, useContext } from "react";

import type { AskBrainData } from "./factory/BrainAsk.api";

export type BrainRead =
  | { state: "loading" }
  /** `last` is what the read before it gave, when this one was a quiet read again (the Repos page's poll). */
  | { state: "error"; last?: AskBrainData }
  | { state: "ok"; data: AskBrainData };

export interface BrainReadValue {
  read: BrainRead;
  /** Reads again. A quiet read keeps what is shown while it runs, and keeps it when it fails. */
  reload: (quiet?: boolean) => void;
}

export const BrainReadContext = createContext<BrainReadValue>({ read: { state: "loading" }, reload: () => undefined });

/** The answer of GET /api/brain as the pages may trust it (TR-1438): null for a body that is not an object
 *  (an empty answer is a failed read), `configured` only when it says true, and `repos` the rows that name a
 *  repo, [] when the list is missing or not a list. */
export function normalizeBrain(raw: unknown): AskBrainData | null {
  if (raw == null || typeof raw !== "object" || Array.isArray(raw)) return null;
  const b = raw as { configured?: unknown; repos?: unknown };
  const repos = Array.isArray(b.repos)
    ? b.repos.filter((r): r is AskBrainData["repos"][number] => r != null && typeof r === "object" && typeof (r as { repo?: unknown }).repo === "string")
    : [];
  return { ...(b as object), configured: b.configured === true, repos } as AskBrainData;
}

export const useBrainRead = (): BrainReadValue => useContext(BrainReadContext);
