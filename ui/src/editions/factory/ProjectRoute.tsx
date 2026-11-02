import * as React from "react";
import { useParams, useSearchParams } from "react-router-dom";

import ProjectDetail from "../../pages/ProjectDetail";
import FactoryProject from "./FactoryProject";

// The route behind /projects/:id on a factory cell. The factory project page answers for a factory
// project that is past its setup. Today's project page answers for the rest: any address with a
// ?view= (an old ticket or doc link), and a project the factory read does not answer for (another
// kind of project, a draft, an address with no project: ProjectDetail has the words for those).
export default function ProjectRoute() {
  const { id = "" } = useParams();
  const [query] = useSearchParams();
  const [other, setOther] = React.useState("");
  if (query.has("view") || other === id) return <ProjectDetail />; // words-ok
  return <FactoryProject key={id} onOther={() => setOther(id)} />;
}
