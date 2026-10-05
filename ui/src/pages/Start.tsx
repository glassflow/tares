import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { ReadyItem, useOwnProjects, useReadiness } from "../components/readiness";
import { GoalStep, type GoalWords, type Who } from "../components/setup/goal";
import { usePolling } from "../components/bits";
import { CLOUD_BACK } from "./Security";
import { startDraft } from "./ProjectSetup";
import type { Project } from "../types";

// Start: where a new cell opens, until the person has a project of their own (then / is
// Projects). A short list read from the cell's state (the demo, a model provider, GitHub, Slack),
// every row optional, and the first project's goal right under it: the same goal step as
// /projects/new, so the product asks for a goal in one place, and "Plan it" goes straight to the
// plan. Connect on Tares Cloud comes back here and the row ticks itself.

export default function Start() {
  const navigate = useNavigate();
  const { projects, reload: reloadProjects } = useOwnProjects(15000);
  const { rows } = useReadiness("/start");
  const [words, setWords] = useState<GoalWords>({ goal: "", name: "", about: "" });
  const [who, setWho] = useState<Who>("tares");

  // back from a Tares Cloud connect page: say what happened once, then drop it from the address
  const [back] = useState(() => {
    const q = new URLSearchParams(window.location.search);
    const m = CLOUD_BACK[q.get("cloud") ?? ""];
    return m ? { ...m, detail: q.get("cloud_detail") ?? "" } : undefined;
  });
  useEffect(() => {
    if (back) window.history.replaceState(null, "", "/start");
  }, [back]);

  const drafts = projects?.filter((p) => p.status === "draft") ?? [];

  return (
    <div className="start">
      <div className="su-head">
        <h1 className="su-title">Welcome to Tares</h1>
        <p className="su-sub">See an agent at work, then connect your own systems. Every row is optional.</p>
      </div>

      {back && (
        <div className={"alert" + (back.kind ? ` ${back.kind}` : "")} role={back.kind === "error" ? "alert" : "status"}>
          {back.kind === "error" && back.detail ? back.detail : <>{back.text}{back.detail && <> {back.detail}</>}</>}
        </div>
      )}

      <ul className="ready-list">
        <DemoRow projects={projects} onChange={reloadProjects} />
        {rows.map((r) => <ReadyItem key={r.key} done={r.done} title={r.title} text={r.text} action={r.action} />)}
      </ul>

      {drafts.length > 0 && (
        <p className="help">
          Being set up: {drafts.map((d, i) => (
            <span key={d.id}>{i > 0 && ", "}<Link to={`/projects/${encodeURIComponent(d.id)}/setup`}>{d.name}</Link></span>
          ))}. Pick up where you stopped, or start another below.
        </p>
      )}

      <GoalStep start words={words} setWords={setWords} who={who} setWho={setWho}
                onSubmit={async (g, w) => {
                  const id = await startDraft(g, w);
                  navigate(`/projects/${encodeURIComponent(id)}/setup`);
                }} />

      <p className="help">
        Prefer to pick the parts yourself? <Link to="/projects/new/templates">Start from a template</Link>, or{" "}
        <Link to="/projects/new/custom">build it by hand from existing objects</Link>.
      </p>
    </div>
  );
}

/** The demo, first on the list: an offer, never started on its own. One click creates it from
 *  what detection finds (on Tares Cloud the hosted demo stack) and opens its page; when the stack
 *  is not reachable, the step-by-step setup takes over with the steps to start it. */
function DemoRow({ projects, onChange }: { projects: Project[] | undefined; onChange: () => void }) {
  const navigate = useNavigate();
  const { data: rec } = usePolling(() => api.templates(), 60000);
  const template = rec?.templates.find((t) => t.key === "ai_sre_demo");
  const demo = projects?.find((p) => p.template === "ai_sre_demo");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [confirm, setConfirm] = useState(false);
  if (!template || !projects) return null;

  const start = async () => {
    setBusy(true); setErr(undefined);
    try {
      const d = await api.detectRecipe(template.key);
      const required = Object.entries(template.params).filter(([, p]) => p.required).map(([k]) => k);
      if (required.some((k) => d.params[k] === undefined || d.params[k] === "")) {
        navigate(`/projects/new/${template.key}`);
        return;
      }
      const made = await api.createProject({ template: template.key, params: d.params });
      navigate(`/projects/${encodeURIComponent(made.id)}`);
    } catch (e) {
      setErr(String((e as Error).message ?? e));
      setBusy(false);
    }
  };
  // the demo's own sources go with it; one another project uses is kept (the daemon decides)
  const remove = async () => {
    if (!demo) return;
    setConfirm(false); setBusy(true); setErr(undefined);
    try {
      await api.deleteProject(demo.id, true, demo.objects.filter((o) => o.kind === "source").map((o) => o.name));
      onChange();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const text = err
    ? <>Could not start the demo: {err}. <Link to={`/projects/new/${template.key}`}>Set it up step by step</Link> instead.</>
    : demo
      ? "Cause an incident from its page and watch the agent write the incident note."
      : "A small live service, an agent watching it, and a button to break it. Ready in a few seconds.";
  return (
    <>
      <ReadyItem done={!!demo} title={demo ? "Demo running" : "Watch an agent handle an incident"} text={text}
                 action={demo
                   ? <span className="btnrow">
                       <Link className="btn" to={`/projects/${encodeURIComponent(demo.id)}`}>Open the demo</Link>
                       <button type="button" className="dim" disabled={busy} onClick={() => setConfirm(true)}>Remove</button>
                     </span>
                   : <button type="button" disabled={busy} onClick={start}>{busy ? "Starting…" : "Start the demo"}</button>} />
      {confirm && (
        <ConfirmDialog title="Remove the demo?" danger confirmLabel="Remove"
                       message="Its agent stops, and its sources and their events are deleted. You can start it again from here."
                       onConfirm={remove} onCancel={() => setConfirm(false)} />
      )}
    </>
  );
}
