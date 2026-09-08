import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../api";
import AgentForm from "../components/AgentForm";
import IngestSetup from "../components/IngestSetup";
import { KeySetup, ToolRun } from "../components/AskChat";
import type { ToolPart } from "../components/AskChat";
import { Picker } from "../components/bits";
import { applyProposal, ProposalBody, ProposalShell, proposalTitle } from "../components/proposals";
import type { DecisionMap, Proposal } from "../components/proposals";
import SourceForm from "../components/SourceForm";
import TemplateParams, { paramText, paramsBody } from "../components/TemplateParams";
import type { Detected } from "../components/TemplateParams";
import TriggerEditor from "../components/TriggerEditor";
import { useAgentStream } from "../components/useAgentStream";
import type { StreamEvent, WireMessage } from "../components/useAgentStream";
import ViewEditor from "../components/ViewEditor";
import type { AgentPreset, BuiltinAgent, ConnectorSpec, Project, ProjectObjectKind, Source, Template, Trigger, View, ViewFilter } from "../types";

// The AI-guided project builder (TR-243 to TR-246): describe what you need in your own words,
// the assistant proposes the pieces one step at a time, and you complete each proposal in the
// same form you would use on that object's own page. Every Apply creates a real object through
// its normal API and appends it to an ordinary `custom` project, so the result is a project like
// any other and abandoning the page mid-way leaves real, editable objects behind by design.
//
// The assistant never creates anything: each build turn gets only that step's proposal tool
// (tares/agent.py, tools_for) and the console fires the create call when the user clicks.
// Secrets and Slack channels are never the model's to fill: a source proposal lists them in
// `needs` and the form highlights them; an agent proposal picks only the delivery kind.

type StepKey = "sources" | "watch" | "agent";
// Views and triggers are one step: the user thinks "what should fire" as one question.
const STEPS: { key: StepKey; label: string; kinds: ProjectObjectKind[] }[] = [
  { key: "sources", label: "Sources", kinds: ["source"] },
  { key: "watch", label: "Views and triggers", kinds: ["view", "trigger"] },
  { key: "agent", label: "Agent", kinds: ["agent"] },
];
const NEXT: Record<StepKey, StepKey | "done"> = { sources: "watch", watch: "agent", agent: "done" };

type Part = { type: "text"; text: string } | ToolPart | { type: "proposal"; proposal: Proposal };
type Turn = { parts: Part[] };
type StepState = { turns: Turn[] };

/** The transcript the model sees. Proposals contribute their decision, so a later step knows
 *  what was created and what the user declined. */
const wireText = (t: Turn, decisions: DecisionMap) => t.parts.map((p) => {
  if (p.type === "text") return p.text;
  if (p.type === "proposal") {
    const d = decisions[p.proposal.id];
    const what = p.proposal.kind === "labels" ? `labels for ${p.proposal.source}` : `${p.proposal.kind} ${p.proposal.name}`;
    return `\n[proposal: ${what}; ${d?.status === "applied" ? "created" : d?.status ?? "pending"}]\n`;
  }
  return "";
}).join("").trim() || "[no answer]";

const kindOf = (p: Proposal): ProjectObjectKind | null =>
  p.kind === "labels" || p.kind === "project" ? null : p.kind;

/** A project name from the goal, editable until the project exists: the nouns of the user's
 *  world (a service, a container, a repo), never the verbs of ours. Three words at most, so
 *  "watch the docker container checkout-api and its prometheus metrics" becomes
 *  "checkout-api metrics". */
const NAME_STOP = new Set([
  "the", "and", "when", "with", "that", "this", "from", "into", "what", "your", "my", "an", "a", "to", "of",
  "on", "in", "it", "its", "is", "me", "for", "want", "please", "then", "also", "some", "any", "all", "one",
  "watch", "tell", "wake", "show", "build", "need", "keep", "make", "get", "let", "run", "runs", "running",
  "agent", "agents", "service", "services", "project", "tares", "data", "something", "wrong", "happens",
  "up", "date", "every", "each", "top", "over", "via", "using", "use", "like",
]);
function nameFromGoal(goal: string, incident: boolean): string {
  const ws = goal.toLowerCase().split(/[^a-z0-9-]+/).filter((w) => w.length > 2 && !NAME_STOP.has(w));
  // prefer words that look like names of things: hyphenated, or ending in a system-ish noun
  const named = ws.filter((w) => w.includes("-") || /(api|logs?|metrics|db|repo|repos|server|checkout|payments?|orders?|weather|deploys?)$/.test(w));
  const pick = [...new Set([...named, ...ws])].slice(0, 3);
  const base = pick.join(" ") || "my project";
  return incident ? `catch ${base}` : base;
}

