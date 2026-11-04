import { request } from "../../api";
import type { HomeData } from "./home.types";

export { hideStart, showStart } from "../StartChecklist.api";

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

/** Mark one call the crew made as kept. */
export function keepAssumption(project: string, ref: string): Promise<unknown> {
  return request(`/api/projects/${enc(project)}/assumptions/${enc(ref)}/state`, {
    method: "POST",
    body: JSON.stringify({ state: "kept", words: "Kept from the home page" }),
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

/** The person answers a crew question from the console. `standing` keeps the answer as a grant. */
export function deskReply(ref: string, words: string, standing: boolean, choice?: string): Promise<unknown> {
  return request(`/api/desk/${enc(ref)}/reply`, {
    method: "POST",
    body: JSON.stringify(choice ? { words, choice, standing } : { words, standing }),
  });
}

/** The person answers a question a project's spec asked them. */
export function answerQuestion(project: string, question: string, answer: string): Promise<unknown> {
  return request(`/api/projects/${enc(project)}/questions/${enc(question)}/answer`, {
    method: "POST",
    body: JSON.stringify({ answer }),
  });
}

/** The owner of a repo says the project's tickets in it are right. */
export function signOff(project: string, repo: string): Promise<unknown> {
  return request(`/api/projects/${enc(project)}/signoffs`, {
    method: "POST",
    body: JSON.stringify({ repo }),
  });
}

/** Agree to a grant that waits for the person. */
export function consentGrant(grant: string): Promise<unknown> {
  return request(`/api/grants/${enc(grant)}/consent`, { method: "POST", body: JSON.stringify({}) });
}

/** Decline a grant that waits for the person, with a reason when they give one. */
export function declineGrant(grant: string, reason: string): Promise<unknown> {
  return request(`/api/grants/${enc(grant)}/decline`, {
    method: "POST",
    body: JSON.stringify(reason ? { reason } : {}),
  });
}

/** The project's lead hands it to a crew although a repo's owner has not signed off, and says why.
 *  The daemon refuses with a sentence when the person is not the lead (403) or the reason is empty (409). */
export function handOverAnyway(project: string, reason: string): Promise<unknown> {
  return request(`/api/projects/${enc(project)}/handover`, {
    method: "POST",
    body: JSON.stringify({ override_reason: reason }),
  });
}
