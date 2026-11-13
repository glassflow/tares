import * as React from "react";

import { Tickets } from "./FactoryTickets";
import { PageHead, ProjectShell } from "./ProjectShell";

// /projects/:id/tickets (TR-1245): the Tickets screen kept to this project: its list, board and
// swimlanes, with every filter but the project. The project's frame reads the project and the
// screen reads the tickets (GET /api/factory/tickets?project=<id>), each once.

export default function FactoryProjectTickets() {
  return (
    <ProjectShell page="tickets">
      {({ id }) => (
        <div className="grid min-w-0 gap-5" data-testid="project-tickets-page">
          <PageHead title="Tickets" />
          <Tickets scope={id} />
        </div>
      )}
    </ProjectShell>
  );
}
