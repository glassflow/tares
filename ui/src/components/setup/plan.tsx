import { useEffect, useRef, useState } from "react";

import { api } from "../../api";
import type { Plan, SetupProblem } from "../../types";
import { errText } from "./common";
import { AgentsCard } from "./planAgents";
import { Problems } from "./planBits";
import { focusSoon, mergeDerived, problemsBy, useCellData, type CellData } from "./planEdit";
import { KnowHowCard, ToolsSection } from "./planKnowhow";
import { WakesCard } from "./planWakes";
import { WatchesCard } from "./planWatches";

// Step 2: the whole plan on one screen, in plain words, and every part of it editable in place.
// Numbers change where they stand; each item (a source, a wake-up, an agent, a skill) opens into
// its editor inside its card, one at a time; anything else is said in plain words (adjust). After
// every edit the plan is checked (POST /api/setup/check, no model call): the sentences and the
// summary are derived again from the plan as it now is, and what stops it from being set up shows
// next to the item it concerns. Nothing is created until "Looks right, set it up".

/** The knob values the plan's sentences were written with, per wake and knob. */
export type Baseline = Record<string, number>;
export const baselineOf = (p: Plan): Baseline =>
  Object.fromEntries(p.wakes.flatMap((w) => w.knobs.map((k) => [`${w.key}.${k.id}`, k.value])));

/** What every card gets: the plan and a way to change it, the problems by item, which editor is
 *  open (one at a time: "watches.w1", "wakes.new", "own_agent"...), and what the cell offers. */
export interface CardCtx {
  plan: Plan;
  edit: (p: Plan) => void;
  probs: Record<string, string[]>;
  open: string | undefined;
  openEditor: (id: string) => void;
  /** Close the open editor and hand focus to the element with this id. */
  closeEditor: (focusId: string) => void;
  busy: boolean;
  cell: CellData;
  base: Baseline;
}

const CHECK_DELAY_MS = 350;

/** The technical list: every object the plan creates, by name, for someone who wants to check. */
function Machinery({ plan }: { plan: Plan }) {
  const m = (s: string) => <span className="mono">{s}</span>;
  const tools = plan.who === "tares" ? plan.tools.filter((t) => t.enabled) : [];
  const skills = plan.skills.filter((s) => s.enabled);
  const agents = plan.who === "tares" ? plan.agents.filter((a) => a.enabled) : [];
  return (
    <ul className="su-machinery">
      {plan.watches.map((w) => (
        <li key={w.key}>
          {w.existing ? <>The existing source {m(w.name)}</> : <>A new {m(w.connector)} source {m(w.name)}</>}
          {!w.existing && w.config && Object.keys(w.config).length > 0 && (
            <> with {Object.keys(w.config).map((k, i) => <span key={k}>{i > 0 && ", "}{m(k)}</span>)}</>
          )}
        </li>
      ))}
      {plan.wakes.map((w) => (
        <li key={w.key}>
          A trigger {m(w.name)} on {w.sources.map((s, i) => <span key={s}>{i > 0 && ", "}{m(s)}</span>)}
          {w.key_field && <>, counted per {m(w.key_field)}</>}
          {w.filters.length > 0 && <>, {w.filters.length} {w.filters.length === 1 ? "filter" : "filters"}</>}
          {w.window && <>, window {m(w.window)}</>}
          {w.cooldown && !w.condition.every && <>, quiet for {m(w.cooldown)} after it fires</>}
        </li>
      ))}
      {agents.map((a) => (
        <li key={a.key}>
          A Tares agent {m(a.name)}{" "}
          {a.on_trigger && a.trigger ? <>woken by {m(a.trigger)}</> : <>started only by a handoff</>}
          {a.provider && <>, provider {m(a.provider)}</>}
          {a.model && <>, model {m(a.model)}</>}
          {a.handoffs.map((h) => <span key={h.verdict + h.agent}>, hands off to {m(h.agent)} on the verdict {m(h.verdict)}</span>)}
          {a.mcp_servers.length > 0 && <>, may use {a.mcp_servers.map((s, i) => <span key={s}>{i > 0 && ", "}{m(s)}</span>)}</>}
        </li>
      ))}
      {plan.who === "own" && plan.own_agent && (
        <li>A project key {m(plan.own_agent.name)} that reads this project and records findings in it</li>
      )}
      {tools.map((t) => (
        <li key={t.key}>{t.existing ? <>The MCP server {m(t.name)} already on Tares</> : <>An MCP server {m(t.name)}</>}
          {t.url && !t.existing && <> at {m(t.url)}</>}</li>
      ))}
      {skills.map((s) => <li key={s.key}>A skill {m(s.name)}</li>)}
      <li>Everything stays editable later under Setup, Advanced setup</li>
    </ul>
  );
}