export default function ProjectNewAssist() {
  const [ready, setReady] = useState<boolean>();
  const refreshKey = () => api.capabilities()
    .then((c) => setReady(!!c.agent_key_configured)).catch(() => setReady(false));
  useEffect(() => { refreshKey(); }, []);

  // Handed over from the landing screen (Landing.tsx): the goal already typed, so the builder
  // starts its first turn on arrival and the user never sees an empty box. `incident` means the
  // text is a pasted alert or thread, framed for the model as something to have caught.
  const handoff = (useLocation().state ?? {}) as { goal?: string; incident?: boolean; template?: string };
  const [goal, setGoal] = useState(handoff.goal ?? "");
  const [projectName, setProjectName] = useState(handoff.goal && !handoff.template ? nameFromGoal(handoff.goal, !!handoff.incident) : "");
  // a picked template is named after itself, not after words chopped out of its sentence
  useEffect(() => {
    if (!handoff.template) return;
    api.template(handoff.template).then((t) => setProjectName((cur) => cur || t.title)).catch(() => {});
  }, [handoff.template]);
  const [step, setStep] = useState<StepKey | "describe" | "done">("describe");
  const [states, setStates] = useState<Record<StepKey, StepState>>({
    sources: { turns: [] }, watch: { turns: [] }, agent: { turns: [] },
  });
  const [decisions, setDecisions] = useState<DecisionMap>({});
  const [history, setHistory] = useState<WireMessage[]>([]);
  const [refine, setRefine] = useState("");
  const { send, stop, streaming } = useAgentStream();
  const chatEnd = useRef<HTMLDivElement>(null);
  // a decided card folds to one line; "show" opens it again, and revealing from the chat too
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  // what this build has made so far; the project is created around the first source
  const [project, setProject] = useState<Project>();
  const [objects, setObjects] = useState<{ kind: ProjectObjectKind; name: string }[]>([]);
  const [projectErr, setProjectErr] = useState<string>();

  // reference data the forms need
  const [specs, setSpecs] = useState<Record<string, ConnectorSpec>>();
  const [existing, setExisting] = useState<Source[]>([]);
  const refreshSources = () => api.sources().then(setExisting).catch(() => {});
  useEffect(() => {
    api.connectors().then(setSpecs).catch(() => setSpecs({}));
    refreshSources();
  }, []);

  const created = (kind: ProjectObjectKind) => objects.filter((o) => o.kind === kind).map((o) => o.name);

  const decide = (id: string, status: "applied" | "skipped" | "error", detail?: string) =>
    setDecisions((d) => ({ ...d, [id]: { status, detail } }));

  /** Create the project around the first object, append every later one. Throws with the API
   *  error so the card shows it; the object itself already exists and stays.
   *
   *  Serialized through a promise chain and refs, not state: two cards applied in quick
   *  succession would both see "no project yet" and try to create it twice, and the second
   *  append would overwrite the first with a stale object list. */
  const projectRef = useRef<Project>();
  const objectsRef = useRef<{ kind: ProjectObjectKind; name: string }[]>([]);
  const ownQueue = useRef<Promise<void>>(Promise.resolve());
  const own = (kind: ProjectObjectKind, name: string) => {
    const run = async () => {
      const next = [...objectsRef.current, { kind, name }];
      setProjectErr(undefined);
      try {
        if (!projectRef.current) {
          const p = await api.createProject({ template: "custom", name: projectName.trim(), objects: next });
          projectRef.current = p;
          setProject(p);
        } else {
          await api.updateProject(projectRef.current.id, { objects: next });
        }
        objectsRef.current = next;
        setObjects(next);
      } catch (e) {
        const msg = String((e as Error).message ?? e);
        setProjectErr(`${kind} ${name} exists but could not be added to the project: ${msg}`);
        throw e;
      }
    };
    const p = ownQueue.current.then(run, run);
    ownQueue.current = p.catch(() => {});
    return p;
  };

  /** A template project is the whole build in one card: record it and finish. */
  const finishWith = (p: Project) => {
    api.template(p.template).then(setFinishedTemplate).catch(() => {});
    projectRef.current = p;
    setProject(p);
    objectsRef.current = p.objects.map((o) => ({ kind: o.kind, name: o.name }));
    setObjects(objectsRef.current);
    setStep("done");
  };

  /** One build turn: push the user framing, stream the assistant's answer into this step. */
  const turn = async (stepKey: StepKey, userText: string) => {
    const messages: WireMessage[] = [...history, { role: "user", content: userText }];
    const idx = states[stepKey].turns.length;
    setStates((s) => ({ ...s, [stepKey]: { turns: [...s[stepKey].turns, { parts: [] }] } }));
    const mut = (fn: (parts: Part[]) => Part[]) =>
      setStates((s) => ({ ...s, [stepKey]: { turns: s[stepKey].turns.map((t, i) => i === idx ? { parts: fn(t.parts) } : t) } }));
    chatEnd.current?.scrollIntoView({ block: "nearest" });
    const appendText = (text: string) => mut((parts) => {
      const last = parts[parts.length - 1];
      if (last && last.type === "text") return [...parts.slice(0, -1), { type: "text", text: last.text + text }];
      return [...parts, { type: "text", text }];
    });
    let collected: Part[] = [];
    const onEvent = (e: StreamEvent) => {
      if (e.type === "text") appendText(e.text);
      else if (e.type === "tool") mut((parts) => [...parts, { type: "tool", id: e.id, name: e.name, input: e.input }]);
      else if (e.type === "tool_done") mut((parts) => parts.map((p) => p.type === "tool" && p.id === e.id ? { ...p, ms: e.ms, ok: e.ok, preview: e.preview } : p));
      else if (e.type === "proposal") mut((parts) => [...parts, { type: "proposal", proposal: e.proposal }]);
      else if (e.type === "error") appendText(`\n\n⚠️ ${e.detail}`);
      // keep a local copy for the wire: state updates are async and the turn ends before they settle
      if (e.type === "text") {
        const last = collected[collected.length - 1];
        if (last && last.type === "text") last.text += e.text; else collected.push({ type: "text", text: e.text });
      } else if (e.type === "proposal") collected.push({ type: "proposal", proposal: e.proposal });
    };
    await send(messages, onEvent, { mode: "build", step: stepKey });
    // the assistant's reply on the wire carries the proposals as pending; their decisions are
    // folded in when the next turn is framed (see framing below)
    setHistory([...messages, { role: "assistant", content: wireText({ parts: collected }, {}) }]);
    collected = [];
  };

  /** What this build has already made, for the framing: after a Change the assistant must build
   *  on it, not propose it again. Sources are the ones the user took pains over. */
  const existingLine = () => {
    const have = (["source", "view", "trigger", "agent"] as ProjectObjectKind[])
      .map((k) => [k, created(k)] as const).filter(([, n]) => n.length);
    return have.length
      ? `\n\nAlready created and part of this project, keep and build on them, do not propose them again: `
        + have.map(([k, n]) => `${k}s ${n.join(", ")}`).join("; ") + "."
      : "";
  };

  const start = async () => {
    setStep("sources");
    const framing = handoff.incident
      ? `Project: ${projectName.trim()}.\nThis is an incident I had, pasted as it was written:\n\n${goal.trim()}\n\nPropose the project that would have caught it: the sources that carry the signal it shows, named the way the paste names things. Say in one sentence when it would have fired. Ask me only what the paste does not say.`
      : handoff.template
      ? `Project: ${projectName.trim()}.\nI picked the template "${handoff.template}": ${goal.trim()}\n\nSet it up for me: read its parameters with list_templates, run detect_template, ask me only for what neither says, then propose the project.`
      : `Project: ${projectName.trim()}.\nGoal: ${goal.trim()}`;
    await turn("sources", framing + existingLine());
  };

  // Change: back to the goal. Stops a turn in flight and drops what was only proposed; what was
  // created stays, stays in the project, and is named to the assistant on the next start.
  const [changing, setChanging] = useState(false);
  const lastStep = useRef<StepKey | "done">("sources");
  const change = () => {
    stop();
    if (step !== "describe") lastStep.current = step;
    setChanging(true);
    setStep("describe");
  };
  const restart = async () => {
    setStates({ sources: { turns: [] }, watch: { turns: [] }, agent: { turns: [] } });
    setDecisions({});
    setHistory([]);
    setRefine("");
    setChanging(false);
    await start();
  };

  // A template's project exists at most once (its objects have fixed names), so a picked template
  // that already has one is answered here, without a model turn: open it.
  const [already, setAlready] = useState<Project | null | undefined>(handoff.template ? undefined : null);
  useEffect(() => {
    if (!handoff.template) return;
    api.projects().then((r) => setAlready(r.projects.find((p) => p.template === handoff.template) ?? null))
      .catch(() => setAlready(null));
  }, [handoff.template]);

  // arriving from the landing screen: start at once, once, when the key is known to be there
  const autoStarted = useRef(false);
  useEffect(() => {
    if (ready && handoff.goal && step === "describe" && already === null && !autoStarted.current) {
      autoStarted.current = true;
      start();
    }
  }, [ready, already]);   // eslint-disable-line react-hooks/exhaustive-deps

  // Sources of this build with no events yet: on the watch step the model has nothing to ground
  // a view or trigger in, and the page says so instead of leaving the user with a silent step.
  const [quiet, setQuiet] = useState<string[]>([]);
  useEffect(() => {
    if (step !== "watch" || streaming) return;
    let live = true;
    Promise.all(created("source").map((n) =>
      api.sourceFields(n).then((f) => (f.sampled === 0 ? n : null)).catch(() => null)))
      .then((r) => { if (live) setQuiet(r.filter((n): n is string => n !== null)); });
    return () => { live = false; };
  }, [step, streaming]);   // eslint-disable-line react-hooks/exhaustive-deps

  // the template behind a finished template project, for its setup steps on Done
  const [finishedTemplate, setFinishedTemplate] = useState<Template>();

  /** Move on: tell the model what got created (with the decisions now known) and ask for the
   *  next step's proposals. */
  const advance = async () => {
    if (step === "describe" || step === "done") return;
    const next = NEXT[step];
    // rewrite the last assistant message with the decisions, so skipped proposals are not
    // reported as pending forever
    const last = states[step].turns[states[step].turns.length - 1];
    if (last) setHistory((h) => [...h.slice(0, -1), { role: "assistant", content: wireText(last, decisions) }]);
    if (next === "done") { setStep("done"); return; }
    setStep(next);
    const framing = next === "watch"
      ? `Sources connected: ${created("source").join(", ") || "none"}. Now the views and triggers: what to correlate and what should fire, for the goal. Check source_fields on each source first. Ask me what you need to know before proposing thresholds or conditions I have not stated.`
      : `Views created: ${created("view").join(", ") || "none"}; triggers created: ${created("trigger").join(", ") || "none"}. Now propose the one Tares agent that runs when they fire, and its delivery kind.`;
    await turn(next, framing);
  };

  const sendRefine = async () => {
    if (step === "describe" || step === "done" || !refine.trim()) return;
    const text = refine.trim();
    setRefine("");
    await turn(step, text);
  };

  if (ready === undefined || already === undefined) return <div className="dim">loading…</div>;
  if (already) {
    return (
      <>
        <PageHead />
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>You already have this</h2>
          <p>
            <strong>{already.name}</strong> was set up from this template. A cell holds one of these,
            because its objects have fixed names.
          </p>
          <div className="btnrow">
            <Link className="btn primary" to={`/projects/${encodeURIComponent(already.id)}`}>Open it</Link>
            <Link className="btn" to="/">Describe something else</Link>
          </div>
        </div>
      </>
    );
  }
  if (!ready) {
    return (
      <>
        <PageHead />
        <KeySetup onSaved={refreshKey} />
      </>
    );
  }

  const stepIndex = step === "describe" ? -1 : step === "done" ? STEPS.length : STEPS.findIndex((s) => s.key === step);
  const pending = step !== "describe" && step !== "done"
    ? states[step].turns.flatMap((t) => t.parts).filter((p): p is { type: "proposal"; proposal: Proposal } =>
        p.type === "proposal" && !decisions[p.proposal.id]).length
    : 0;
  /** Why Continue is held on this step, or undefined when it may go on. Each step needs the
   *  object the next one builds on: a source to watch, a trigger to wake the agent. */
  const held = (k: StepKey): string | undefined => {
    if (k === "sources" && created("source").length === 0) return "connect at least one source first";
    if (k === "watch" && created("trigger").length === 0) return "create a trigger first; the agent needs one to wake it";
    return undefined;
  };
  // every proposal of every step reached, in the order the assistant made them
  const allProposals = STEPS.filter((_, i) => i <= stepIndex && i < STEPS.length)
    .flatMap((s) => states[s.key].turns.flatMap((t) => t.parts)
      .filter((p): p is { type: "proposal"; proposal: Proposal } => p.type === "proposal")
      .map((p) => ({ p: p.proposal, stepKey: s.key })));
  const reveal = (id: string) => {
    setExpanded((x) => ({ ...x, [id]: true }));
    requestAnimationFrame(() => document.getElementById(`card-${id}`)?.scrollIntoView({ behavior: "smooth", block: "nearest" }));
  };
  const proposalsInStep = step !== "describe" && step !== "done"
    ? states[step].turns.flatMap((t) => t.parts).filter((p) => p.type === "proposal").length
    : 0;
  // A turn with text and no cards is the assistant asking: make the box read as the answer box.
  const lastTurn = step !== "describe" && step !== "done" ? states[step].turns[states[step].turns.length - 1] : undefined;
  const asking = !!lastTurn && !streaming && lastTurn.parts.some((p) => p.type === "text")
    && !lastTurn.parts.some((p) => p.type === "proposal");

  const running = stepIndex >= 0 && step !== "done";
  const stepsStrip = (
    <div className="builder-steps">
      {STEPS.map((s, i) => (
        <span key={s.key} className={"step" + (i === stepIndex ? " active" : i < stepIndex ? " done" : "") + (i === stepIndex && streaming ? " busy" : "")}>
          <span className="n">{i + 1}</span>{s.label}
          {i < stepIndex && s.kinds.some((k) => created(k).length > 0) && (
            <span className="badge ok">{s.kinds.reduce((n, k) => n + created(k).length, 0)}</span>
          )}
        </span>
      ))}
    </div>
  );
  return (
    <>
      {!running && <PageHead />}
      {!running && stepsStrip}

      {/* Describe: the form until the flow starts (and again on Change); a header afterwards */}
      {step === "describe" ? (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Describe</h2>
          <label className="field">
            <span className="lbl">project name<span className="req"> *</span></span>
            <input type="text" value={projectName} onChange={(e) => setProjectName(e.target.value)}
                   disabled={!!project} placeholder="e.g. checkout incidents" style={{ maxWidth: 420 }} />
            {handoff.goal && !project && <span className="help">proposed from what you typed; change it any time before the first object is created</span>}
          </label>
          <label className="field">
            <span className="lbl">what you need<span className="req"> *</span></span>
            <textarea value={goal} onChange={(e) => setGoal(e.target.value)} rows={3} autoFocus={changing}
                      placeholder="e.g. watch my payment service logs and wake an agent in Slack when checkouts fail"
                      style={{ width: "100%", boxSizing: "border-box" }} />
            <span className="help">
              Name the systems you run and where they are. Tares proposes sources from the connectors
              installed here, then views, triggers and an agent, one step at a time. Everything you
              create is a real object you can edit on its own page.
            </span>
          </label>
          {changing && objects.length > 0 && (
            <div className="alert">
              Starting again from the goal. What you already created stays and stays in the project:{" "}
              {objects.map((o) => <span key={`${o.kind}:${o.name}`} className="chip mono" style={{ marginRight: 4 }}>{o.name}</span>)}
              Proposals you had not applied are dropped.
            </div>
          )}
          <div className="btnrow">
            <button className="primary" disabled={!goal.trim() || !projectName.trim() || streaming}
                    onClick={changing ? restart : start}>
              {changing ? "Propose again" : "Propose sources"}
            </button>
            {changing
              ? <button onClick={() => { setChanging(false); setStep(lastStep.current); }}>Keep going instead</button>
              : <Link className="btn" to="/projects/new">Cancel</Link>}
          </div>
        </div>
      ) : running ? null : (
        <div className="panel builder-brief">
          <label className="field" style={{ marginBottom: 10 }}>
            <span className="lbl">project name</span>
            {project
              ? <span className="mono">{project.name}</span>
              : <input type="text" value={projectName} onChange={(e) => setProjectName(e.target.value)} style={{ maxWidth: 360 }} />}
          </label>
          <div className="field" style={{ marginBottom: 10 }}>
            <span className="lbl">building</span>
            <blockquote>{goal.trim()}</blockquote>
          </div>
          {step !== "done" && <button onClick={change}>Change what you need</button>}
        </div>
      )}

      {!running && projectErr && <div className="alert error">{projectErr}</div>}

      {/* The running builder fills the viewport: a one-line head (steps, the brief), then the
          conversation on the left and everything it proposes on the right as cards to complete
          or skip, each column scrolling on its own with the answer box pinned under the chat.
          A decided card folds to one line so the right side is always the work still to do. */}
      {running && (
      <div className="builder-run">
        <div className="builder-head">
          <h1>Build with Tares</h1>
          {stepsStrip}
          <div className="brief" title={goal.trim()}>
            {project
              ? <span className="mono">{project.name}</span>
              : <input type="text" value={projectName} onChange={(e) => setProjectName(e.target.value)} />}
            <span className="goal">{goal.trim()}</span>
          </div>
          <button type="button" onClick={change}>Change what you need</button>
        </div>
        {projectErr && <div className="alert error" style={{ margin: "8px 36px 0" }}>{projectErr}</div>}
      <div className="builder-split">
      <div className="builder-chat">
      <div className="builder-log">
      {STEPS.filter((_, i) => i <= stepIndex && i < STEPS.length).map((s, i) => (
        <div className="builder-step" key={s.key}>
          <div className="pagehead" style={{ marginBottom: 4 }}>
            <h3 style={{ margin: 0 }}>{s.label}</h3>
            {i < stepIndex && <span className="badge ok">done</span>}
            {s.key === step && streaming && (
              <span className="builder-running">
                <span className="spinner" /> proposing {s.label.toLowerCase()}…
                <button type="button" className="dim" onClick={stop}>Stop</button>
              </span>
            )}
          </div>
          {states[s.key].turns.map((t, j) => (
            <TurnText key={j} turn={t} decisions={decisions} reveal={reveal}
                      thinking={streaming && s.key === step && j === states[s.key].turns.length - 1 && t.parts.length === 0} />
          ))}
          {s.key === step && !streaming && states[s.key].turns.length > 0 && proposalsInStep === 0 && !asking && (
            <div className="empty">
              {s.key === "sources"
                ? <>No source was proposed. Say which systems you run and where (a container name, a URL, a repo), or <Link to="/sources/new">add a source by hand</Link> and come back to assemble the project from <Link to="/projects/new/custom">existing objects</Link>.</>
                : <>Nothing was proposed for this step. Ask for what you have in mind below, or continue.</>}
            </div>
          )}
          {s.key === step && s.key === "watch" && quiet.length > 0 && !streaming && (
            <div className="alert">
              {quiet.length === 1 ? <>Source <span className="mono">{quiet[0]}</span> has</> : <>Sources <span className="mono">{quiet.join(", ")}</span> have</>}{" "}
              no events yet. Views and triggers are proposed from real events, so send one first
              (the ingest URL is on the source page), then ask again below.
            </div>
          )}
          {s.key === step && s.key === "watch" && created("trigger").length === 0 && !streaming && (
            <p className="help" style={{ marginTop: 8 }}>The agent step needs a trigger to wake the agent. Create one here first.</p>
          )}
        </div>
      ))}
      <div ref={chatEnd} />
      </div>
      {((active: StepKey) => (
        <div className="builder-compose">
          <div className="builder-refine">
            <textarea value={refine} rows={asking ? 2 : 1} autoFocus={asking}
                      placeholder={asking ? "answer here, then Send" : pending > 0 ? "decide the cards on the right, or ask for a change" : "ask for a change, e.g. use the staging URL, or add the alerts too"}
                      onChange={(e) => setRefine(e.target.value)}
                      onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendRefine(); } }} />
            <button type="button" disabled={streaming || !refine.trim()} onClick={sendRefine}>Send</button>
          </div>
          <div className="btnrow" style={{ marginTop: 10 }}>
            <button className="primary" disabled={streaming || held(active) !== undefined}
                    onClick={advance} title={held(active)}>
              {NEXT[active] === "done" ? "Finish" : `Continue to ${STEPS[stepIndex + 1].label.toLowerCase()}`}
              {pending > 0 ? ` (${pending} undecided)` : ""}
            </button>
            {project && <Link className="btn" to={`/projects/${encodeURIComponent(project.id)}`}>Open the project so far</Link>}
          </div>
        </div>
      ))(step as StepKey)}
      </div>

      <div className="builder-actions">
        <div className="pagehead" style={{ marginBottom: 8 }}>
          <h2 style={{ margin: 0 }}>To do</h2>
          {pending > 0 && <span className="badge paused">{pending} to decide</span>}
        </div>
        {allProposals.length === 0 && (
          <div className="empty">Each source, view, trigger and agent the assistant proposes appears here for you to complete or skip.</div>
        )}
        {allProposals.map(({ p, stepKey }) => {
          const d = decisions[p.id];
          const folded = !!d && d.status !== "error" && !expanded[p.id];
          if (folded) {
            return (
              <div key={p.id} className="action-folded" id={`card-${p.id}`}>
                <span className="title">{proposalTitle(p)}</span>
                {d.status === "applied" ? <span className="badge ok">applied</span> : <span className="badge starting">skipped</span>}
                <button type="button" className="dim" onClick={() => setExpanded((x) => ({ ...x, [p.id]: true }))}>show</button>
              </div>
            );
          }
          return (
            <div key={p.id} id={`card-${p.id}`}>
              {d && d.status !== "error" && (
                <div className="btnrow" style={{ justifyContent: "flex-end", marginTop: 10 }}>
                  <button type="button" className="dim" onClick={() => setExpanded((x) => ({ ...x, [p.id]: false }))}>fold</button>
                </div>
              )}
              <CardFor proposal={p} decision={d} decide={decide} own={own} active={stepKey === step}
                       specs={specs ?? {}} existing={existing} refreshSources={refreshSources}
                       sourceNames={[...new Set([...created("source"), ...existing.map((x) => x.name)])]}
                       createdTriggers={created("trigger")} finishWith={finishWith} />
            </div>
          );
        })}
      </div>
      </div>
      </div>
      )}

      {step === "done" && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Done</h2>
          {project ? (
            <>
              <p>
                <strong>{project.name}</strong> is set up with{" "}
                {(["source", "view", "trigger", "agent"] as ProjectObjectKind[])
                  .map((k) => ({ k, n: created(k).length })).filter((x) => x.n > 0)
                  .map((x, i, arr) => <span key={x.k}>{i > 0 ? (i === arr.length - 1 ? " and " : ", ") : ""}{x.n} {x.k}{x.n === 1 ? "" : "s"}</span>)}.
                The project page shows its objects, firings and agent runs.
              </p>
              {finishedTemplate?.setup && finishedTemplate.setup.length > 0 && (
                <>
                  <h3 style={{ margin: "14px 0 6px" }}>What happens next</h3>
                  <ol className="uc-setup">
                    {finishedTemplate.setup.map((st, i) => (
                      <li key={i}>
                        <div className="uc-setup-title">{st.title}</div>
                        {st.text && <p className="help" style={{ margin: "2px 0 6px", whiteSpace: "normal" }}>{st.text}</p>}
                        {st.check === "anthropic_key" && <p className="help" style={{ margin: "2px 0 6px" }}>Under <Link to="/settings">Settings</Link>, if none is set yet.</p>}
                        {st.command && (
                          <div className="uc-setup-cmd">
                            <pre className="mono">{st.command}</pre>
                            <button type="button" onClick={() => navigator.clipboard?.writeText(st.command!)}>copy</button>
                          </div>
                        )}
                      </li>
                    ))}
                  </ol>
                </>
              )}
              <div className="btnrow">
                <Link className="btn primary" to={`/projects/${encodeURIComponent(project.id)}`}>Open the project</Link>
              </div>
            </>
          ) : (
            <p className="help">Nothing was created. Start again with a fuller description, or <Link to="/projects/new/templates">pick a template</Link>.</p>
          )}
        </div>
      )}
    </>
  );
}

