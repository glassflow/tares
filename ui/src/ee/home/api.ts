// Tares enterprise licence, see ee/LICENSE
import { request } from "@/api";

// The calls of the team actions on the home. Each path and body is the one the daemon reads.

const enc = encodeURIComponent;

/** The person answers a question a project's spec asked them. */
export function answerQuestion(project: string, question: string, answer: string): Promise<unknown> {
  return request(`/api/projects/${enc(project)}/questions/${enc(question)}/answer`, {
    method: "POST",
    body: JSON.stringify({ answer }),
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
