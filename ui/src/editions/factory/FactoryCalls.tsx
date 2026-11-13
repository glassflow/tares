import * as React from "react";

import { useToast } from "../../components/edition";
import { cn } from "../../lib/utils";
import { decideCall, type FactoryProjectData, type PastCall, type ProjectCall } from "./FactoryProject.api";
import { CallRow } from "./FactoryProject";
import { DecisionTabs } from "./FactoryDecisions";
import { PageHead, ProjectShell } from "./ProjectShell";

// /projects/:id/calls, the Calls tab of Decisions and calls (TR-1245): the calls the crew made without the person. The open
// ones first, each kept or changed here; then the ones already kept or changed, newest first.
// Waiting for you on the project page and Home show a short item that links here.

const MUTED = "text-xs text-muted-foreground";
const CARD = "border border-border bg-card shadow-[0_1px_3px_rgba(0,0,0,0.07)]";

function PastRow({ c }: { c: PastCall }) {
  return (
    <div className={cn(CARD, "px-5 py-4")} data-testid="call-past" data-ref={c.ref}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <div className="flex min-w-0 items-baseline gap-2.5">
          <span className="font-mono text-xs font-medium">{c.ref}</span>
          <span className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-user-text>{c.question}</span>
        </div>
        <div className={cn(MUTED, "whitespace-nowrap")}>{c.state === "kept" ? "Kept" : "Changed"}{c.decided && `, ${c.decided}`}</div>
      </div>
      <div className={cn("mt-1.5 border-l-[3px] pl-3 [overflow-wrap:anywhere]",
                         c.state === "kept" ? "border-[color:var(--accent)] font-medium" : "border-border text-muted-foreground line-through")}
           data-user-text>{c.choice}</div>
      {c.state === "overturned" && c.words && (
        <div className="mt-1.5 border-l-[3px] border-[color:var(--accent)] pl-3 font-medium [overflow-wrap:anywhere]" data-testid="call-past-words">
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
        <PageHead title="Decisions and calls" />
        <DecisionTabs d={d} on="calls" />
        <p className="m-0! text-xs text-muted-foreground">What the crew decided on <span data-user-text>{d.name}</span> without you, so the work did not stop.</p>
      </div>
      <section className="grid gap-3" aria-label="To confirm" data-testid="calls-open">
        <h2 className="m-0! text-base! font-semibold!">To confirm</h2>
        {open.length === 0 ? (
          <div className={cn(CARD, "px-5 py-4 text-muted-foreground")} data-testid="calls-empty">Nothing to confirm. When the crew decides something without you, it shows here.</div>
        ) : open.map((c) => <div key={c.ref} className={CARD}><CallRow c={c} onDecide={decide} /></div>)}
      </section>
      {past.length > 0 && (
        <section className="grid gap-3" aria-label="Already confirmed" data-testid="calls-past">
          <h2 className="m-0! text-base! font-semibold!">Kept or changed</h2>
          {past.map((c) => <PastRow key={c.ref} c={c} />)}
        </section>
      )}
    </div>
  );
}

export default function FactoryCalls() {
  return <ProjectShell page="decisions">{({ d, reload }) => <Calls d={d} reload={reload} />}</ProjectShell>;
}
