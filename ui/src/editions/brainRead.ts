// The shell's one read of GET /api/brain, shared with the Ask page (TR-1206): the shell needs `configured` for
// the Second Brain menu group, the page needs the repo rows, and a screen loads from one read. `reload` reads
// again (the page's Retry and Check again).

import { createContext, useContext } from "react";

import type { AskBrainData } from "./factory/BrainAsk.api";

export type BrainRead =
  | { state: "loading" }
  | { state: "error" }
  | { state: "ok"; data: AskBrainData };

export interface BrainReadValue {
  read: BrainRead;
  reload: () => void;
}

export const BrainReadContext = createContext<BrainReadValue>({ read: { state: "loading" }, reload: () => undefined });

export const useBrainRead = (): BrainReadValue => useContext(BrainReadContext);
