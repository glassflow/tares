import * as React from "react";
import { Navigate, useParams } from "react-router-dom";

import { SkeletonRows } from "../components/edition";
import { useEdition } from "./edition";

// The route table of the factory and sre editions: every page either edition will have. A screen
// ticket gives its entry a lazy `page`; until then the entry's `fallback` decides what the address
// shows: today's page ("today" is the `today` prop of ByEdition) or a redirect. The menu lists an
// entry only once its route has a page (see hasPage and visibleEntries).
//
// Each ticket changes only its own entry. Keep one entry per line with a blank line between them,
// so two tickets never edit the same or neighbouring lines. In a redirect, `:id`, `:name` and
// `:ref` take the values of the address. `any` holds the pages every edition has, platform included.

type Page = React.LazyExoticComponent<React.ComponentType> | null;
type Fallback = "today" | { redirect: string };
type Entry = { page: Page; fallback: Fallback };

export const EDITION_ROUTES: Record<"factory" | "sre" | "any", Record<string, Entry>> = {
  factory: {
    // TR-730 sample page, TR-732 live page
    "/": { page: React.lazy(() => import("./factory/FactoryHome")), fallback: "today" },

    // TR-733
    "/projects": { page: React.lazy(() => import("./factory/FactoryProjects")), fallback: "today" },

    // TR-734
    "/projects/:id": { page: React.lazy(() => import("./factory/ProjectRoute")), fallback: "today" },

    // TR-760 (an old ?view=ticket:<ref> address lands here, see ProjectRoute)
    "/projects/:id/tickets/:ref": { page: React.lazy(() => import("./factory/FactoryTicket")), fallback: { redirect: "/projects/:id?view=ticket::ref" } },

    // TR-806
    "/tickets": { page: React.lazy(() => import("./factory/FactoryTickets")), fallback: { redirect: "/projects" } },

    // TR-735
    "/reviews": { page: React.lazy(() => import("./factory/FactoryReviews")), fallback: { redirect: "/crew" } },

    // TR-736
    "/crew": { page: React.lazy(() => import("./factory/FactoryCrew")), fallback: "today" },

    // TR-737
    "/docs": { page: React.lazy(() => import("./factory/FactoryDocs")), fallback: { redirect: "/projects" } },

    // TR-738
    "/settings": { page: React.lazy(() => import("./factory/FactorySettings")), fallback: "today" },

    // TR-732: the factory home is /, so Start (the login landing) goes there
    "/start": { page: null, fallback: { redirect: "/" } },
  },
  sre: {
    // TR-749
    "/": { page: React.lazy(() => import("./sre/Home")), fallback: "today" },

    // TR-750
    "/services": { page: React.lazy(() => import("./sre/Services")), fallback: { redirect: "/" } },

    // TR-750
    "/services/:name": { page: React.lazy(() => import("./sre/Service")), fallback: { redirect: "/" } },

    // TR-751
    "/incidents": { page: React.lazy(() => import("./sre/Incidents")), fallback: { redirect: "/" } },

    // TR-751
    "/incidents/:id": { page: React.lazy(() => import("./sre/Incidents").then((m) => ({ default: m.IncidentPage }))), fallback: { redirect: "/" } },

    // TR-752
    "/timeline": { page: React.lazy(() => import("./sre/Timeline")), fallback: "today" },

    // TR-753
    "/runbooks": { page: React.lazy(() => import("./sre/Runbooks")), fallback: { redirect: "/" } },

    // TR-764
    "/settings": { page: React.lazy(() => import("./sre/Settings")), fallback: "today" },

    // TR-749: the AI SRE home is /, so Start (the login landing) goes there
    "/start": { page: null, fallback: { redirect: "/" } },
  },
  any: {
    // TR-755 sets the chooser page
    "/welcome/edition": { page: null, fallback: { redirect: "/" } },
  },
};

/** The entry of an address for a cell's edition. The `any` group holds for every edition, platform
 *  included; any other address on a platform cell has no entry (the platform console is today's). */
function entryFor(edition: string | undefined, path: string): Entry | undefined {
  const own = edition === "factory" || edition === "sre" ? EDITION_ROUTES[edition][path] : undefined;
  return own ?? EDITION_ROUTES.any[path];
}

/** True when the route has a page for this edition, so its menu entry may show. */
export function hasPage(edition: string | undefined, to: string): boolean {
  return entryFor(edition, to)?.page != null;
}

function fill(to: string, params: Record<string, string | undefined>): string {
  return to.replace(/:(id|name|ref)(?![A-Za-z])/g, (_, k: string) => encodeURIComponent(params[k] ?? ""));
}

/** One route element for both editions. A platform cell, and an address the edition has no entry
 *  for, get `today`. An edition with a page for the address gets the page; with none yet, `today`
 *  or the entry's redirect. */
export function ByEdition({ path, today }: { path: string; today: React.ReactNode }) {
  const { edition } = useEdition();
  const params = useParams();
  const entry = entryFor(edition, path);
  if (!entry) return <>{today}</>;
  if (entry.page) {
    const Page = entry.page;
    return (
      <React.Suspense fallback={<SkeletonRows />}>
        <Page />
      </React.Suspense>
    );
  }
  if (entry.fallback === "today") return <>{today}</>;
  return <Navigate to={fill(entry.fallback.redirect, params)} replace />;
}
