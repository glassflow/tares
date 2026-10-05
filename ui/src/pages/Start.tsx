import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api";
import { DemoRow, ReadyItem, useOwnProjects, useReadiness } from "../components/readiness";
import { GoalStep, type GoalWords, type Who } from "../components/setup/goal";
import { CLOUD_BACK } from "./Security";
import { startDraft } from "./ProjectSetup";

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
        {rows.map((r) => <ReadyItem key={r.key} done={r.done} title={r.title} text={r.text} action={r.action()} />)}
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
