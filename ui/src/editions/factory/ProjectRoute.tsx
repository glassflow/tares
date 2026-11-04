import * as React from "react";
import { Navigate, useParams, useSearchParams } from "react-router-dom";

import { ApiError } from "../../api";
import ProjectDetail from "../../pages/ProjectDetail";
import { useSample } from "../sample";
import FactoryProject from "./FactoryProject";
import { factoryProject } from "./FactoryProject.api";

// An old ticket link (?view=ticket:<ref>) goes to the ticket page, but only for a project that page
// answers for. For any other (a draft, another kind of project) the cell answers the project read with
// 404 and today's project page keeps the link, as it always has.
function TicketLink({ id, ticket }: { id: string; ticket: string }) {
  const sample = useSample();
  const [forTicketPage, setForTicketPage] = React.useState<boolean | null>(sample ? true : null);
  React.useEffect(() => {
    if (sample) return;
    let live = true;
    factoryProject(id).then(
      () => live && setForTicketPage(true),
      // only a 404 says "not a factory project"; any other failure leaves the ticket page to show it
      (e) => live && setForTicketPage(!(e instanceof ApiError && e.status === 404)),
    );
    return () => { live = false; };
  }, [id, sample]);
  if (forTicketPage === null) return <div data-testid="route-probe" aria-busy="true" />;
  if (!forTicketPage) return <ProjectDetail />;
  return <Navigate replace to={`/projects/${encodeURIComponent(id)}/tickets/${encodeURIComponent(ticket)}`} />;
}

// The route behind /projects/:id on a factory cell. The factory project page answers for a factory
// project that is past its setup. An old ticket link goes to the ticket page (see TicketLink).
// Today's project page answers for the rest: any other address with a ?view= (an old doc link), and
// a project the factory read does not answer for (another kind of project, a draft, an address with
// no project: ProjectDetail has the words for those).
export default function ProjectRoute() {
  const { id = "" } = useParams();
  const [query] = useSearchParams();
  const [other, setOther] = React.useState("");
  const view = query.get("view") || ""; // words-ok
  const ticket = view.startsWith("ticket:") ? view.slice("ticket:".length) : "";
  if (ticket) return <TicketLink key={`${id}/${ticket}`} id={id} ticket={ticket} />;
  if (query.has("view") || other === id) return <ProjectDetail />; // words-ok
  return <FactoryProject key={id} onOther={() => setOther(id)} />;
}