export function PlanStep({ projectId, plan, setPlan, base, setBase, onBack, onAdjust, onApplied }: {
  /** The draft project this plan is for. */
  projectId: string;
  plan: Plan; setPlan: (p: Plan) => void;
  base: Baseline; setBase: (b: Baseline) => void;
  onBack: () => void;
  /** Change the plan as said in plain words: the draft is planned again, shown as it happens. */
  onAdjust: (instruction: string, plan: Plan) => Promise<void>;
  onApplied: (r: Awaited<ReturnType<typeof api.applySetup>>) => void;
}) {
  const [instruction, setInstruction] = useState("");
  const [adjusting, setAdjusting] = useState(false);
  const [adjustErr, setAdjustErr] = useState<string>();
  const [details, setDetails] = useState(false);
  const [applying, setApplying] = useState(false);
  const [applyErr, setApplyErr] = useState<string>();
  const [open, setOpen] = useState<string>();
  const [problems, setProblems] = useState<SetupProblem[]>([]);
  const [checkErr, setCheckErr] = useState<string>();
  // rev counts the person's edits; checkedRev is the edit the problems on screen belong to
  const [rev, setRev] = useState(0);
  const [checkedRev, setCheckedRev] = useState(-1);
  const planRef = useRef(plan);
  planRef.current = plan;
  const revRef = useRef(rev);
  revRef.current = rev;
  const cell = useCellData();
  const busy = adjusting || applying;
  const tares = plan.who === "tares";

  const edit = (p: Plan) => { setPlan(p); setRev((r) => r + 1); };

  // check the plan a moment after the last edit: derived sentences back in, problems per item
  useEffect(() => {
    const at = rev;
    const t = window.setTimeout(async () => {
      try {
        const r = await api.checkSetup(planRef.current, projectId);
        if (revRef.current !== at) return;     // edited since: a newer check is on its way
        const merged = mergeDerived(planRef.current, r.plan);
        setPlan(merged);
        setBase(baselineOf(merged));
        setProblems(r.problems);
        setCheckErr(undefined);
      } catch (e) {
        if (revRef.current !== at) return;
        setCheckErr(errText(e));
      }
      setCheckedRev(at);
    }, rev === 0 ? 0 : CHECK_DELAY_MS);
    return () => window.clearTimeout(t);
  }, [rev]);   // eslint-disable-line react-hooks/exhaustive-deps

  const probs = problemsBy(problems);
  const stale = checkedRev !== rev;
  const ctx: CardCtx = {
    plan, edit, probs, open, busy, cell, base,
    openEditor: (id) => setOpen(id),
    closeEditor: (focusId) => { setOpen(undefined); focusSoon(focusId); },
  };

  const adjust = async () => {
    const text = instruction.trim();
    if (!text || busy) return;
    setAdjusting(true); setAdjustErr(undefined);
    try { await onAdjust(text, plan); }
    catch (e) { setAdjustErr(errText(e)); setAdjusting(false); }
  };

  const apply = async () => {
    if (busy || problems.length || stale) return;
    setApplying(true); setApplyErr(undefined);
    try { onApplied(await api.applySetup({ ...plan, name: plan.name.trim() || plan.goal }, projectId)); }
    catch (e) { setApplyErr(errText(e)); setApplying(false); }
  };

  const blocked = problems.length > 0;
  // what belongs to no item on screen (the plan as a whole; a tool while your own agent does
  // the work) shows under the name, so nothing that blocks setting up is out of sight
  const shown = new Set<string>(["watches", "wakes", "agents", "own_agent",
    ...plan.watches.map((w) => `watches.${w.key}`), ...plan.wakes.map((w) => `wakes.${w.key}`),
    ...(tares ? plan.agents.map((a) => `agents.${a.key}`) : []),
    ...(tares ? plan.tools.map((t) => `tools.${t.key}`) : []), ...plan.skills.map((s) => `skills.${s.key}`)]);
  // a problem about a part the plan no longer shows (the Tares agents, right after switching to
  // your own agent, until the next check) belongs nowhere: it is not plan-wide
  const gone = (where: string) => (!tares && /^(agents|tools)\./.test(where)) || (tares && where === "own_agent");
  const planWide = problems.filter((p) => !shown.has(p.where) && !gone(p.where)).map((p) => p.message);

  return (
    <section className="su-section" aria-labelledby="su-plan-h">
      <div className="su-head su-narrow">
        <h1 id="su-plan-h" className="su-title">Here is the plan</h1>
        <p className="su-goal-line">
          {plan.goal}{" "}
          <button type="button" className="linklike su-small" onClick={onBack} disabled={busy}>Change the goal</button>
        </p>
        {plan.summary && <p>{plan.summary}</p>}
        <p className="su-sub">Nothing runs until you say so. Change any part in place, or say what to change below. Your changes are kept as a draft, so you can leave and finish it later from Projects.</p>
      </div>

      <label className="field su-name">
        <span className="lbl">Project name</span>
        <input type="text" value={plan.name} maxLength={80} disabled={busy}
               onChange={(e) => edit({ ...plan, name: e.target.value })} />
      </label>
      <Problems list={planWide} />

      <ol className="su-cards">
        <WatchesCard ctx={ctx} />
        <WakesCard ctx={ctx} />
        <AgentsCard ctx={ctx} />
        <KnowHowCard ctx={ctx} />
      </ol>

      {tares && <ToolsSection ctx={ctx} />}

      {plan.notes.length > 0 && (
        <ul className="su-notes" aria-label="Good to know">
          {plan.notes.map((n, i) => <li key={i}>{n}</li>)}
        </ul>
      )}

      <form className="su-adjust" onSubmit={(e) => { e.preventDefault(); adjust(); }}>
        <label htmlFor="su-adjust-in" className="sr-only">Say what to change</label>
        <input id="su-adjust-in" type="text" value={instruction} disabled={busy}
               placeholder="Say what to change, for example: only for payments-api"
               onChange={(e) => setInstruction(e.target.value)} />
        <button type="submit" disabled={busy || !instruction.trim() || !!open}>
          {adjusting ? "Updating the plan…" : "Update the plan"}
        </button>
      </form>
      <p className="su-live" role="status" aria-live="polite">{adjusting && "Updating the plan…"}</p>
      {adjustErr && <div className="alert error" role="alert">{adjustErr}</div>}

      <div className="su-details">
        <button type="button" className="linklike su-small" aria-expanded={details} aria-controls="su-machinery"
                onClick={() => setDetails(!details)}>
          {details ? "Hide what Tares sets up for you" : "Show what Tares sets up for you"}
        </button>
        {details && <div id="su-machinery"><Machinery plan={plan} /></div>}
      </div>

      {applyErr && <div className="alert error" role="alert">{applyErr}</div>}
      {checkErr && <div className="alert error" role="alert">Could not check the plan: {checkErr}</div>}
      <div className="btnrow">
        <button type="button" className="primary su-cta" onClick={apply}
                disabled={busy || !!open || blocked || stale}>
          {applying ? "Setting it up…" : "Looks right, set it up"}
        </button>
        <button type="button" className="su-cta" onClick={onBack} disabled={busy}>Back</button>
      </div>
      <p className="su-live help" role="status" aria-live="polite">
        {applying ? "Setting it up…"
          : open ? "Save or cancel the part you are changing first."
          : blocked ? `${problems.length === 1 ? "One thing" : `${problems.length} things`} to fix first, marked in the plan above.`
          : stale ? "Checking the plan…" : ""}
      </p>
    </section>
  );
}