function PageHead() {
  return (
    <div className="pagehead">
      <div>
        <h1>Build with Tares</h1>
        <p className="subtitle">
          describe what you need; Tares proposes the sources, views, triggers and agent, and you
          confirm each one in place. If you leave part way, what you created so far stays, real
          and editable on its own page; the project page is where to pick it up.
        </p>
      </div>
    </div>
  );
}

/** One assistant turn inside a step: text, the tool rail, and each proposal as a card the user
 *  completes. Mirrors AskChat's Turn, with forms in place of Apply for sources and agents. */
/** One assistant turn as conversation: its text and tool rail, and for each proposal a line
 *  that points at its card on the right, with where that card stands. */
function TurnText({ turn, decisions, reveal, thinking }: {
  turn: Turn; decisions: DecisionMap; thinking: boolean; reveal: (id: string) => void;
}) {
  const blocks: ({ kind: "tools"; tools: ToolPart[] } | { kind: "part"; part: Part })[] = [];
  for (const p of turn.parts) {
    const last = blocks[blocks.length - 1];
    if (p.type === "tool" && last && last.kind === "tools") last.tools.push(p);
    else if (p.type === "tool") blocks.push({ kind: "tools", tools: [p] });
    else blocks.push({ kind: "part", part: p });
  }
  return (
    <div className="turn assistant" style={{ marginBottom: 8 }}>
      {blocks.map((b, j) => {
        if (b.kind === "tools") return <ToolRun key={j} tools={b.tools} />;
        if (b.part.type === "text")
          return <div key={j} className="md"><ReactMarkdown remarkPlugins={[remarkGfm]}>{b.part.text}</ReactMarkdown></div>;
        if (b.part.type !== "proposal") return null;
        const p = b.part.proposal;
        const d = decisions[p.id];
        return (
          <button key={j} type="button" className={"proposal-ref" + (d ? " " + d.status : "")} onClick={() => reveal(p.id)}>
            <span>{proposalTitle(p)}</span>
            <span className="state">{d?.status === "applied" ? "applied" : d?.status === "skipped" ? "skipped" : d?.status === "error" ? "failed" : "to decide"}</span>
          </button>
        );
      })}
      {thinking && <div className="dim">thinking…</div>}
    </div>
  );
}

