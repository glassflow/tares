import { useNavigate, useSearchParams } from "react-router-dom";

import ProjectBadge from "../components/ProjectBadge";
import TriggerEditor from "../components/TriggerEditor";

export default function TriggerNew() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  // the project it is made in; without one the daemon uses the Default project
  const project = params.get("project") ?? undefined;

  return (
    <>
      <h1>New trigger</h1>
      <p className="subtitle">
        A condition Tares checks all the time over some of a project's sources. When it is met, the
        agents it wakes get the timeline of what happened.
      </p>
      {project && <p className="help">in <ProjectBadge ownedBy={project} compact /></p>}
      <TriggerEditor project={project}
                     onSaved={(name) => nav(`/triggers/${encodeURIComponent(name)}`)}
                     onCancel={() => nav(project ? `/projects/${encodeURIComponent(project)}` : "/triggers")} />
    </>
  );
}
