import { Lock } from "../../components/icons";
import { cn } from "@/lib/utils";
import type { HomeProject } from "./home.types";
import { Sender } from "./WaitingForYou";

// The parts of the Projects section that only a cell with more than one person has: the Mine and
// Team switch, the lock on a private project, who leads it and who is on it, and the one line about
// private projects the person is not on.

export type ProjectsView = "mine" | "team";

const LOCK = "size-3.5 flex-none text-muted-foreground";

export function ProjectsSwitch({ view, counts, onView }: {
  view: ProjectsView; counts: { mine: number; team: number }; onView?: (v: ProjectsView) => void;
}) {
  const tab = (v: ProjectsView, label: string, n: number) => (
    <button type="button" role="tab" aria-selected={view === v} data-testid={`projects-${v}`} onClick={() => onView?.(v)}
            className={cn("h-8 cursor-pointer border-0 px-3.5 text-[13px] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color:var(--accent)]",
                          v === "team" && "border-l! border-border!",
                          view === v ? "bg-foreground font-medium text-background" : "bg-transparent text-foreground")}>
      {label}, {n}
    </button>
  );
  return (
    <div role="tablist" aria-label="Which projects" data-testid="projects-switch" className="flex border border-border">
      {tab("mine", "Mine", counts.mine)}
      {tab("team", "Team", counts.team)}
    </div>
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
