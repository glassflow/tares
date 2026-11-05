import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";

import App from "./App";
import AuthGate from "./components/AuthGate";
import { EditionProvider, useEdition } from "./editions/edition";
import { ByEdition } from "./editions/routes";
import AgentActivity, { ConnectPage } from "./pages/Activity";
import Ask from "./pages/Ask";
import CatalogExport from "./pages/CatalogExport";
import CatalogImport from "./pages/CatalogImport";
import Explore from "./pages/Explore";
import SourceClaudeCode from "./pages/SourceClaudeCode";
import SourceDiscover from "./pages/SourceDiscover";
import SourceNew from "./pages/SourceNew";
import Security from "./pages/Security";
import Home from "./pages/Home";
import Start from "./pages/Start";
import Sources from "./pages/Sources";
import Projects from "./pages/Projects";
import Crew from "./pages/Crew";
import ProjectTemplates from "./pages/ProjectNew";
import ProjectDetail from "./pages/ProjectDetail";
import { AgentRedirect, DispatchRedirect, FiringsRedirect, NewInProjectRedirect, NotFound, SourceByName, TriggerRedirect } from "./pages/Redirects";
import ProjectSetup from "./pages/ProjectSetup";
import Resources from "./pages/Resources";
import ProjectNewCustom from "./pages/ProjectNewCustom";
import ProjectNewGeneric from "./pages/ProjectNewGeneric";
import ProjectNewSharedContext from "./pages/ProjectNewSharedContext";
import "./styles.css";

// The component gallery loads on demand, so it stays out of the main chunk. No menu links to it.
const DevComponents = lazy(() => import("./pages/dev/Components"));

/** /activity?tab=dispatches (and the bare page, whose default tab was dispatches) → Deliveries;
 *  /activity?tab=queries → Reads; ?agent=… → the subscriber roster on Deliveries. */
function ActivityRedirect() {
  const q = new URLSearchParams(window.location.search);
  if (q.get("tab") === "queries") return <Navigate to="/reads" replace />;
  const agent = q.get("agent");
  return <Navigate to={agent ? `/agents?agent=${encodeURIComponent(agent)}` : "/firings"} replace />;
}

/** /usecases* was the name of /projects* before 1.14; bookmarks and old links still land. */
function UsecasesRedirect() {
  const rest = window.location.pathname.replace(/^\/usecases/, "");
  return <Navigate to={`/projects${rest}${window.location.search}`} replace />;
}

/** Settings, Advanced: today's Settings page with its Advanced tab on the factory and sre editions.
 *  A platform cell has no such tab, so the address lands on Settings. */
function AdvancedSettings() {
  const { edition } = useEdition();
  if (edition === "platform") return <Navigate to="/settings" replace />;
  return <Security initialTab="advanced" />;
}

