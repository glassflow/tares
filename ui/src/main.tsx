import React, { lazy, Suspense } from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, Navigate, RouterProvider, useLocation, type RouteObject } from "react-router-dom";

import App from "./App";
import AuthGate from "./components/AuthGate";
import { SkeletonRows } from "./components/edition";
import FeedRedirect from "./editions/FeedRedirect";
import NotFound from "./editions/NotFound";
import { PAGES } from "./editions/pages";
import StartRedirect from "./editions/StartRedirect";
import { WhoProvider } from "./editions/who";
import { EeLoader } from "./eeSlots";
import { LicenceProvider } from "./licence";
import "./styles.css";

// The component gallery loads on demand, so it stays out of the main chunk. No menu links to it.
const DevComponents = lazy(() => import("./pages/dev/Components"));

/** Settings, Advanced was a page of its own; Settings has its items now, so the address opens one. */
function AdvancedSettings() {
  const { search, hash } = useLocation();
  return <Navigate to={`/settings${search || "?tab=advanced"}${hash}`} replace />;
}

/** /usecases* was the name of /projects* before 1.14; bookmarks and old links still land. */
function UsecasesRedirect() {
  const rest = window.location.pathname.replace(/^\/usecases/, "");
  return <Navigate to={`/projects${rest}${window.location.search}`} replace />;
}

const to = (path: string) => <Navigate to={path} replace />;

// Every page of the console (editions/pages.ts), each behind a skeleton while its code loads.
const pageRoutes: RouteObject[] = Object.entries(PAGES).map(([path, Page]) => ({
  ...(path === "/" ? { index: true } : { path: path.slice(1) }),
  element: (
    <Suspense fallback={<SkeletonRows />}>
      <Page />
    </Suspense>
  ),
}));

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      // `/` is Home; the cloud login handoff (TARES_LOGIN_URL) lands there
      ...pageRoutes,
      // TR-1014: the template gallery and the by-hand setup are gone; a project starts as a spec
      { path: "projects/new/templates", element: to("/projects/new") },
      { path: "projects/new/custom", element: to("/projects/new") },
      { path: "projects/new/shared_code_context", element: to("/projects/new") },
      { path: "projects/new/assist", element: to("/projects/new") },
      { path: "projects/new/:template", element: to("/projects/new") },
      // TR-1723: Start (the login landing) is Home; it keeps its query and hash (/start#settings)
      { path: "start", element: <StartRedirect /> },
      // a link to /feed?item=<id> is in Slack messages already sent: it keeps the query, for good (TR-1527)
      { path: "feed", element: <FeedRedirect /> },
      { path: "settings/advanced", element: <AdvancedSettings /> },
      { path: "security", element: to("/settings") },
      { path: "welcome/edition", element: to("/") },
      // Slack messages already sent link /agents/<name> and /dispatches/<id>: they land on a page that exists
      { path: "agents/*", element: to("/projects") },
      { path: "dispatches/*", element: to("/") },
      { path: "usecases/*", element: <UsecasesRedirect /> },
      { path: "usecases", element: <UsecasesRedirect /> },
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
      <WhoProvider>
        <LicenceProvider>
          <EeLoader />
          {/* Not v7_startTransition: it makes every router update a transition, a tab's search param too, so
              the page lags its own address by a render, and a check that reads it after a click sees the old
              one. The menu preloads a page's code on hover and focus instead, and the Suspense fallback waits
              200ms (TR-1433). */}
          <RouterProvider router={router} />
        </LicenceProvider>
      </WhoProvider>
    </AuthGate>
  </React.StrictMode>,
);
