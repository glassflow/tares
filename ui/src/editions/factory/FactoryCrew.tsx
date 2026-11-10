import * as React from "react";
import { Link } from "react-router-dom";

import { Banner, Button, ProgressBar, Skeleton } from "../../components/edition";
import { ApiError } from "../../api";
import { cn } from "@/lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  crewMember, decideRule, factoryCrew, readMemoryAgain,
  type CrewBuilders, type CrewMember, type CrewPage, type CrewProposal, type MemberDetail, type MemberLine,
} from "./FactoryCrew.api";
import { isSample, sampleDetail, sampleLoad } from "./crewSamples";

// The factory Crew page: who is on the crew, what each member is doing, how far the crew may go,
// what the Claude plan has left, the rules the reviewer proposes and the standup. FactoryCrewView is
// pure: it draws the CrewPage it is given and asks `actions` for the rest. The default export loads
// GET /api/factory/crew every 10 seconds. Every string from the cell is drawn as text.

const POLL_MS = 10_000;
const WARN_AT = 80;
const ASKED_RELOAD_MS = 4_000;

const SECTION_H = "m-0! text-base!";
const CARD = "min-w-0 border border-border bg-card [overflow-wrap:anywhere]";
const MUTED = "text-xs text-muted-foreground";
const INK_FILL = "[&>div:first-child]:bg-foreground!";
const WARN_FILL = "[&>div:first-child]:bg-[var(--warn)]!";
const LINK = "text-[color:var(--accent-deep)]! underline!";
const FOLD_BTN = "linklike text-[13px]";
const CODE = "mt-0.5 inline-block border border-border bg-muted px-1.5 py-0.5 font-mono";

export interface CrewActions {
  detail: (station: string) => Promise<MemberDetail>;
  ask: (station: string) => Promise<unknown>;
  decide: (id: string, decision: "accept" | "reject") => Promise<unknown>;
}

const DOT: Record<string, string> = {
  working: "bg-[var(--accent)]",
  idle: "bg-[var(--ok)]",
  "waiting for you": "bg-[var(--warn)]",
  "not heard": "bg-[var(--ink-faint)]",
  ended: "border border-[var(--ink-faint)] bg-transparent",
};

const LINE_TYPES: Record<string, string> = {
  user: "Asked", assistant: "Said", tool_use: "Ran", tool_result: "Result", session_state: "State",
};

/** One line of the fold: the time, what kind of line, then the text. */
function lineText(l: MemberLine): string | null {
  const type = l.type ?? "";
  const text = (l.text ?? "").trim();
  if (!LINE_TYPES[type] || !text || text === type) return null;
  let when = "";
  if (l.at) {
    const d = new Date(l.at);
    if (!Number.isNaN(d.getTime())) when = d.toTimeString().slice(0, 5);
  }
  return `${when.padEnd(5)}  ${LINE_TYPES[type].padEnd(6)}  ${text}`;
}

function SectionHead({ title, aside }: { title: string; aside?: React.ReactNode }) {
  return (
    <div className="mb-2 flex items-baseline justify-between gap-3">
      <h2 className={SECTION_H}>{title}</h2>
      {aside}
    </div>
  );
}

const Count = ({ children }: { children: React.ReactNode }) => (
  <span className="font-mono text-xs text-muted-foreground">{children}</span>
);

/** What it did lately, the terminal command and a way to ask for a fresh memory reading. Read only
 *  while the fold is open. */
