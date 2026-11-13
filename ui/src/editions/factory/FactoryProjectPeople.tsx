import * as React from "react";

import { People } from "./FactoryProject";
import { PageHead, ProjectShell } from "./ProjectShell";
import { ProjectSecrets } from "./ProjectSecrets";

// /projects/:id/people (TR-1245): who is on the project and the secrets its crew may use, side by
// side. #people and #secrets still land on each part.

export default function FactoryProjectPeople() {
  return (
    <ProjectShell page="people">
      {({ d, act, sample }) => (
        <div className="grid min-w-0 gap-5" data-testid="people-page">
          <PageHead title="People and access" sub="Who leads the project, who works on it, and the secrets its crew may use." />
          <div className="grid items-start gap-6 xl:grid-cols-2">
            {d.people ? <People people={d.people} onLead={sample ? undefined : act.lead} onAdd={sample ? undefined : act.add} />
              : <div className="border border-border bg-card px-4 py-5 text-muted-foreground" data-testid="people-none">Only the people on this project see who is on it.</div>}
            {!sample && d.secrets && <ProjectSecrets project={d.id} initial={d.secrets} />}
          </div>
        </div>
      )}
    </ProjectShell>
  );
}
