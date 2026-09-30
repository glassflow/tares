import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router-dom";

import App from "./App";
import AuthGate from "./components/AuthGate";
import AgentActivity, { ConnectPage } from "./pages/Activity";
import Ask from "./pages/Ask";
import CatalogExport from "./pages/CatalogExport";
import CatalogImport from "./pages/CatalogImport";
import Explore from "./pages/Explore";
import SourceClaudeCode from "./pages/SourceClaudeCode";
import SourceDetail from "./pages/SourceDetail";
import SourceDiscover from "./pages/SourceDiscover";
import SourceNew from "./pages/SourceNew";
import Security from "./pages/Security";
import Home from "./pages/Home";
import Sources from "./pages/Sources";
import Projects from "./pages/Projects";
import ProjectTemplates from "./pages/ProjectNew";
import ProjectDetail from "./pages/ProjectDetail";
import { AgentRedirect, DispatchRedirect, FiringsRedirect, NewInProjectRedirect, TriggerRedirect } from "./pages/Redirects";
import ProjectSetup from "./pages/ProjectSetup";
import ProjectNewCustom from "./pages/ProjectNewCustom";
import ProjectNewGeneric from "./pages/ProjectNewGeneric";
import ProjectNewSharedContext from "./pages/ProjectNewSharedContext";
import "./styles.css";

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

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      // `/` is Overview, not the source list. The cloud login handoff (TARES_LOGIN_URL) lands
      // here, so a customer arrives at the instance at a glance rather than at a table.
      { index: true, element: <Home /> },
      { path: "projects", element: <Projects /> },
      // Create new is the goal-first setup: goal, plan, connect, try it. The template gallery and
      // the by-hand path are links from its first step.
      { path: "projects/new", element: <ProjectSetup /> },
      { path: "projects/new/templates", element: <ProjectTemplates /> },
      { path: "projects/new/shared_code_context", element: <ProjectNewSharedContext /> },
      { path: "projects/new/custom", element: <ProjectNewCustom /> },
      // the AI-guided builder's old address; the goal-first setup replaced it. Above the
      // :template fallback, which would read "assist" as a template key.
      { path: "projects/new/assist", element: <Navigate to="/projects/new" replace /> },
      { path: "projects/new/:template", element: <ProjectNewGeneric /> },
      { path: "projects/:id", element: <ProjectDetail /> },
      // a project set up goal first, resumed at the step it stored
      { path: "projects/:id/setup", element: <ProjectSetup /> },
      { path: "sources", element: <Sources /> },
      { path: "sources/discover", element: <SourceDiscover /> },
      { path: "sources/claude-code", element: <SourceClaudeCode /> },
      { path: "sources/new", element: <SourceNew /> },
      { path: "sources/export", element: <CatalogExport /> },
      { path: "sources/import", element: <CatalogImport /> },
      { path: "sources/:name", element: <SourceDetail /> },
      { path: "organize", element: <Navigate to="/ask" replace /> },
      { path: "explore", element: <Explore /> },
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
      { path: "settings", element: <Security /> },
      // legacy paths → new homes (bookmarks, the old Entities/Activity/Catalog nav). Catalog
      // dissolved: source schema/freshness now lives on the source detail; the agent's-eye read
      // is Explore's "What the agent gets" toggle.
      { path: "entities", element: <Navigate to="/explore" replace /> },
      // These two meant "the source list" when `/` was the source list — they still do.
      { path: "catalog", element: <Navigate to="/sources" replace /> },
      { path: "usecases/*", element: <UsecasesRedirect /> },
      { path: "usecases", element: <UsecasesRedirect /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <AuthGate>
      <RouterProvider router={router} />
    </AuthGate>
  </React.StrictMode>,
);