function Fold({ member, actions }: { member: CrewMember; actions: CrewActions }) {
  const [open, setOpen] = React.useState(false);
  const [detail, setDetail] = React.useState<MemberDetail>();
  const [failed, setFailed] = React.useState(false);
  const [asked, setAsked] = React.useState<"idle" | "asked" | "failed">("idle");
  const timer = React.useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  React.useEffect(() => () => clearTimeout(timer.current), []);

  const load = React.useCallback(() => {
    setFailed(false);
    actions.detail(member.station).then(setDetail).catch(() => setFailed(true));
  }, [actions, member.station]);

  const toggle = () => {
    if (!open && !detail) load();
    setOpen(!open);
  };
  const ask = () => {
    setAsked("asked");
    actions.ask(member.station)
      .then(() => { timer.current = setTimeout(load, ASKED_RELOAD_MS); })
      .catch(() => setAsked("failed"));
  };

  const lines = (detail?.recent ?? []).map(lineText).filter((x): x is string => x !== null);
  return (
    <div className="col-start-2 col-end-[-1]">
      <button type="button" className={FOLD_BTN} aria-expanded={open} onClick={toggle} data-testid="member-fold">
        {open ? "Hide what it did" : "Show what it did"}
      </button>
      {open && (
        <div data-testid="member-detail">
          {failed && (
            <p className="m-0 mt-2 text-[13px] text-[color:var(--err)]">
              It did not load. <button type="button" className={FOLD_BTN} onClick={load}>Try again</button>
            </p>
          )}
          {!failed && !detail && <Skeleton className="mt-2" width="70%" />}
          {detail && (
            <>
              <pre
                data-words-exempt
                className="m-0 mt-2 max-h-72 overflow-auto whitespace-pre-wrap break-words bg-[#1e0f00] px-3 py-2.5 font-mono text-xs leading-normal text-[#efeddb]"
              >
                {lines.length ? lines.join("\n") : "Nothing recent."}
              </pre>
              {detail.attach && (
                <div className={cn(MUTED, "mt-2")}>
                  Open in a terminal<br />
                  <code data-words-exempt className={CODE}>{detail.attach}</code>
                </div>
              )}
              <div className="mt-2 flex flex-wrap items-center gap-3">
                <Button variant="secondary" className="h-[30px] text-[13px]!" onClick={ask} disabled={asked === "asked"}>
                  Read memory again
                </Button>
                {asked === "asked" && <span className={MUTED} role="status">Asked. It answers in a few seconds.</span>}
                {asked === "failed" && <span className="text-xs text-[color:var(--err)]" role="status">It could not be asked. Try again.</span>}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function Member({ m, actions }: { m: CrewMember; actions: CrewActions }) {
  const pct = typeof m.memory_pct === "number" ? m.memory_pct : null;
  const helpers = m.helpers > 0 ? `${m.helpers} ${m.helpers === 1 ? "helper" : "helpers"}` : null;
  return (
    <div
      className="grid grid-cols-[10px_minmax(0,1fr)] gap-x-3 gap-y-1 border-b border-border px-4 py-3.5 last:border-b-0 md:grid-cols-[10px_minmax(0,1fr)_auto]"
      data-testid="crew-member" data-state={m.state}
    >
      <span aria-hidden className={cn("mt-1.5 inline-block size-2", DOT[m.state] ?? DOT["not heard"])} />
      <div className="min-w-0">
        <div className="font-semibold" data-user-text>{m.doing}</div>
        <div className={MUTED}><b className="font-semibold text-foreground">{m.role_label}</b> · {m.state_text}</div>
      </div>
      <div className={cn(MUTED, "col-start-2 md:col-start-auto md:whitespace-nowrap md:text-right")}>
        {m.heard ? `Heard ${m.heard}` : "Not heard from yet"}
      </div>
      {(pct !== null || helpers) && (
        <div className={cn(MUTED, "col-start-2 col-end-[-1] flex flex-wrap items-center gap-x-2.5 gap-y-1")}>
          {pct !== null && (
            <>
              <ProgressBar
                value={pct} aria-label="Memory" data-testid="memory-bar"
                className={cn("mt-0 h-1 w-[120px] shrink-0", pct >= WARN_AT ? WARN_FILL : INK_FILL)}
              />
              <span>{m.memory_text ?? `Memory ${Math.round(pct)}% full`}</span>
            </>
          )}
          {helpers && <span>{helpers}</span>}
        </div>
      )}
      {m.state !== "ended" && <Fold member={m} actions={actions} />}
    </div>
  );
}

function Single({ title, m, actions, empty }: { title: string; m: CrewMember | null; actions: CrewActions; empty?: React.ReactNode }) {
  return (
    <section>
      <SectionHead title={title} />
      <div className={CARD}>{m ? <Member m={m} actions={actions} /> : empty}</div>
    </section>
  );
}

function NotStarted() {
  return (
    <div className="px-4 py-5 text-muted-foreground" data-testid="crew-not-started">
      The crew is not started. Start it from a terminal and it appears here.
      <div className={cn(MUTED, "mt-2")}>
        Open in a terminal<br />
        <code data-words-exempt className={CODE}>tares factory crew up</code>
      </div>
    </div>
  );
}

/** One project's builders: the running ones (a quiet one counts), then a fold with the finished ones. */
function BuilderGroup({ g, actions }: { g: CrewBuilders; actions: CrewActions }) {
  const [open, setOpen] = React.useState(false);
  const running = g.members.filter((m) => m.state !== "ended");
  const finished = g.members.filter((m) => m.state === "ended");
  return (
    <div data-testid="crew-group">
      <div className="flex items-baseline justify-between gap-3 border-b border-border bg-[var(--wash-2)] px-4 py-2.5 text-[13px] font-semibold">
        <span data-user-text>{g.project.name}</span>
        <span className="shrink-0 whitespace-nowrap font-mono text-xs font-normal text-muted-foreground">{running.length} running · {g.members.length}</span>
      </div>
      {running.map((m) => <Member key={m.station} m={m} actions={actions} />)}
      {finished.length > 0 && (
        <div className="border-b border-border last:border-b-0">
          <button
            type="button" className={cn(FOLD_BTN, "block! w-full px-4! py-2.5! text-left")}
            aria-expanded={open} onClick={() => setOpen(!open)} data-testid="crew-finished-fold"
          >
            {open ? "Hide" : "Show"} {finished.length} finished
          </button>
        </div>
      )}
      {open && finished.map((m) => <Member key={m.station} m={m} actions={actions} />)}
    </div>
  );
}

function Builders({ groups, actions }: { groups: CrewBuilders[]; actions: CrewActions }) {
  const live = groups.reduce((n, g) => n + g.members.filter((m) => m.state !== "ended").length, 0);
  return (
    <section data-testid="crew-builders">
      <SectionHead title="Builders" aside={<Count>{live} on the crew now</Count>} />
      <div className={CARD}>
        {groups.length === 0 && <div className="px-4 py-5 text-muted-foreground">No builder is on the crew.</div>}
        {groups.map((g) => <BuilderGroup key={g.project.id ?? "none"} g={g} actions={actions} />)}
      </div>
    </section>
  );
}

function Proposals({ list, actions, reload }: { list: CrewProposal[]; actions: CrewActions; reload: () => void }) {
  const [gone, setGone] = React.useState<Set<string>>(new Set());
  const [failed, setFailed] = React.useState<string | null>(null);
  const shown = list.filter((p) => !gone.has(p.id));
  const decide = (id: string, decision: "accept" | "reject") => {
    setFailed(null);
    actions.decide(id, decision)
      .catch((e) => { if (!(e instanceof ApiError && e.status === 409)) throw e; })
      .then(() => { setGone((g) => new Set(g).add(id)); reload(); })
      .catch(() => setFailed(id));
  };
  return (
    <section data-testid="crew-rules">
      <SectionHead title="Rules the crew proposes" aside={<Count>{shown.length}</Count>} />
      <div className={CARD}>
        {shown.length === 0 && <div className="px-4 py-5 text-muted-foreground">No rules are proposed.</div>}
        {shown.map((p) => (
          <div key={p.id} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border px-4 py-3.5 last:border-b-0" data-testid="crew-rule">
            <div className="min-w-0 flex-[1_1_320px]">
              <div data-user-text>{p.text}</div>
              <div className={cn(MUTED, "mt-0.5")}>From {p.from}, {p.when}</div>
              {failed === p.id && <div className="mt-1 text-xs text-[color:var(--err)]" role="alert">It could not be saved. Try again.</div>}
            </div>
            <div className="flex gap-2">
              <Button variant="secondary" className="h-8 border-foreground! text-[13px]!" onClick={() => decide(p.id, "accept")}>Accept</Button>
              <Button variant="secondary" className="h-8 text-[13px]!" onClick={() => decide(p.id, "reject")}>Reject</Button>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function HowFar({ how }: { how: CrewPage["how_far"] }) {
  const href = how.settings_href || "/settings#crew";
  return (
    <section data-testid="crew-how-far">
      <SectionHead title="Autonomy" />
      <div className={cn(CARD, "px-4 py-3.5")}>
        <div className="font-semibold">{how.text}</div>
        {how.detail && <div className="mt-0.5 text-[13px] text-muted-foreground">{how.detail}</div>}
        <Link to={href} className={cn(LINK, "mt-2 inline-block text-[13px]")}>Change in Settings</Link>
      </div>
    </section>
  );
}

function Plan({ plan }: { plan: CrewPage["plan"] }) {
  if (!plan?.known || !(plan.limits ?? []).length) return null;
  return (
    <section data-testid="crew-plan">
      <SectionHead title="Claude plan" />
      <div className={CARD}>
        {plan.limits.map((l) => (
          <div key={l.kind} className="border-b border-border px-4 py-3">
            <div className="mb-1.5 flex justify-between gap-3 text-[13px]"><span>{l.label}</span><span>{l.text}</span></div>
            <ProgressBar value={l.pct} aria-label={l.label} className={cn("mt-0 h-1", l.pct >= WARN_AT ? WARN_FILL : INK_FILL)} />
          </div>
        ))}
        {(plan.by_project ?? []).length > 0 && (
          <div className="px-4 py-3 text-[13px]">
            {plan.by_project.map((r) => (
              <div key={r.name} className="flex justify-between gap-3 py-0.5">
                <span data-user-text>{r.name}</span>
                <span className="text-right text-muted-foreground">{r.share_text}{r.cost_text ? `, ${r.cost_text}` : ""}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function Loading() {
  return (
    <div className={cn(CARD, "grid gap-4 p-4")} role="status" aria-busy="true" aria-label="Loading" data-testid="crew-loading">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="grid gap-2"><Skeleton width="60%" /><Skeleton width="40%" /><Skeleton width="75%" /></div>
      ))}
    </div>
  );
}

export function FactoryCrewView({ data, error, reload, actions }: {
  data: CrewPage | undefined; error: string | undefined; reload: () => void; actions: CrewActions;
}) {
  const started = Boolean(data && (data.talk_to || data.reviewer || data.releaser || (data.builders ?? []).length));
  return (
    <div className="grid gap-6 md:gap-8" data-testid="factory-crew">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Crew</h1>
        <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground">Who is working, what each is doing and the crew's autonomy.</p>
      </div>

      {error && (
        <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
          <b>The crew did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
        </Banner>
      )}
      {!data && !error && <Loading />}
      {data && (
        <div className="grid gap-5">
          {data.summary && <p className="m-0! text-sm text-muted-foreground" data-testid="crew-summary">{data.summary}</p>}
          {data.pacing && <Banner tone="warn" data-testid="crew-pacing">{data.pacing}</Banner>}
          <div className="flex flex-wrap items-start gap-8">
            <div className="grid min-w-0 flex-[2_1_560px] gap-7">
              <Single title="Orchestrator" m={data.talk_to} actions={actions} empty={<NotStarted />} />
              {started && data.reviewer && <Single title="Reviewer" m={data.reviewer} actions={actions} />}
              {started && data.releaser && <Single title="Releaser" m={data.releaser} actions={actions} />}
              {started && <Builders groups={data.builders ?? []} actions={actions} />}
              {started && <Proposals list={data.rule_proposals ?? []} actions={actions} reload={reload} />}
              {started && (
                <section data-testid="crew-standup">
                  <SectionHead title="Standup" />
                  <div className={CARD}>
                    {data.standup ? (
                      <div className="px-4 py-3.5">
                        <div data-user-text>{data.standup.text}</div>
                        <div className={cn(MUTED, "mt-1.5")}>{data.standup.when}</div>
                      </div>
                    ) : (
                      <div className="px-4 py-5 text-muted-foreground">Nothing moved in the last day.</div>
                    )}
                  </div>
                </section>
              )}
            </div>
            <div className="grid min-w-0 flex-[1_1_300px] gap-6">
              {data.how_far && <HowFar how={data.how_far} />}
              {started && data.plan && <Plan plan={data.plan} />}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

const LIVE_ACTIONS: CrewActions = {
  detail: crewMember,
  ask: readMemoryAgain,
  decide: decideRule,
};
const SAMPLE_ACTIONS: CrewActions = {
  detail: () => sampleDetail(),
  ask: () => Promise.resolve(),
  decide: () => Promise.resolve(),
};

export default function FactoryCrew() {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const { data, error, reload } = usePagePolling<CrewPage>(
    () => (sample ? sampleLoad(sample) : factoryCrew()), POLL_MS,
  );
  return <FactoryCrewView data={data} error={error} reload={reload} actions={sample ? SAMPLE_ACTIONS : LIVE_ACTIONS} />;
}
