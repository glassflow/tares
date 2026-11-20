import { request } from "../../api";
import type { HomeAnswered, HomeData, HomeVia } from "./home.types";


// The calls of the live factory home. Edition code stays here, so nothing is added to the shared
// api.ts. Each path and body is the one the daemon reads (tares/factory_api.py, tares/daemon.py).

/** What the daemon answers when a permission is allowed. */
export interface AllowedGrant {
  station: string;
  line: string;
  added: boolean;
  scope: "project" | "all";
}

const enc = encodeURIComponent;

/** The whole home page in one read. A team cell lists `projects` for the view asked (`mine` unless told). */
export function getHome(projects?: "mine" | "team"): Promise<HomeData> {
  return request<HomeData>(projects ? `/api/home?projects=${projects}` : "/api/home");
}

/** One row of GET /api/answers (factory_api.answers_list); only what Home reads. */
interface AnswerRow {
  id: string;
  ask: string;
  choice: "yes" | "no" | null;
  said: string;
  by: string | null;
  mine: boolean;
  at: string;
  via: HomeVia;
}

const toAnswered = (a: AnswerRow): HomeAnswered => ({
  id: a.id, ask: a.ask, choice: a.choice, said: a.said,
  by: a.mine ? "you" : a.by ?? "someone", at: a.at, via: a.via,
});

/** The latest answer to one item, of any day, or null (TR-1459): a link to an item opened after it was
 *  answered says who answered it. A desk item is found under any of its ids. */
export async function getAnswerTo(item: string): Promise<HomeAnswered | null> {
  const { answers } = await request<{ answers: AnswerRow[] }>(`/api/answers?item=${encodeURIComponent(item)}`);
  return answers.length ? toAnswered(answers[0]) : null;
}

/** Mark one call the crew made as kept. `item` is the Home item that offered it, so the answer is
 *  recorded under the id the feed and Home showed (a project with calls for two owners has two). */
export function keepAssumption(project: string, ref: string, item?: string): Promise<unknown> {
  return request(`/api/projects/${enc(project)}/assumptions/${enc(ref)}/state`, {
    method: "POST",
    body: JSON.stringify({ state: "kept", words: "Kept from the home page", ...(item ? { item } : {}) }),
  });
}

/** Allow a permission request, kept as a grant in these words, for this project or every project. */
export function allowDesk(ref: string, words: string, scope: "project" | "all"): Promise<AllowedGrant> {
  return request<AllowedGrant>(`/api/desk/${enc(ref)}/allow`, {
    method: "POST",
    body: JSON.stringify({ words, scope }),
  });
}

/** Close a project whose tickets are all finished. The daemon refuses with a sentence when one is not. */
export function finishProject(id: string): Promise<unknown> {
  return request(`/api/projects/${enc(id)}/finish`, {
    method: "POST",
    body: JSON.stringify({ words: "Closed from the home page" }),
  });
}

/** The person answers a crew question from the console. `standing` keeps the answer as a grant. `own`
 *  says the choice is the person's own words, typed: the desk keeps them unless they are an option's exactly. */
export function deskReply(ref: string, words: string, standing: boolean, choice?: string, kept?: string[], own?: boolean): Promise<unknown> {
  return request(`/api/desk/${enc(ref)}/reply`, {
    method: "POST",
    body: JSON.stringify({ words, standing, ...(choice ? { choice } : {}), ...(kept ? { kept } : {}), ...(own ? { own } : {}) }),
  });
}

/** Keep a project for later, off the home until someone starts it, or start it again. */
/** Approve the merge the merge queue asks the person for (TR-1450). */
export function approveMerge(queue: string): Promise<unknown> {
  return request(`/api/factory/merge-queue/${enc(queue)}/approve`, { method: "POST" });
}

/** Accept a milestone as it is, every ticket merged (TR-1450). */
export function acceptMilestone(project: string, milestone: string): Promise<unknown> {
  return request(`/api/projects/${enc(project)}/milestones/${enc(milestone)}/accept`, {
    method: "POST",
    body: JSON.stringify({}),
  });
}

export function keepForLater(project: string, later = true): Promise<unknown> {
  return request(`/api/projects/${enc(project)}/later`, {
    method: "POST",
    body: JSON.stringify({ later }),
  });
}

/** How the crew works (Set up your factory): the crew's three choices, saved together when the person confirms. */
export function saveCrewChoices(c: { autonomy: string; builders_max: number; releaser: boolean }): Promise<unknown> {
  return request("/api/crew/settings", { method: "PUT", body: JSON.stringify(c) });
}
