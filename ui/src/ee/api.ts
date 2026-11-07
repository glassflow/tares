// Tares enterprise licence, see ee/LICENSE
import { ownerName, request } from "../api";
import type { Area, Grant, Project, ProjectScope, SpecQuestion } from "../types";

// The calls the enterprise screens make. They sit here, not in the core api object, so a build
// without src/ee carries none of them.

/** Who leads a project and who can see it. The lead changes both; an admin changes the lead only. */
export const setProjectAccess = (id: string, body: { lead_person_id?: string; scope?: ProjectScope }) =>
  request<Project>(`/api/projects/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) });

/** Members: the lead only. Each answers with the project. */
export const addProjectMember = (id: string, personId: string) =>
  request<Project>(`/api/projects/${encodeURIComponent(id)}/members`,
    { method: "POST", body: JSON.stringify({ person_id: personId }) });

export const removeProjectMember = (id: string, personId: string) =>
  request<Project>(`/api/projects/${encodeURIComponent(id)}/members/${encodeURIComponent(personId)}`,
    { method: "DELETE" });

/** Sets (or changes) the owner of a GitHub repo. */
export const setArea = (repo: string, personId: string, note?: string) =>
  request<Area>(`/api/areas/${ownerName(repo).split("/").map(encodeURIComponent).join("/")}`,
    { method: "PUT", body: JSON.stringify({ person_id: personId, ...(note !== undefined ? { note } : {}) }) });

export const deleteArea = (repo: string) =>
  request<{ ok: boolean }>(`/api/areas/${repo.split("/").map(encodeURIComponent).join("/")}`, { method: "DELETE" });

/** A person (or an admin, for a grant that waits for one) agrees to a grant. */
export const consentGrant = (id: string) =>
  request<{ grant: Grant }>(`/api/grants/${encodeURIComponent(id)}/consent`, { method: "POST" });

/** Questions a spec put to the owners of the repos it changes, and their answers. */
export const projectQuestions = (id: string) =>
  request<{ questions: SpecQuestion[] }>(`/api/projects/${encodeURIComponent(id)}/questions`);
