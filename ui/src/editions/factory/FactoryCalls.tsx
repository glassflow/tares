import * as React from "react";

import { EmptyState, useToast } from "../../components/edition";
import { cn } from "../../lib/utils";
import { decideCall, type FactoryProjectData, type PastCall, type ProjectCall } from "./FactoryProject.api";
import { CallRow } from "./FactoryProject";
import { DecisionTabs } from "./FactoryDecisions";
import { PageHead, ProjectShell } from "./ProjectShell";

// /projects/:id/calls, the Answered by the crew tab of Crew questions (TR-1245, named in TR-1444): the calls the crew made without the person. The open
// ones first, each kept or changed here; then the ones already kept or changed, newest first.
// Waiting for you on the project page and Home show a short item that links here.

const MUTED = "text-xs text-muted-foreground";
// On the paper (TR-1266): a 1px rule over each section's head, rules between the calls, no cards.
const HEAD = "mb-2.5 border-t border-border pt-3";
const ROW = "border-b border-border py-3 last:border-b-0";

function PastRow({ c }: { c: PastCall }) {
  return (
    <div className={ROW} data-testid="call-past" data-ref={c.ref}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <div className="flex min-w-0 items-baseline gap-2.5">
          <span className="font-mono text-xs font-medium">{c.ref}</span>
          <span className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-user-text>{c.question}</span>
        </div>
        <div className={cn(MUTED, "whitespace-nowrap")}>{c.state === "kept" ? "Kept" : "Changed"}{c.decided && `, ${c.decided}`}</div>
      </div>
      <div className={cn("mt-1.5 [overflow-wrap:anywhere]",
                         c.state === "kept" ? "font-medium" : "text-muted-foreground line-through")}
           data-user-text>{c.choice}</div>
      {c.state === "overturned" && c.words && (
        <div className="mt-1.5 font-medium [overflow-wrap:anywhere]" data-testid="call-past-words">
          <span className="font-normal text-muted-foreground">You said: </span><span data-user-text>{c.words}</span>
        </div>
      )}
    </div>
  );
}

function Calls({ d, reload }: { d: FactoryProjectData; reload: () => void }) {
  const { toast } = useToast();
  const [gone, setGone] = React.useState<string[]>([]);
  const decide = async (c: ProjectCall, state: "kept" | "overturned", words?: string) => {
    await decideCall(d.id, c.ref, state, words);
    setGone((g) => [...g, c.ref]);
    toast(state === "kept" ? `Kept ${c.ref}. The crew carries on.` : `Sent. The crew changes ${c.ref}.`);
    reload();
  };
  const open = d.calls.filter((c) => !gone.includes(c.ref));
  const past = d.calls_past ?? [];
  return (
    <div className="grid min-w-0 gap-6" data-testid="calls-page">
      <div className="grid gap-4">
        <PageHead title="Crew questions" />
        <DecisionTabs d={d} on="calls" />
        <p className="m-0 text-xs text-muted-foreground">What the crew decided on <span data-user-text>{d.name}</span> without you, so the work did not stop.</p>
      </div>
      <section className="min-w-0" aria-label="To confirm" data-testid="calls-open">
        <div className={HEAD}><h2 className="m-0 text-base font-semibold">To confirm</h2></div>
        {open.length === 0 ? (
          <EmptyState data-testid="calls-empty">Nothing to confirm. When the crew decides something without you, it shows here.</EmptyState>
        ) : <div>{open.map((c) => <CallRow key={c.ref} c={c} onDecide={decide} />)}</div>}
      </section>
      {past.length > 0 && (
        <section className="min-w-0" aria-label="Already confirmed" data-testid="calls-past">
          <div className={HEAD}><h2 className="m-0 text-base font-semibold">Kept or changed</h2></div>
          <div>{past.map((c) => <PastRow key={c.ref} c={c} />)}</div>
        </section>
      )}
    </div>
  );
}

export default function FactoryCalls() {
  return <ProjectShell page="decisions">{({ d, reload }) => <Calls d={d} reload={reload} />}</ProjectShell>;
}
