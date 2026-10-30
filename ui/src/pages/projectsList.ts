import type { Project } from "../types";

export type Show = "active" | "done" | "all";
export type Sort = "recent" | "most" | "least" | "name";

export const SHOWS: Show[] = ["active", "done", "all"];
export const SORTS: { key: Sort; label: string }[] = [
  { key: "recent", label: "Recently active" },
  { key: "most", label: "Most work pending" },
  { key: "least", label: "Least work pending" },
  { key: "name", label: "Name" },
];

/** Tickets not done yet: to do, in progress and blocked. A project without tickets has none to count. */
export function pendingWork(p: Project): number {
  const g = p.progress;
  return g ? g.todo + g.doing + g.blocked : 0;
}

/** When the project last moved: the server's `activity_at` (its newest ticket counts), else its own update. */
export function lastActive(p: Project): number {
  return Date.parse(p.activity_at ?? p.updated_at) || 0;
}

export function matchesShow(p: Project, show: Show): boolean {
  return show === "all" || (show === "done") === (p.status === "done");
}

export function matchesQuery(p: Project, query: string): boolean {
  const q = query.trim().toLowerCase();
  return !q || p.name.toLowerCase().includes(q) || (p.goal ?? "").toLowerCase().includes(q);
}

const hasTickets = (p: Project) => !!p.progress && p.progress.total > 0;

/** Most or least pending work puts projects without tickets last either way: they have no work to rank. */
export function sortProjects(list: Project[], sort: Sort): Project[] {
  const byRecent = (a: Project, b: Project) => lastActive(b) - lastActive(a);
  const byPending = (dir: 1 | -1) => (a: Project, b: Project) => {
    if (hasTickets(a) !== hasTickets(b)) return hasTickets(a) ? -1 : 1;
    return dir * (pendingWork(b) - pendingWork(a)) || byRecent(a, b);
  };
  const order = sort === "name" ? (a: Project, b: Project) => a.name.localeCompare(b.name)
    : sort === "most" ? byPending(1)
    : sort === "least" ? byPending(-1)
    : byRecent;
  return [...list].sort(order);
}