/** The card for one proposal: the form or the apply card its kind needs. */
function CardFor({ proposal: p, decision, decide, own, active, specs, existing, refreshSources, sourceNames,
                   createdTriggers, finishWith }: CardCommon & {
  proposal: Proposal; specs: Record<string, ConnectorSpec>; existing: Source[]; refreshSources: () => void;
  sourceNames: string[]; createdTriggers: string[]; finishWith: (p: Project) => void;
}) {
  const common = { decision, decide, own, active };
  if (p.kind === "source")
    return <SourceCard proposal={p} specs={specs} existing={existing} refreshSources={refreshSources} {...common} />;
  if (p.kind === "agent")
    return <AgentCard proposal={p} triggers={createdTriggers} {...common} />;
  if (p.kind === "project")
    return <ProjectCard proposal={p} finishWith={finishWith} {...common} />;
  return <CatalogCard proposal={p} sourceNames={sourceNames} {...common} />;
}
type CardCommon = {
  decision?: DecisionMap[string]; active: boolean;
  decide: (id: string, status: "applied" | "skipped" | "error", detail?: string) => void;
  own: (kind: ProjectObjectKind, name: string) => Promise<void>;
};

/** A proposed source as the connector form, prefilled, the `needs` fields highlighted. Test and
 *  Create are the form's own; on create the project adopts the source. */
