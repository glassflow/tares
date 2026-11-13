import * as React from "react";

import { MilestoneRow } from "./FactoryProject";
import { PageHead, ProjectShell } from "./ProjectShell";

// /projects/:id/milestones (TR-1245): each milestone of the project with its checks, what holds it
// up, and Accept when its tickets have merged. The project's frame reads the project; nothing else.

export default function FactoryProjectMilestones() {
  return (
    <ProjectShell page="milestones">
      {({ d, act, sample }) => (
        <section aria-label="Milestones" className="grid min-w-0 gap-5" data-testid="milestones">
          <PageHead title="Milestones" sub="Each milestone with its checks. Try the checks that are for you, or accept a milestone once its tickets have merged." />
          {d.milestones.length === 0 ? (
            <div className="border border-border bg-card px-4 py-5 text-muted-foreground" data-testid="milestones-empty">
              No milestones yet. The crew adds them when it plans the build.
            </div>
          ) : (
            <div className="border border-border bg-card">
              {d.milestones.map((m) => <MilestoneRow key={m.id} m={m} onAnswer={sample ? undefined : act.answer} onAccept={sample ? undefined : act.accept} />)}
            </div>
          )}
        </section>
      )}
    </ProjectShell>
  );
}
