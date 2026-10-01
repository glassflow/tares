import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { errStatus, errText } from "./common";

// Step 1: the goal in the person's own words and who does the work. "Plan it" hands both to the
// page (a new draft project, or the draft planned again); the planning itself is shown on the
// draft's page, step by step, and a missing model provider says where to add one.

const EXAMPLES = [
  "Catch checkout outages early and find the root cause",
  "Explain every Prometheus alert before I get paged",
  "Keep one context repo in step with my code",
  "Summarize each Claude Code session when it ends",
];

export type Who = "tares" | "own";

export function GoalStep({ goal, setGoal, who, setWho, onSubmit, onCancel }: {
  goal: string; setGoal: (g: string) => void;
  who: Who; setWho: (w: Who) => void;
  onSubmit: (goal: string, who: Who) => Promise<void>;
  /** Back to the plan the draft already has, unchanged. */
  onCancel?: () => void;
}) {
  const box = useRef<HTMLTextAreaElement>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<{ text: string; noProvider: boolean }>();
  useEffect(() => { box.current?.focus(); }, []);

  const plan = async () => {
    const g = goal.replace(/\s+/g, " ").trim();
    if (!g || busy) return;
    setBusy(true); setErr(undefined);
    try {
      await onSubmit(g, who);
    } catch (e) {
      setErr({ text: errText(e), noProvider: errStatus(e) === 409 });
      setBusy(false);
    }
  };

  return (
    <section className="su-section su-narrow" aria-labelledby="su-goal-h">
      <div className="su-head">
        <h1 id="su-goal-h" className="su-title">What should this project achieve?</h1>
        <p className="su-sub">Say it the way you would to a colleague. Tares plans the rest and shows you the plan before anything runs.</p>
      </div>

      <form className="su-section" onSubmit={(e) => { e.preventDefault(); plan(); }}>
        <label htmlFor="su-goal" className="sr-only">Goal</label>
        <textarea id="su-goal" ref={box} rows={3} className="su-goal-box" value={goal} disabled={busy}
                  placeholder="Catch checkout outages early and find the root cause"
                  onChange={(e) => setGoal(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); plan(); } }} />

        <div className="su-examples">
          <span className="help">Or start from one of these</span>
          <div className="su-chips">
            {EXAMPLES.map((x) => (
              <button key={x} type="button" className={`su-chip${goal.trim() === x ? " on" : ""}`}
                      aria-pressed={goal.trim() === x} disabled={busy}
                      onClick={() => { setGoal(x); box.current?.focus(); }}>{x}</button>
            ))}
          </div>
        </div>

        <fieldset className="su-who" disabled={busy}>
          <legend>Who does the work?</legend>
          <label className={`su-who-opt${who === "tares" ? " on" : ""}`}>
            <input type="radio" name="su-who" value="tares" checked={who === "tares"} onChange={() => setWho("tares")} />
            <span>
              <strong>Tares agents</strong>
              <span className="help">Agents that run inside Tares on your model provider. Nothing to host.</span>
            </span>
          </label>
          <label className={`su-who-opt${who === "own" ? " on" : ""}`}>
            <input type="radio" name="su-who" value="own" checked={who === "own"} onChange={() => setWho("own")} />
            <span>
              <strong>Your own agent</strong>
              <span className="help">Claude Code or a service of yours joins with a project key and is woken with what happened.</span>
            </span>
          </label>
        </fieldset>

        <div className="btnrow">
          <button type="submit" className="primary su-cta" disabled={busy || !goal.trim()}>
            {busy ? "Starting…" : "Plan it"}
          </button>
          {onCancel && (
            <button type="button" className="su-cta" onClick={onCancel} disabled={busy}>Keep the current plan</button>
          )}
        </div>
      </form>

      {err && (
        <div className="alert error" role="alert">
          {err.text}
          {err.noProvider && (
            <div className="btnrow" style={{ marginTop: 8 }}>
              <Link className="btn" to="/settings">Add a model provider</Link>
              <Link className="btn" to="/projects/new/templates">Start from a template</Link>
            </div>
          )}
        </div>
      )}

      <p className="help su-alt">
        Prefer to pick the parts yourself? <Link to="/projects/new/templates">Start from a template</Link>, or{" "}
        <Link to="/projects/new/custom">build it by hand from existing objects</Link>.
      </p>
    </section>
  );
}