function SourceCard({ proposal: p, specs, existing, refreshSources, decision, decide, own }: CardCommon & {
  proposal: Extract<Proposal, { kind: "source" }>;
  specs: Record<string, ConnectorSpec>; existing: Source[]; refreshSources: () => void;
}) {
  const known = Object.keys(specs).filter((k) => !specs[k].internal).sort();
  const [connector, setConnector] = useState(p.connector);
  const spec = specs[connector];
  // After Create the form goes away, but a push source is useless until something posts to it:
  // keep the ingest URL, the setup snippet and a link to the source on the card. Same show-once
  // ingest key as the Sources page mints on a secured instance.
  const [made, setMade] = useState<{ name: string; connector: string; ingestKey: string; authKey?: string; keyErr?: string }>();
  const [authOn, setAuthOn] = useState(false);
  useEffect(() => { api.health().then((h) => setAuthOn(h.auth_required)).catch(() => {}); }, []);
  const unknown = Object.keys(specs).length > 0 && !specs[p.connector];
  // the same name, unowned: adopt it instead of creating a second one
  const twin = existing.find((s) => s.name === p.name && !s.owned_by);

  // prefill from the proposal only while the connector is the proposed one; a switched
  // connector starts from its own defaults. Fields in `needs` stay empty whatever the model sent.
  const initial = useMemo(() => {
    if (!spec) return undefined;
    const config: Record<string, unknown> = {};
    for (const f of spec.fields) {
      if (f.secret || p.needs.includes(f.name)) continue;
      if (connector === p.connector && p.config && p.config[f.name] !== undefined) config[f.name] = p.config[f.name];
      else if (f.default != null && (f.type === "string" || f.type === "number")) config[f.name] = f.default;
    }
    return { name: p.name, type: "event_stream", poll: p.poll ?? spec.poll ?? "5s", config };
  }, [spec, connector]);   // eslint-disable-line react-hooks/exhaustive-deps

  const adopt = async () => {
    try { await own("source", p.name); decide(p.id, "applied"); }
    catch (e) { decide(p.id, "error", String((e as Error).message ?? e)); }
  };

  return (
    <ProposalShell title={proposalTitle(p)} decision={decision} reasoning={p.reasoning}
                   actions={(!decision || decision.status === "error") ? (
                     <div className="btnrow"><button onClick={() => decide(p.id, "skipped")}>Skip</button></div>
                   ) : null}>
      <ProposalBody proposal={p} />
      {decision?.status === "applied" && (
        <CreatedSource made={made} name={p.name} specs={specs} />
      )}
      {(!decision || decision.status === "error") && (
        <>
          {unknown && (
            <div className="alert error">
              The connector <span className="mono">{p.connector}</span> is not installed here. Pick one below.
            </div>
          )}
          {twin && (
            <div className="alert">
              A source named <span className="mono">{twin.name}</span> already exists and is not part of a project.{" "}
              <button type="button" onClick={adopt}>Use the existing source</button>
            </div>
          )}
          <div className="field">
            <span className="lbl">connector</span>
            <Picker value={connector} onChange={setConnector} options={known}
                    labels={Object.fromEntries(known.map((k) => [k, specs[k].label ?? k]))} />
          </div>
          {spec && initial && (
            <SourceForm key={connector} connector={connector} spec={spec} initial={initial}
                        highlight={connector === p.connector ? p.needs : undefined}
                        submitLabel="Create source"
                        onSubmit={async (body) => {
                          const res = await api.createSource(body);
                          refreshSources();
                          let authKey: string | undefined, keyErr: string | undefined;
                          if (spec.mode === "push" && authOn) {
                            try { authKey = (await api.createKey(`ingest: ${body.name}`, ["ingest"])).secret; }
                            catch (e) { keyErr = String((e as Error).message ?? e); }
                          }
                          setMade({ name: body.name, connector: body.connector, ingestKey: res.ingest_key || body.name, authKey, keyErr });
                          try { await own("source", body.name); }
                          catch (e) { decide(p.id, "error", String((e as Error).message ?? e)); return; }
                          decide(p.id, "applied");
                        }} />
          )}
        </>
      )}
    </ProposalShell>
  );
}

