import { useEffect, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../../api";
import { ResultCard } from "../project/overview";
import type { AgentRun, Plan, ProjectResult, ProjectResultDetail, SetupChecks } from "../../types";
import { errText } from "./common";
import { useSeconds } from "./planning";

// Step 4: one practice run ("Run it once now"), so the person sees a result before a real one. Tares agents: the
// first agent woken by a trigger runs on the example event, and its chain concludes as usual.
// Own agent: a practice wake-up goes to its subscriptions and the result is the finding it
// records next. Either way the result is marked practice and left out of today's totals.

const POLL_MS = 3000;
const GIVE_UP_MS = 5 * 60 * 1000;
const STOPPED: AgentRun["status"][] = ["failed", "capped", "exhausted"];

type Phase = "idle" | "starting" | "waiting" | "done" | "stopped" | "slow";

export function TryStep({ projectId, plan, ownCheck, onBackToConnect, onFinish, finishing }: {
  projectId: string; plan: Plan; ownCheck: SetupChecks["own_agent"] | undefined;
  onBackToConnect: () => void; onFinish: () => void; finishing: boolean;
}) {
  const navigate = useNavigate();
  const own = plan.who === "own";
  const first = plan.agents.find((a) => a.enabled && a.on_trigger);
  const firstName = first?.name;
  const [phase, setPhase] = useState<Phase>("idle");
  const [err, setErr] = useState<string>();
  const [runId, setRunId] = useState<string>();
  const [result, setResult] = useState<ProjectResult>();
  const [detail, setDetail] = useState<ProjectResultDetail>();
  const [startedAt, setStartedAt] = useState<string>();
  const started = useRef(0);
  const secs = useSeconds(phase === "waiting" || phase === "slow" ? startedAt : undefined);

  const start = async () => {
    setPhase("starting"); setErr(undefined); setResult(undefined); setDetail(undefined);
    started.current = Date.now();
    setStartedAt(new Date().toISOString());
    try {
      const r = await api.runSetupPractice(projectId);
      setRunId(r.run_id);
      setPhase("waiting");
    } catch (e) { setErr(errText(e)); setPhase("idle"); }
  };

  // Wait for the practice result: the newest practice result since the run started. Tares agents
  // also watch the first run, so a run that stopped says so instead of waiting forever.
  useEffect(() => {
    if (phase !== "waiting") return;
    let live = true;
    const tick = async () => {
      try {
        const page = await api.projectResults(projectId, { limit: 10 });
        const hit = page.results.find((r) => r.practice && new Date(r.at).getTime() >= started.current - 10000);
        if (hit && live) {
          setResult(hit);
          setPhase("done");
          return;
        }
        if (!own && firstName && runId) {
          const runs = await api.builtinAgentRuns(firstName, 10);
          const run = runs.find((x) => x.id === runId);
          if (run && STOPPED.includes(run.status) && live) {
            setErr(run.error ?? `The run ended as ${run.status}.`);
            setPhase("stopped");
            return;
          }
        }
        if (Date.now() - started.current > GIVE_UP_MS && live) setPhase("slow");
      } catch { /* the next tick tries again */ }
    };
    tick();
    const id = window.setInterval(tick, POLL_MS);
    return () => { live = false; window.clearInterval(id); };
  }, [phase, projectId, runId, own, firstName]);

  // the steps of the result, once it is here (its own effect: the polling one is torn down as
  // soon as the result stops it)
  const resultId = result?.id;
  useEffect(() => {
    if (!resultId) return;
    let live = true;
    api.projectResult(projectId, resultId).then((d) => { if (live) setDetail(d); }).catch(() => {});
    return () => { live = false; };
  }, [projectId, resultId]);

  const tried = phase !== "idle" && phase !== "starting";
  const waitingText = own
    ? "Sent a practice wake-up to your agent. Waiting for its finding."
    : `${first?.sentence ?? "The first agent is looking."} Working on it now.`;

  return (
    <section className="su-section su-mid" aria-labelledby="su-try-h">
      <div className="su-head">
        <h1 id="su-try-h" className="su-title">See it work</h1>
        <p className="su-sub">
          {own
            ? "Send your agent a practice wake-up from the example event, so you see a finding before a real one. It is marked as practice and left out of today's totals."
            : "Runs the agent on the latest event, so you see a result before a real one arrives. It uses your model provider like a real run, is marked as practice and is left out of today's totals."}
        </p>
      </div>

      {own && ownCheck?.state !== "joined" && phase === "idle" && (
        <div className="alert warn">
          Your agent has not connected yet, so nothing may answer the wake-up.{" "}
          <button type="button" className="linklike" onClick={onBackToConnect}>Connect it first</button>
        </div>
      )}
      {!own && !first && (
        <div className="alert warn">No agent in this project is woken by a trigger, so there is nothing to practice.</div>
      )}

      {(phase === "idle" || phase === "starting") && (!own ? !!first : true) && (
        <div>
          <button type="button" className="primary su-cta" onClick={start} disabled={phase === "starting"}>
            {phase === "starting" ? "Starting…" : "Run it once now"}
          </button>
        </div>
      )}

      {tried && (
        <div className="su-section">
          <ol className="su-progress" aria-live="polite">
            {!detail && !result && (
              // a spinner and the seconds so far: it is working, not stuck
              <li className={phase === "stopped" ? "stopped" : "running"}>
                <span className="su-progress-mark" aria-hidden="true" />
                <span>
                  {waitingText}
                  {phase !== "stopped" && <span className="help"> · {secs} s</span>}
                </span>
              </li>
            )}
            {detail?.steps.map((st, i) => (
              <li key={i} className="done"><span className="su-progress-mark" aria-hidden="true" /><span>{st.text}</span></li>
            ))}
            {phase === "slow" && <li>No result yet. It lands on the project page when it arrives.</li>}
          </ol>
          {result && (
            <ol className="gf-cards">
              <ResultCard r={result} onOpen={() => navigate(
                `/projects/${encodeURIComponent(projectId)}?view=result:${encodeURIComponent(result.id)}`)} />
            </ol>
          )}
        </div>
      )}

      {err && (
        <div className="alert error" role="alert">
          {phase === "stopped" ? <>The practice run stopped: {err}</> : err}{" "}
          {phase === "stopped" && <button type="button" className="linklike" onClick={start}>Try again</button>}
        </div>
      )}

      <div className="btnrow">
        <button type="button" className={tried || !first && !own ? "primary su-cta" : "su-cta"}
                onClick={onFinish} disabled={finishing}>
          {finishing ? "Opening…" : "Open the project"}
        </button>
      </div>
    </section>
  );
}
