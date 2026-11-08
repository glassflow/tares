import { Lock } from "../../components/icons";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "../../components/ui/select";
import { cn } from "@/lib/utils";
import type { HomeHiddenProject, HomeProject } from "./home.types";
import { Sender } from "./Sender";

// The parts of the Projects section that only a cell with more than one person has: the Mine and
// Team switch, the lock on a private project, who leads it and who is on it, the quiet row that stands
// where a private project the person is not on would be (Team), and the one line about them (Mine).

export type ProjectsView = "mine" | "team";

const LOCK = "size-3.5 flex-none text-muted-foreground";

/** Which projects the home lists (TR-1023): yours by default (you lead it, hold a ticket in it, or
 *  something in it waits for you), or all of the cell's open projects. A dropdown, top right. */
export function ProjectsSwitch({ view, counts, onView }: {
  view: ProjectsView; counts: { mine: number; team: number }; onView?: (v: ProjectsView) => void;
}) {
  return (
    <Select value={view} onValueChange={(v) => onView?.(v as ProjectsView)}>
      <SelectTrigger aria-label="Which projects" data-testid="projects-switch" className="h-8! w-auto! min-w-[110px] text-[13px]!">
        <SelectValue />
      </SelectTrigger>
      <SelectContent align="end">
        <SelectItem value="mine" data-testid="projects-mine">Yours ({counts.mine})</SelectItem>
        <SelectItem value="team" data-testid="projects-team">All ({counts.team})</SelectItem>
      </SelectContent>
    </Select>
  );
}

/** The lock before a private project's name. */
export function PrivateLock() {
  return <Lock role="img" aria-label="Private" data-testid="project-lock" className={LOCK} />;
}

/** "Led by Lee", the people on the project besides the lead, and how many tickets are the person's. */
export function LeadLine({ p, me }: { p: HomeProject; me?: string }) {
  const lead = p.lead?.name;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-muted-foreground">
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

/** "1 private project you are not on, led by Lee", as the cell words it. */
export function HiddenLine({ text }: { text: string }) {
  return (
    <div data-testid="hidden-summary" className="flex items-center gap-2.5 border-t border-border bg-background px-[18px] py-3.5 text-muted-foreground">
      <Lock aria-hidden className={LOCK} />
      <span data-user-text className="[overflow-wrap:anywhere]">{text}</span>
    </div>
  );
}

/** Team view: where a private project the person is not on would be. No name, no goal, only its lead. */
export function HiddenRow({ stub }: { stub: HomeHiddenProject }) {
  return (
    <div data-testid="hidden-row" className="flex items-center gap-2.5 border-t border-border bg-background px-[18px] py-3.5 text-muted-foreground first:border-t-0">
      <Lock aria-hidden className={LOCK} />
      <span className="[overflow-wrap:anywhere]">
        Private{stub.lead && <>, led by <span data-user-text>{stub.lead.name}</span></>}
      </span>
    </div>
  );
}