/** What an applied source card keeps showing: where it lives, and for a push source the ingest
 *  URL and setup snippet, because nothing arrives until a producer posts to it. */
function CreatedSource({ made, name, specs }: {
  made?: { name: string; connector: string; ingestKey: string; authKey?: string; keyErr?: string };
  name: string; specs: Record<string, ConnectorSpec>;
}) {
  const [copied, setCopied] = useState(false);
  const n = made?.name ?? name;
  const push = made ? specs[made.connector]?.mode === "push" : false;
  const url = made ? (made.connector === "otlp" ? `${window.location.origin}/v1/logs` : `${window.location.origin}/ingest/${made.ingestKey}`) : "";
  return (
    <div style={{ margin: "0 0 10px" }}>
      <p className="help" style={{ margin: "0 0 6px" }}>
        Created. <Link to={`/sources/${encodeURIComponent(n)}`}>Open the source</Link>
        {push ? " to see events as they arrive." : " to see its first poll."}
      </p>
      {push && made && (
        <>
          <div className="field">
            <span className="lbl">send events here</span>
            <span className="mono" style={{ wordBreak: "break-all" }}>{url}</span>{" "}
            <button type="button" onClick={() => { navigator.clipboard?.writeText(url); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>
              {copied ? "copied" : "copy"}
            </button>
          </div>
          {made.keyErr && <div className="alert error">could not mint an ingest key: {made.keyErr}. Create one under Settings.</div>}
          <IngestSetup connector={made.connector} url={url} authKey={made.authKey} />
        </>
      )}
    </div>
  );
}

/** A whole project from a template, as the template's own form prefilled with what the assistant
 *  knew and the `needs` params marked. Start creates it through the normal create API, so the
 *  template's own logic runs; the build is then complete. */
function ProjectCard({ proposal: p, decision, decide, finishWith }: CardCommon & {
  proposal: Extract<Proposal, { kind: "project" }>; finishWith: (p: Project) => void;
}) {
  const [template, setTemplate] = useState<Template>();
  const [err, setErr] = useState<string>();
  const [name, setName] = useState(p.name);
  const [vals, setVals] = useState<Record<string, string>>({});
  const [models, setModels] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [detected, setDetected] = useState<Detected>();
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.template(p.template).then((t) => {
      setTemplate(t);
      setVals(Object.fromEntries(Object.entries(t.params).map(([k, spec]) =>
        [k, p.needs.includes(k) ? "" : paramText(spec, p.params?.[k])])));
    }).catch((e) => setErr(String((e as Error).message ?? e)));
    api.builtinAgents().then((r) => { setModels(r.models); setDefaultModel(r.default_model); }).catch(() => {});
    api.detectRecipe(p.template).then(setDetected).catch(() => {});
  }, [p.id]);   // eslint-disable-line react-hooks/exhaustive-deps

  const start = async () => {
    if (!template) return;
    setBusy(true); setErr(undefined);
    try {
      const made = await api.createProject({ template: template.key, name: name.trim() || undefined, params: paramsBody(template, vals) });
      decide(p.id, "applied");
      finishWith(made);
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  const open = !decision || decision.status === "error";
  return (
    <ProposalShell title={proposalTitle(p)} decision={decision} reasoning={p.reasoning}
                   actions={open ? <div className="btnrow"><button onClick={() => decide(p.id, "skipped")}>Skip</button></div> : null}>
      <ProposalBody proposal={p} />
      {err && <div className="alert error">{err}</div>}
      {open && template && (
        <>
          <p className="help" style={{ whiteSpace: "normal" }}>{template.description}</p>
          <label className="field">
            <span className="lbl">name</span>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} style={{ maxWidth: 420 }} />
          </label>
          <TemplateParams template={template} vals={vals} setVals={setVals} needs={p.needs}
                          models={models} defaultModel={defaultModel} detected={detected} />
          <div className="btnrow" style={{ alignItems: "center" }}>
            <button className="primary" disabled={busy} onClick={start}>{busy ? "starting…" : "Start"}</button>
            <span className="help">
              creates the project with everything the template sets up
              {template.setup && template.setup.length > 0 ? `, then ${template.setup.length} steps to do on your side` : ""}
            </span>
          </div>
        </>
      )}
      {open && !template && !err && <div className="dim">loading the template…</div>}
    </ProposalShell>
  );
}

/** A proposed view or trigger, or labels for a source: Apply as is, or Edit in the object's own
 *  editor first. Either way the object is created through its API and appended to the project. */
function CatalogCard({ proposal: p, sourceNames, decision, decide, own }: CardCommon & {
  proposal: Extract<Proposal, { kind: "labels" | "view" | "trigger" }>;
  sourceNames: string[];
}) {
  const [editing, setEditing] = useState(false);
  const kind = kindOf(p);
  const settle = async (name: string) => {
    if (kind) {
      try { await own(kind, name); }
      catch (e) { decide(p.id, "error", String((e as Error).message ?? e)); return; }
    }
    decide(p.id, "applied");
  };
  const apply = async () => {
    try { await applyProposal(p); }
    catch (e) { decide(p.id, "error", String((e as Error).message ?? e)); return; }
    await settle(p.kind === "labels" ? p.source : p.name);
  };
  const open = !decision || decision.status === "error";
  return (
    <ProposalShell title={proposalTitle(p)} decision={decision} reasoning={p.reasoning}
                   actions={open ? (
                     <div className="btnrow">
                       {!editing && <button className="primary" onClick={apply}>{decision?.status === "error" ? "Retry" : "Apply"}</button>}
                       {p.kind !== "labels" && <button onClick={() => setEditing((e) => !e)}>{editing ? "Close editor" : "Edit"}</button>}
                       <button onClick={() => decide(p.id, "skipped")}>Skip</button>
                     </div>
                   ) : null}>
      <ProposalBody proposal={p} />
      {open && editing && p.kind === "view" && (
        <ViewEditor prefill sourceNames={sourceNames}
                    initial={{ name: p.name, key_field: p.key_field, sources: p.sources,
                               filters: (p.filters ?? []) as unknown as ViewFilter[] } as View}
                    onSaved={settle} onCancel={() => setEditing(false)} />
      )}
      {open && editing && p.kind === "trigger" && (
        <TriggerEditor prefill presetView={p.view}
                       initial={{ name: p.name, view: p.view, condition: p.condition,
                                  emit: { kind: p.emit?.kind ?? p.name, context_window: p.emit?.context_window ?? "15m" },
                                  cooldown: p.cooldown ?? "5m" } as Trigger}
                       onSaved={settle} onCancel={() => setEditing(false)} />
      )}
    </ProposalShell>
  );
}

/** The proposed agent as the agent form, prefilled. Create, then enable (which checks a key
 *  resolves), then the project adopts it. */
function AgentCard({ proposal: p, triggers, decision, decide, own }: CardCommon & {
  proposal: Extract<Proposal, { kind: "agent" }>; triggers: string[];
}) {
  const [bundle, setBundle] = useState<{ presets: AgentPreset[]; models: string[]; default_model: string;
    slack_workspace: boolean; default_max_rounds: number; default_max_rounds_with_mcp: number;
    max_rounds_limit: number; key_configured: boolean }>();
  const [allTriggers, setAllTriggers] = useState<string[]>();
  const [note, setNote] = useState<string>();
  useEffect(() => {
    api.builtinAgents().then(setBundle).catch(() => {});
    api.triggers().then((ts) => setAllTriggers(ts.map((t) => t.name))).catch(() => setAllTriggers(triggers));
  }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  const open = !decision || decision.status === "error";
  const triggerNames = [...new Set([...(allTriggers ?? []), ...triggers])];
  // a trigger the model named but that does not exist is not prefilled: the form would submit
  // it as is and the daemon would refuse it every time
  const known = triggerNames.includes(p.trigger);
  const initial: BuiltinAgent = {
    name: p.name, trigger: known ? p.trigger : "", prompt: p.prompt, enabled: false, slack_configured: false,
    model: p.model ?? "", slack_channel: "",
    webhook_url: p.delivery.kind === "webhook" ? (p.delivery.url ?? "") : "", webhook_token_configured: false,
    mcp_servers: [], max_rounds: p.max_rounds ?? null, budget_usd: p.budget_usd ?? null,
    effective_max_rounds: p.max_rounds ?? 6,
  };
  return (
    <ProposalShell title={proposalTitle(p)} decision={decision} reasoning={p.reasoning}
                   actions={open ? <div className="btnrow"><button onClick={() => decide(p.id, "skipped")}>Skip</button></div> : null}>
      <ProposalBody proposal={p} />
      {note && <div className="alert">{note}</div>}
      {open && allTriggers && !known && (
        <div className="alert">
          {triggerNames.length > 0
            ? <>The proposed trigger <span className="mono">{p.trigger}</span> does not exist; pick one of yours in the form.</>
            : <>This project has no trigger yet, and an agent needs one to wake it. Go back to Views and triggers and create one first.</>}
        </div>
      )}
      {open && bundle && allTriggers && (
        <AgentForm prefill deliveryKind={p.delivery.kind} initial={initial}
                   presetTrigger={known ? p.trigger : undefined}
                   triggers={triggerNames} presets={bundle.presets} models={bundle.models}
                   defaultModel={bundle.default_model} slackWorkspace={bundle.slack_workspace}
                   defaultMaxRounds={bundle.default_max_rounds} defaultMaxRoundsWithMcp={bundle.default_max_rounds_with_mcp}
                   maxRoundsLimit={bundle.max_rounds_limit}
                   onSaved={async (name) => {
                     // enable through the same endpoint the agent page uses: it refuses without a
                     // key, so the agent cannot look enabled and then fail on the first firing
                     try { await api.enableBuiltinAgent(name); }
                     catch (e) { setNote(`Created, but not enabled: ${String((e as Error).message ?? e)}. Enable it on the agent's page once a key is set.`); }
                     try { await own("agent", name); }
                     catch (e) { decide(p.id, "error", String((e as Error).message ?? e)); return; }
                     decide(p.id, "applied");
                   }}
                   onCancel={() => decide(p.id, "skipped")} />
      )}
      {open && (!bundle || !allTriggers) && <div className="dim">loading the agent form…</div>}
    </ProposalShell>
  );
}
