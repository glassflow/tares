import * as React from "react";
import { Link } from "react-router-dom";

import { ApiError } from "../../api";
import { Button, useToast } from "../../components/edition";
import { cn } from "../../lib/utils";
import { DeleteProject } from "./FactoryProject";
import type { FactoryProjectData } from "./FactoryProject.api";
import { ProjectIconHeader } from "./ProjectIcon";
import { PageHead, ProjectShell, type ProjectActions } from "./ProjectShell";

// /projects/:id/settings (TR-1245): the project's icon, its plan, where its tickets live, the repos
// handed over to the crew, keeping it for later, and Delete, last.

const NOT_SAVED = "That was not saved. Try again in a moment.";
const LINK = "text-[color:var(--accent-deep)]! underline!";

function Part({ title, testid, children }: { title: string; testid: string; children: React.ReactNode }) {
  return (
    <section aria-label={title} className="grid min-w-0 gap-1.5 border-b border-border py-4 first:pt-0 md:grid-cols-[200px_minmax(0,1fr)] md:gap-6" data-testid={testid}>
      <h2 className="m-0! text-[15px]! font-semibold!">{title}</h2>
      <div className="grid min-w-0 gap-2 text-[14px]">{children}</div>
    </section>
  );
}

function Icon({ d, act }: { d: FactoryProjectData; act: ProjectActions }) {
  const { toast } = useToast();
  // what the person just picked, shown at once and until the page's data has caught up with it
  const [picked, setPicked] = React.useState<string | null | undefined>(undefined);
  const save = React.useRef(0);
  const icon = picked === undefined ? d.icon ?? null : picked;
  React.useEffect(() => {
    if (picked !== undefined && picked === (d.icon ?? null)) setPicked(undefined);
  }, [picked, d.icon]);
  const change = (next: string | null) => {
    if (!act.icon) return;
    const n = ++save.current;
    setPicked(next);
    act.icon(next).catch(() => {
      if (n === save.current) { setPicked(undefined); toast(NOT_SAVED); }
    });
  };
  const canEdit = Boolean(d.can_edit_icon && act.icon);
  return (
    <ProjectIconHeader icon={icon} canEdit={canEdit} onChange={change} shown className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
      {!icon && !canEdit && <span className="text-muted-foreground" data-testid="icon-none-set">No icon.</span>}
      <span className="text-[13px] text-muted-foreground">Shown beside the project's name everywhere it is listed.</span>
    </ProjectIconHeader>
  );
}

function Later({ d, act }: { d: FactoryProjectData; act: ProjectActions }) {
  const { toast } = useToast();
  const [busy, setBusy] = React.useState(false);
  const go = async (on: boolean) => {
    setBusy(true);
    try {
      await act.later(on);
      toast(on ? `${d.name} is kept for later. It is off your home until you start it.` : `${d.name} is started again. It is back on your home.`);
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "Could not reach the cell. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  };
  if (d.later) {
    return (
      <>
        <p className="m-0!">Kept for later. It is off your home until someone starts it.</p>
        <div><Button busy={busy} onClick={() => void go(false)} data-testid="project-start-now">Start now</Button></div>
      </>
    );
  }
  if (!d.can_later) return <p className="m-0! text-muted-foreground" data-testid="later-not-now">A project the crew is working on, or a closed one, cannot be kept for later.</p>;
  return (
    <>
      <p className="m-0!">Takes the project off your home until someone starts it again. Nothing is lost.</p>
      <div><Button variant="secondary" busy={busy} onClick={() => void go(true)} data-testid="project-later">Keep for later</Button></div>
    </>
  );
}

export default function FactoryProjectSettings() {
  return (
    <ProjectShell page="settings">
      {({ d, act }) => {
        const repos = d.repos ?? [];
        return (
          <div className="grid min-w-0 max-w-[920px] gap-6" data-testid="settings-page">
            <PageHead title="Settings" />
            <div className="grid min-w-0">
              <Part title="Icon" testid="settings-icon"><Icon d={d} act={act} /></Part>
              <Part title="Plan" testid="settings-plan">
                <p className="m-0!">The goal, the milestones and how the crew builds them.</p>
                <div><Link to={`/setup?project=${encodeURIComponent(d.id)}`} className={LINK} data-testid="edit-plan">Edit plan</Link></div>
              </Part>
              <Part title="Tickets" testid="settings-tickets">
                {d.linked.tickets_in ? (
                  <p className="m-0!">
                    The tickets live in {d.linked.tickets_in}.{" "}
                    {d.linked.url && <a href={d.linked.url} target="_blank" rel="noopener noreferrer" className={LINK} data-testid="settings-tracker-link">Open in {d.linked.tickets_in} {"↗"}</a>}
                  </p>
                ) : <p className="m-0!">The tickets live in Tares.</p>}
              </Part>
              <Part title="Repos" testid="settings-repos">
                {repos.length === 0 ? (
                  <p className="m-0! text-muted-foreground" data-testid="repos-none">No repos handed over yet. They show here once the project is handed to the crew.</p>
                ) : (
                  <ul className="m-0! grid list-none! gap-1 p-0!" data-testid="repos-list">
                    {repos.map((r) => <li key={r} className="m-0! font-mono text-[13px]" data-user-text data-testid="repo-name">{r}</li>)}
                  </ul>
                )}
              </Part>
              <Part title="Later" testid="settings-later"><Later d={d} act={act} /></Part>
            </div>
            <div className={cn("min-w-0")}><DeleteProject name={d.name} onDelete={act.remove} /></div>
          </div>
        );
      }}
    </ProjectShell>
  );
}
