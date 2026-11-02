// The body of GET /api/sre/runbooks on an AI SRE cell (tares/screens/sre_runbooks.py), and the two
// writes that answer with the same body. Every string is worded for people and shown as it is. A
// line's `text` is written by a person or by an agent; `by` is a name.

import { request } from "../../api";

/** One line of a runbook (written by people) or of memory (written by the investigator).
 *  `by` and `when_words` are empty for a line someone wrote into the skill by hand without a stamp. */
export interface RunbookLine {
  text: string;
  by: string;
  when_words: string;
}

export interface RunbookService {
  name: string;
  /** "No lines yet", "1 line", "3 lines". The page words the count itself, from `runbook`. */
  lines_words: string;
  runbook: RunbookLine[];
  memory: RunbookLine[];
  /** "Read by the investigator 4 times this week", or null when nothing read it. */
  used_words: string | null;
}

export interface RunbooksData {
  services: RunbookService[];
  /** A sentence when there are no services; null otherwise. */
  empty_words: string | null;
}

const lines = (service: string) => `/api/sre/runbooks/${encodeURIComponent(service)}/lines`;

/** Every service with its runbook and memory, in one read. */
export const sreRunbooks = () => request<RunbooksData>("/api/sre/runbooks");

/** Adds one line to a service's runbook (an admin key). Answers the new screen. A refusal is an
 *  ApiError with a plain sentence. */
export const addRunbookLine = (service: string, text: string) =>
  request<RunbooksData>(lines(service), { method: "POST", body: JSON.stringify({ text }) });

/** Removes the line at `index` (its place in the service's `runbook`). Answers the new screen. */
export const removeRunbookLine = (service: string, index: number) =>
  request<RunbooksData>(`${lines(service)}/${index}`, { method: "DELETE" });
