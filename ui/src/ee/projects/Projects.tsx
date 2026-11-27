// Tares enterprise licence, see ee/LICENSE
import { Lock } from "@/components/icons";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@paper-kit/ui/select";
import type { SlotProps } from "@/slotProps";
import { Sender } from "../home/Sender";

// The parts of the Projects page that only a cell with more than one person has (TR-1277): the Mine
// and Team switch, the lock on a private project, who leads it and who is on it, the quiet row that
// stands where a private project the person is not on would be (Team), and the one line about them
// (Mine). The page shows them through the slots projects.switch, projects.lock, projects.lead and
// projects.hidden (ee/index.ts); without a licence, or on a cell of one, the plain rows stay.
// Home had these until TR-1275 took its Projects block away (#655).

const LOCK = "size-3.5 flex-none text-muted-foreground";

/** Which projects the page lists: yours by default (you lead it, hold a ticket in it, or something in
 *  it waits for you), or the team's. "Team", not "All", so it never reads as the All state tab. */
export function ProjectsSwitch({ view, counts, onView }: SlotProps["projects.switch"]) {
  return (
    <Select value={view} onValueChange={(v) => onView(v === "team" ? "team" : "mine")}>
      <SelectTrigger aria-label="Whose projects" data-testid="projects-switch" className="h-9 w-auto min-w-[120px]">
        <SelectValue />
      </SelectTrigger>
      <SelectContent align="end">
        <SelectItem value="mine" data-testid="projects-mine">Yours ({counts.mine})</SelectItem>
        <SelectItem value="team" data-testid="projects-team">Team ({counts.team})</SelectItem>
      </SelectContent>
    </Select>
  );
}

/** The lock before a private project's name. */
export function PrivateLock() {
  return <Lock role="img" aria-label="Private" data-testid="project-lock" className={`${LOCK} mr-1.5 inline align-[-2px]`} />;
}

/** "Led by Lee", the people on the project besides the lead, and how many tickets are the person's. */
export function LeadLine({ p, me }: SlotProps["projects.lead"]) {
  const lead = p.lead?.name;
  if (!lead && !p.members?.length && !p.yours_text) return null;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
      {lead && (
        <span data-testid="project-lead" className="[overflow-wrap:anywhere]">
          Led by {me && lead === me ? "you" : <span data-user-text>{lead}</span>}
        </span>
      )}
      {(p.members?.length ?? 0) > 0 && (
        <span role="group" aria-label="Members" className="flex gap-[3px]">
          {p.members?.map((m) => <Sender key={m.id} from={m} small />)}
        </span>
      )}
      {p.yours_text && <span data-user-text className="[overflow-wrap:anywhere]">{p.yours_text}</span>}
    </div>
  );
}

const QUIET = "flex items-center gap-2.5 border-b border-border px-4 py-3.5 text-muted-foreground last:border-b-0";

/** After the rows: a quiet row for each private project the person is not on (Team), or the one line
 *  about them, as the cell words it (Mine). No name, no goal, only whom to ask: its lead. */
export function HiddenRows({ stubs, summary, view }: SlotProps["projects.hidden"]) {
  if (view === "mine") {
    return summary ? (
      <div data-testid="hidden-summary" className={QUIET}>
        <Lock aria-hidden className={LOCK} />
        <span data-user-text className="[overflow-wrap:anywhere]">{summary}</span>
      </div>
    ) : null;
  }
  return (
    <>
      {stubs.map((s, i) => (
        <div key={i} data-testid="hidden-row" className={QUIET}>
          <Lock aria-hidden className={LOCK} />
          <span className="[overflow-wrap:anywhere]">
            Private project. {s.lead ? <>Ask <span data-user-text>{s.lead.name}</span> to add you.</> : "Ask an admin to give it a lead."}
          </span>
        </div>
      ))}
    </>
  );
}
