import * as React from "react";
import { Navigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import { useSample } from "../sample";
import FactoryProject from "./FactoryProject";
import { factoryProject } from "./FactoryProject.api";
import OtherProject from "./OtherProject";

/** Where an old project address (?view=, from before the factory pages) leads on a factory project.
 *  Every view the old page had lands on the factory page that holds the same thing, or the project's own. */
export function oldViewTarget(id: string, view: string): string {
  const base = `/projects/${encodeURIComponent(id)}`;
  const i = view.indexOf(":");
  const kind = i < 0 ? view : view.slice(0, i);
  const name = i < 0 ? "" : view.slice(i + 1);
  switch (kind) {
    case "ticket": return name ? `${base}/tickets/${encodeURIComponent(name)}` : `${base}/tickets`;
    case "tickets": return `${base}/tickets`;
    case "docs": return `${base}/docs`;
    // the docs page opens its first document when this one is not the project's
    case "doc": return name ? `${base}/docs?doc=${encodeURIComponent(name)}` : `${base}/docs`;
    case "settings": return name === "members" ? `${base}/people` : `${base}/settings`;
    case "grants": return `${base}/settings`;
    default: return base;
  }
}

// An old address (?view=) goes to the factory page that holds the same thing, but only for a project the
// factory pages answer for. For any other (a draft, another kind of project, no project) the cell answers
// the project read with 404 and OtherProject says what is there.
function OldLink({ id, view }: { id: string; view: string }) {
  const sample = useSample();
  const [factory, setFactory] = React.useState<boolean | null>(sample ? true : null);
  React.useEffect(() => {
    if (sample) return;
    let live = true;
    factoryProject(id).then(
      () => live && setFactory(true),
      // only a 404 says "not a factory project"; any other failure leaves the factory page to show it
      (e) => live && setFactory(!(e instanceof ApiError && e.status === 404)),
    );
    return () => { live = false; };
  }, [id, sample]);
  if (factory === null) return <div data-testid="route-probe" aria-busy="true" />;
  if (!factory) return <OtherProject id={id} />;
  return <Navigate replace to={oldViewTarget(id, view)} />;
}

// The route behind /projects/:id on a factory cell. The factory project page answers for a factory
// project that is past its setup, and an old ?view= address goes to its factory page (see OldLink).
// OtherProject answers for the rest: another kind of project, a draft (it sends that to its setup), an
// address with no project.
export default function ProjectRoute() {
  const { id = "" } = useParams();
  const [query] = useSearchParams();
  const [other, setOther] = React.useState("");
  const view = query.get("view") ?? ""; // words-ok
  if (view) return <OldLink key={`${id}?${view}`} id={id} view={view} />;
  if (other === id) return <OtherProject key={id} id={id} />;
  return <FactoryProject key={id} onOther={() => setOther(id)} />;
}
