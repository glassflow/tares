import { Link } from "react-router-dom";

import { ListRow, ListSection, StatusLabel } from "../../components/edition";
import { cn } from "@/lib/utils";
import { ageWords, formatStampUTC } from "../../lib/time";
import { MUTED } from "./homeShared";
import type { HomeTeam, HomeTeamPerson, HomeTeamProject } from "./home.types";
import { GroupHead } from "./waitingGroups";

// "Your team" on Home, for a person joining a factory someone else set up (TR-1646): who is on the cell
// and whether their crew is up, the open projects with their leads, and who answers questions. It is the
// first section both of the joiner's steps (JoinerStart) and of the usual Home while they are joining.

const ROW = "min-h-0 justify-between py-2.5";
const NAME = "min-w-0 flex-[1_1_200px] font-semibold";
const SAID = "text-[13px] text-muted-foreground";
const MARK = "font-normal text-muted-foreground";

function CrewState({ person, now }: { person: HomeTeamPerson; now: number }) {
  if (person.crew_running) return <StatusLabel tone="ready">Crew running</StatusLabel>;
  const heard = person.last_heard;
  if (!heard) return <span className={SAID}>No crew yet</span>;
  return (
    <span className={SAID}>
      No crew running, last heard{" "}
      <time className="whitespace-nowrap" dateTime={heard} title={formatStampUTC(heard)}>{ageWords(now - Date.parse(heard))}</time>
    </span>
  );
}

function Person({ person, now }: { person: HomeTeamPerson; now: number }) {
  return (
    <ListRow className={ROW} data-testid="your-team-person" data-id={person.id}>
      <span className={NAME}>
        <span data-user-text>{person.name}</span>
        {person.you && <span className={MARK} data-testid="your-team-you"> (you)</span>}
        {person.is_admin && <> <span className={cn(MARK, "ml-1 text-xs")} data-testid="your-team-admin">Admin</span></>}
      </span>
      <span data-testid="your-team-crew"><CrewState person={person} now={now} /></span>
    </ListRow>
  );
}

function Project({ project }: { project: HomeTeamProject }) {
  return (
    <ListRow className={ROW} data-testid="your-team-project" data-id={project.id}>
      <span className={NAME}>
        <Link to={`/projects/${encodeURIComponent(project.id)}`} data-user-text
              className="text-foreground underline decoration-border underline-offset-[3px]">{project.name}</Link>
      </span>
      <span className={SAID} data-testid="your-team-lead">
        {project.lead ? <>Lead: <span data-user-text>{project.lead.name}</span></> : "No lead"}
      </span>
    </ListRow>
  );
}

/** Nothing at all without a team: the cell sends one only while the person is joining. `now` is the time
 *  of the read, so "last heard" counts from the cell's clock. */
export default function YourTeam({ team, now }: { team?: HomeTeam; now: number }) {
  if (!team) return null;
  // the heading takes the size of Home's own section heads (SectionHead); the kit draws it a step smaller
  return (
    <ListSection paper title="Your team" className="[&_h2]:text-base" data-testid="your-team">
      {team.people.length === 0
        ? <p className={cn(SAID, "m-0 py-2.5")} data-testid="your-team-no-people">Nobody is listed yet.</p>
        : team.people.map((p) => <Person key={p.id} person={p} now={now} />)}
      <GroupHead title="Projects" testid="your-team-projects" flush />
      {team.projects.length === 0
        ? <p className={cn(SAID, "m-0 py-2.5")} data-testid="your-team-no-projects">No open project yet.</p>
        : team.projects.map((p) => <Project key={p.id} project={p} />)}
      <p className={cn(MUTED, "m-0 border-t border-border pt-2.5")} data-testid="your-team-answers">
        A project&rsquo;s lead answers its questions. With no lead, an admin does.
      </p>
    </ListSection>
  );
}
