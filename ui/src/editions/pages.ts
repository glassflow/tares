import * as React from "react";

// The factory console's pages, each loaded on demand. main.tsx builds the router from them, and the
// menu starts loading a page's code while a pointer rests on its link (TR-1433).

export type Lazy = React.LazyExoticComponent<React.ComponentType> & { preload: () => void };

/** React.lazy that can also be asked to start loading early. A failed early load is forgotten, and the
 *  click tries again. */
function lazyPage(load: () => Promise<{ default: React.ComponentType }>): Lazy {
  let started: Promise<{ default: React.ComponentType }> | null = null;
  const once = () => (started ??= load().catch((e) => { started = null; throw e; }));
  const page = React.lazy(once) as Lazy;
  page.preload = () => { once().catch(() => {}); };
  return page;
}

/** Address (as the router writes it) to page. */
export const PAGES: Record<string, Lazy> = {
  "/": lazyPage(() => import("./factory/FactoryHome")),
  "/projects": lazyPage(() => import("./factory/FactoryProjects")),
  // TR-1014: a factory project starts as a spec in Claude Code
  "/projects/new": lazyPage(() => import("./factory/FactoryNewProject")),
  "/projects/:id": lazyPage(() => import("./factory/ProjectRoute")),
  // TR-760 (an old ?view=ticket:<ref> address lands here, see ProjectRoute)
  "/projects/:id/tickets/:ref": lazyPage(() => import("./factory/FactoryTicket")),
  "/projects/:id/decisions": lazyPage(() => import("./factory/FactoryDecisions")),
  "/projects/:id/calls": lazyPage(() => import("./factory/FactoryCalls")),
  "/projects/:id/github": lazyPage(() => import("./factory/FactoryProjectGitHub")),
  "/projects/:id/docs": lazyPage(() => import("./factory/FactoryProjectDocs")),
  // TR-1245: the other pages of a project, in its frame
  "/projects/:id/tickets": lazyPage(() => import("./factory/FactoryProjectTickets")),
  "/projects/:id/milestones": lazyPage(() => import("./factory/FactoryProjectMilestones")),
  "/projects/:id/people": lazyPage(() => import("./factory/FactoryProjectPeople")),
  "/projects/:id/settings": lazyPage(() => import("./factory/FactoryProjectSettings")),
  "/tickets": lazyPage(() => import("./factory/FactoryTickets")),
  "/reviews": lazyPage(() => import("./factory/FactoryReviews")),
  "/report": lazyPage(() => import("./factory/FactoryReport")),
  "/crew": lazyPage(() => import("./factory/FactoryCrew")),
  "/docs": lazyPage(() => import("./factory/FactoryDocs")),
  "/settings": lazyPage(() => import("./factory/FactorySettings")),
  "/people": lazyPage(() => import("./factory/FactoryPeople")),
  "/integrations": lazyPage(() => import("./factory/FactoryIntegrations")),
  "/setup": lazyPage(() => import("./factory/FactorySetup")),
  "/brain": lazyPage(() => import("./factory/BrainRepos")),
  "/brain/decisions": lazyPage(() => import("./factory/BrainDecisions")),
  "/brain/decisions/new": lazyPage(() => import("./factory/BrainDecisionNew")),
  "/brain/ask": lazyPage(() => import("./factory/BrainAsk")),
  "/brain/connect": lazyPage(() => import("./factory/BrainConnect")),
};

/** Start loading the page of a menu address before it is opened. Nothing when it has none. */
export function preloadPage(to: string): void {
  PAGES[to]?.preload();
}