/** /timeline has a page only on an AI SRE cell; any other cell lands on Home. */
function TimelineToday() {
  return <Navigate to="/" replace />;
}

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      // `/` opens Start on a cell with no project of the person's own, else Projects. The cloud
      // login handoff (TARES_LOGIN_URL) lands here.
      { index: true, element: <ByEdition path="/" today={<Home />} /> },
      { path: "start", element: <ByEdition path="/start" today={<Start />} /> },
      { path: "projects", element: <ByEdition path="/projects" today={<Projects />} /> },
      { path: "crew", element: <ByEdition path="/crew" today={<Crew />} /> },
      // Create new is the goal-first setup: goal, plan, connect, try it. The template gallery and
      // the by-hand path are links from its first step.
      // A factory cell has its own New project (a spec in Claude Code) and none of these (TR-1014).
      { path: "projects/new", element: <ByEdition path="/projects/new" today={<ProjectSetup />} /> },
      { path: "projects/new/templates", element: <ByEdition path="/projects/new/templates" today={<ProjectTemplates />} /> },
      { path: "projects/new/shared_code_context", element: <ByEdition path="/projects/new/shared_code_context" today={<ProjectNewSharedContext />} /> },
      { path: "projects/new/custom", element: <ByEdition path="/projects/new/custom" today={<ProjectNewCustom />} /> },
      // the AI-guided builder's old address; the goal-first setup replaced it. Above the
      // :template fallback, which would read "assist" as a template key.
      { path: "projects/new/assist", element: <Navigate to="/projects/new" replace /> },
      { path: "projects/new/:template", element: <ByEdition path="/projects/new/:template" today={<ProjectNewGeneric />} /> },
      { path: "projects/:id", element: <ByEdition path="/projects/:id" today={<ProjectDetail />} /> },
      // a ticket's own page (edition cells); a platform cell has no such address
      { path: "projects/:id/tickets/:ref", element: <ByEdition path="/projects/:id/tickets/:ref" today={<NotFound />} /> },
      // a project set up goal first, resumed at the step it stored
      { path: "projects/:id/setup", element: <ProjectSetup /> },
      // a project's decisions on their own page (edition cells)
      { path: "projects/:id/decisions", element: <ByEdition path="/projects/:id/decisions" today={<NotFound />} /> },
      { path: "projects/:id/calls", element: <ByEdition path="/projects/:id/calls" today={<NotFound />} /> },
      { path: "projects/:id/docs", element: <ByEdition path="/projects/:id/docs" today={<NotFound />} /> },
      { path: "sources", element: <Sources /> },
      { path: "sources/discover", element: <SourceDiscover /> },
      { path: "sources/claude-code", element: <SourceClaudeCode /> },
      { path: "sources/new", element: <SourceNew /> },
      { path: "sources/export", element: <CatalogExport /> },
      { path: "sources/import", element: <CatalogImport /> },
      { path: "sources/:name", element: <SourceByName /> },
      { path: "organize", element: <Navigate to="/ask" replace /> },
      { path: "explore", element: <Explore /> },
      { path: "resources", element: <Resources /> },
      { path: "resources/:kind", element: <Resources /> },
      // triggers, agents, firings and MCP servers live in their project now (Advanced setup);
      // the old addresses land on the same thing there (Slack links /agents and /dispatches)
      { path: "triggers", element: <Navigate to="/projects" replace /> },
      { path: "triggers/new", element: <NewInProjectRedirect kind="triggers" /> },
      { path: "triggers/:name", element: <TriggerRedirect /> },
      { path: "connect", element: <ConnectPage /> },
      { path: "reads", element: <AgentActivity /> },
      { path: "firings", element: <FiringsRedirect /> },
      // /deliveries was this page's name before the firing/delivery/dispatch words settled
      { path: "deliveries", element: <Navigate to="/firings" replace /> },
      // TR-137 renames: /activity split into /reads + /deliveries (dispatches live with their
      // subscribers now); /security is /settings.
      { path: "activity", element: <ActivityRedirect /> },
      { path: "security", element: <Navigate to="/settings" replace /> },
      { path: "dispatches/:id", element: <DispatchRedirect /> },
      { path: "agents", element: <Navigate to="/projects" replace /> },
      { path: "mcp-servers", element: <Navigate to="/projects" replace /> },
      { path: "agents/new", element: <NewInProjectRedirect kind="agents" /> },
      { path: "agents/:name", element: <AgentRedirect /> },
      { path: "ask", element: <Ask /> },
      { path: "settings", element: <ByEdition path="/settings" today={<Security />} /> },
      { path: "settings/advanced", element: <AdvancedSettings /> },
      // pages only the factory or sre edition has; a cell of the other edition lands on Home
      { path: "tickets", element: <ByEdition path="/tickets" today={<Navigate to="/" replace />} /> },
      { path: "reviews", element: <ByEdition path="/reviews" today={<Navigate to="/" replace />} /> },
      { path: "docs", element: <ByEdition path="/docs" today={<Navigate to="/" replace />} /> },
      { path: "people", element: <ByEdition path="/people" today={<Navigate to="/" replace />} /> },
      { path: "services", element: <ByEdition path="/services" today={<Navigate to="/" replace />} /> },
      { path: "services/:name", element: <ByEdition path="/services/:name" today={<Navigate to="/" replace />} /> },
      { path: "incidents", element: <ByEdition path="/incidents" today={<Navigate to="/" replace />} /> },
      { path: "incidents/:id", element: <ByEdition path="/incidents/:id" today={<Navigate to="/" replace />} /> },
      { path: "timeline", element: <ByEdition path="/timeline" today={<TimelineToday />} /> },
      { path: "runbooks", element: <ByEdition path="/runbooks" today={<Navigate to="/" replace />} /> },
      { path: "welcome/edition", element: <ByEdition path="/welcome/edition" today={<Navigate to="/" replace />} /> },
      // legacy paths → new homes (bookmarks, the old Entities/Activity/Catalog nav). Catalog
      // dissolved: source schema/freshness now lives on the source detail; the agent's-eye read
      // is Explore's "What the agent gets" toggle.
      { path: "entities", element: <Navigate to="/explore" replace /> },
      // These two meant "the source list" when `/` was the source list — they still do.
      { path: "catalog", element: <Navigate to="/sources" replace /> },
      { path: "usecases/*", element: <UsecasesRedirect /> },
      { path: "usecases", element: <UsecasesRedirect /> },
      // views are gone: a project holds what a view used to
      { path: "views", element: <Navigate to="/projects" replace /> },
      { path: "views/*", element: <Navigate to="/projects" replace /> },
      { path: "*", element: <NotFound /> },
    ],
  },
  // The gallery sits outside the console shell, so it has the whole width at any screen size.
  {
    path: "/dev/components",
    element: (
      <Suspense fallback={null}>
        <DevComponents />
      </Suspense>
    ),
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthGate>
      <EditionProvider>
        <RouterProvider router={router} />
      </EditionProvider>
    </AuthGate>
  </React.StrictMode>,
);
