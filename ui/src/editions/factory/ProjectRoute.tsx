import * as React from "react";
import { Navigate, useParams, useSearchParams } from "react-router-dom";

import ProjectDetail from "../../pages/ProjectDetail";
import FactoryProject from "./FactoryProject";

// The route behind /projects/:id on a factory cell. The factory project page answers for a factory
// project that is past its setup. An old ticket link (?view=ticket:<ref>) goes to the ticket page.
// Today's project page answers for the rest: any other address with a ?view= (an old doc link), and
// a project the factory read does not answer for (another kind of project, a draft, an address with
// no project: ProjectDetail has the words for those).
export default function ProjectRoute() {
  const { id = "" } = useParams();
  const [query] = useSearchParams();
  const [other, setOther] = React.useState("");
  const view = query.get("view") || ""; // words-ok
  const ticket = view.startsWith("ticket:") ? view.slice("ticket:".length) : "";
  if (ticket) return <Navigate replace to={`/projects/${encodeURIComponent(id)}/tickets/${encodeURIComponent(ticket)}`} />;
  if (query.has("view") || other === id) return <ProjectDetail />; // words-ok
  return <FactoryProject key={id} onOther={() => setOther(id)} />;
}
