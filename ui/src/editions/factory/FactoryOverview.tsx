import * as React from "react";

import { Button } from "../../components/edition";
import { cn } from "../../lib/utils";
import type { SetupData, SetupSlot } from "./FactorySetup.api";

/** Step 0 of setup: what the factory is, before any form. Who is on the crew, what they work with
 *  (each with the step that connects it), and what happens to one goal. Always in the rail, so a
 *  person can come back to it. */

const CARD = "min-w-0 border border-border bg-card [overflow-wrap:anywhere]";

function Ico({ children }: { children: React.ReactNode }) {
  return (
    <svg aria-hidden viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round" className="size-6">
      {children}
    </svg>
  );
}

const MEMBERS: { key: string; name: string; line: string; icon: React.ReactNode }[] = [
  { key: "you", name: "You", line: "Set the goal and the bar. Step in only for the calls that are yours.",
    icon: <><circle cx="12" cy="8" r="4" /><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6" /></> },
  { key: "orchestrator", name: "Orchestrator", line: "Drives the crew. Turns your plan into work, settles what it can and asks you only what it can't.",
    icon: <><circle cx="12" cy="12" r="3" /><path d="M12 3v4M12 17v4M3 12h4M17 12h4" /></> },
  { key: "builders", name: "Builders", line: "Turn tickets into merged pull requests, many at once. A second model checks every commit.",
    icon: <path d="M8 7 3 12l5 5M16 7l5 5-5 5" /> },
  { key: "reviewer", name: "Reviewer", line: "Holds every pull request to a strict bar. Nothing merges without its approval.",
    icon: <><circle cx="11" cy="11" r="6" /><path d="m20 20-4.5-4.5" /></> },
  { key: "releaser", name: "Releaser", line: "Tags, releases and deploys, then proves it works. Optional.",
    icon: <path d="M12 3v12M7 10l5 5 5-5M4 20h16" /> },
];

/** "a", "a or b", "a, b or c" */
function orList(xs: string[]): string {
  return xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} or ${xs[xs.length - 1]}`;
}

/** What this cell offers for a step: its connected and available integrations, by name. */
function choicesOf(data: SetupData, slot: SetupSlot): string {
  const st = data.steps.find((s) => s.slot === slot);
  if (!st) return "";
  return orList([...new Set([...st.connected.map((c) => c.title), ...st.available.map((a) => a.title)])]);
}

interface Tool { key: string; name: string; steps: string[]; use: string; options: string; wide?: boolean }

function tools(data: SetupData): Tool[] {
  return [
    { key: "code", name: "Code", steps: ["github"], use: "Builders push branches and open pull requests.", options: "GitHub, through the GitHub App or a token" },
    { key: "tickets", name: "Tickets", steps: ["tickets"], use: "Holds the plan. Builders take their work from it.", options: choicesOf(data, "tickets") },
    { key: "review", name: "Review", steps: ["review"], use: "Reads each pull request before it merges.", options: choicesOf(data, "review") },
    { key: "ci", name: "CI", steps: ["ci"], use: "Tests each pull request.", options: choicesOf(data, "ci") },
    { key: "runtime", name: "Runtime", steps: ["runtime", "terminal"], use: "Every crew member is a session of the runtime you choose, on your computer. A crew keeps the runtime it started on.", options: choicesOf(data, "runtime"), wide: true },
  ];
}

const FLOW: { who: string; text: string; tools: string[]; you?: boolean }[] = [
  { who: "You", text: "plan the goal with Claude Code or Codex. It writes the tickets.", tools: ["Tickets"], you: true },
  { who: "The orchestrator", text: "puts builders to work and answers their questions, so you are not asked.", tools: [] },
  { who: "Builders", text: "write the code and open pull requests.", tools: ["Code"] },
  { who: "The reviewer", text: "checks each one while CI tests it.", tools: ["Review", "CI"] },
  { who: "It merges", text: "when review and CI pass, or when you approve.", tools: [], you: true },
];

const H2 = "m-0 text-lg font-semibold";

export function OverviewView({ data, numbers, next, onGo }: {
  data: SetupData;
  /** the rail's number of each step, by key */
  numbers: Record<string, number>;
  next: { key: string; name: string } | null;
  onGo: (key: string) => void;
}) {
  const stepWords = (keys: string[]) => {
    const ns = keys.map((k) => numbers[k]).filter((n): n is number => typeof n === "number");
    return ns.length === 0 ? "" : ns.length === 1 ? `Step ${ns[0]}` : `Steps ${ns.slice(0, -1).join(", ")} and ${ns[ns.length - 1]}`;
  };
  return (
    <div className="grid min-w-0 max-w-[900px] gap-8" data-testid="step-view" data-slot="overview">
      <div className="grid gap-2">
        <h1 className="m-0 text-[22px] min-[900px]:text-[26px]" data-testid="page-title" tabIndex={-1}>How the factory works</h1>
        <p className="m-0 max-w-[760px] text-[15px] text-muted-foreground" data-testid="page-sub">
          A crew of Claude Code sessions on your computer turns your goals into merged pull requests.
        </p>
      </div>

      <section aria-labelledby="overview-crew" className="grid gap-3">
        <h2 id="overview-crew" className={H2}>Your crew</h2>
        <ul className="m-0 grid list-none gap-3 p-0 grid-cols-2 min-[700px]:grid-cols-3 min-[1100px]:grid-cols-5" data-testid="overview-crew">
          {MEMBERS.map((m) => (
            <li key={m.key} className={cn(CARD, "grid content-start gap-1.5 p-3.5")} data-testid={`member-${m.key}`}>
              <Ico>{m.icon}</Ico>
              <span className="text-[15px] font-semibold">{m.name}</span>
              <span className="text-[13px] leading-snug text-muted-foreground">{m.line}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="overview-tools" className="grid gap-3">
        <h2 id="overview-tools" className={H2}>What they work with</h2>
        <ul className="m-0 grid list-none gap-3 p-0 min-[700px]:grid-cols-2" data-testid="overview-tools">
          {tools(data).map((t) => (
            <li key={t.key} className={cn(CARD, "grid content-start gap-1 px-3.5 py-3", t.wide && "min-[700px]:col-span-2")} data-testid={`tool-${t.key}`}>
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[15px] font-semibold">{t.name}</span>
                {stepWords(t.steps) && (
                  <button type="button" onClick={() => onGo(t.steps[0])} data-testid="tool-step"
                    className="border-0 bg-transparent p-0 text-xs font-normal text-muted-foreground underline-offset-2 cursor-pointer hover:underline">
                    {stepWords(t.steps)}
                  </button>
                )}
              </div>
              <span className="text-[13.5px] leading-snug">{t.use}</span>
              {t.options && <span className="text-[12.5px] text-muted-foreground" data-testid="tool-options">{t.options}</span>}
            </li>
          ))}
        </ul>
      </section>

      <section aria-labelledby="overview-flow" className="grid gap-3.5">
        <h2 id="overview-flow" className={H2}>One goal, start to finish</h2>
        <ol className="m-0 ml-2.5 list-none border-l border-foreground p-0" data-testid="overview-flow">
          {FLOW.map((f, i) => (
            <li key={i} className={cn("relative pl-6", i < FLOW.length - 1 ? "pb-4" : "pb-0.5")}>
              <span aria-hidden className={cn("absolute top-1 -left-[6.5px] size-3", f.you ? "bg-foreground" : "border-2 border-foreground bg-background")} />
              <div className="flex flex-wrap items-center gap-2 text-[15px]">
                <span><b>{f.who}</b> {f.text}</span>
                {f.tools.map((t) => <span key={t} className="whitespace-nowrap border border-border bg-card px-2 py-px text-[12.5px]">{t}</span>)}
              </div>
            </li>
          ))}
        </ol>
      </section>

      {next && (
        <div className="border-t border-border pt-5" data-testid="step-footer">
          <Button onClick={() => onGo(next.key)} data-testid="step-next">Set up your crew</Button>
        </div>
      )}
    </div>
  );
}
