import { request } from "../api";

// The Start checklist of an edition home (GET /api/home carries it as `start`, tares/start_steps.py).
// Every string is worded for people and shown as it is.

/** A button of a step: a link inside the console (`to`), a link out (`href`), a command to copy
 *  (`copy`) or something the page itself does (`do`, such as `start_demo`; the page passes the handler). */
export type StartAction =
  | { label: string; to: string }
  | { label: string; href: string }
  | { label: string; copy: string }
  | { label: string; do: string };

export interface StartStep {
  key: string;
  title: string;
  /** What the person gains from this step. */
  gets: string;
  done: boolean;
  /** Empty once the step is done. */
  actions: StartAction[];
  detail?: string;
}

export interface StartBlock {
  steps: StartStep[];
  done: number;
  total: number;
  /** False once everything is done or this person hid the list. */
  show: boolean;
}

/** Hide the list for the caller. The daemon answers with the caller's new block. */
export function hideStart(): Promise<StartBlock> {
  return request<StartBlock>("/api/home/start/hide", { method: "POST" });
}

/** Show the list again for the caller. */
export function showStart(): Promise<StartBlock> {
  return request<StartBlock>("/api/home/start/hide", { method: "DELETE" });
}
