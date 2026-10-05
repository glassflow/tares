import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../../api";
import { errStatus, errText } from "./common";

// Step 1: the goal in the person's own words, an optional name and description, and who does the
// work. "Plan it" hands them to the page (a new draft project, or the draft planned again); the
// planning itself is shown on the draft's page, step by step, and a missing model provider says
// where to add one. The Start page shows the same step under its list (`start`), so the product
// asks for a goal in one place.

const EXAMPLES = [
  "Catch checkout outages early and find the root cause",
  "Explain every Prometheus alert before I get paged",
  "Keep one context repo in step with my code",
  "Summarize each Claude Code session when it ends",
];

export type Who = "tares" | "own";

/** What the goal step asks for, besides who does the work. */
export type GoalWords = { goal: string; name: string; about: string };

export function GoalStep({ words, setWords, who, setWho, onSubmit, onCancel, start }: {
  words: GoalWords; setWords: (w: GoalWords) => void;
  who: Who; setWho: (w: Who) => void;
  onSubmit: (words: GoalWords, who: Who) => Promise<void>;
  /** Back to the plan the draft already has, unchanged. */
  onCancel?: () => void;
  /** Shown on the Start page, under its list: a smaller heading, no focus grab, no links out. */
  start?: boolean;
}) {
  const { goal } = words;
  const setGoal = (g: string) => setWords({ ...words, goal: g });
  const box = useRef<HTMLTextAreaElement>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<{ text: string; noProvider: boolean }>();
  useEffect(() => { if (!start) box.current?.focus(); }, [start]);
  // GitHub connected: open with one goal grounded in their repositories (reacting is easier than
  // writing). Quiet when there is none: the examples below still stand.
  const [suggestion, setSuggestion] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    api.setupGithubSuggestion().then((r) => { if (live) setSuggestion(r.suggestion); }).catch(() => {});
    return () => { live = false; };
  }, []);

  const plan = async () => {
    const g = goal.replace(/\s+/g, " ").trim();
    if (!g || busy) return;
    setBusy(true); setErr(undefined);
    try {
      await onSubmit({ goal: g, name: words.name.replace(/\s+/g, " ").trim(), about: words.about.trim() }, who);
    } catch (e) {
      setErr({ text: errText(e), noProvider: errStatus(e) === 409 });
      setBusy(false);
    }
  };

  return (
    <section className="su-section su-narrow" aria-labelledby="su-goal-h">
      <div className="su-head">
        {start
          ? <h2 id="su-goal-h" className="su-title su-title-sm">Your first project</h2>
          : <h1 id="su-goal-h" className="su-title">What should this project achieve?</h1>}
        <p className="su-sub">Say it the way you would to a colleague. Tares plans the rest and shows you the plan before anything runs.</p>
      </div>

      <form className="su-section" onSubmit={(e) => { e.preventDefault(); plan(); }}>
        <div className="su-field">
          <label htmlFor="su-name">Name <span className="help">(optional, Tares suggests one)</span></label>
          <input id="su-name" value={words.name} disabled={busy} maxLength={80}
                 placeholder="New signups"
                 onChange={(e) => setWords({ ...words, name: e.target.value })} />
        </div>
        <label htmlFor="su-goal" className="su-label">Goal</label>
        <textarea id="su-goal" ref={box} rows={2} className="su-goal-box" value={goal} disabled={busy}
                  placeholder="Catch checkout outages early and find the root cause"
                  onChange={(e) => setGoal(e.target.value)}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); plan(); } }} />

        {suggestion && (
          <div className="su-examples">
            <span className="help">From your GitHub repositories</span>
            <div className="su-chips">
              <button type="button" className={`su-chip${goal.trim() === suggestion ? " on" : ""}`}
                      aria-pressed={goal.trim() === suggestion} disabled={busy}
                      onClick={() => { setGoal(suggestion); box.current?.focus(); }}>{suggestion}</button>
            </div>
          </div>
        )}

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

        <div className="su-field">
          <label htmlFor="su-about">Describe it <span className="help">(optional)</span></label>
          <textarea id="su-about" rows={3} value={words.about} disabled={busy} maxLength={4000}
                    placeholder="Where the data comes from, what a good result looks like, who should hear about it. You can paste an alert or an incident thread here."
                    onChange={(e) => setWords({ ...words, about: e.target.value })} />
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

      {!start && (
        <p className="help su-alt">
          Prefer to pick the parts yourself? <Link to="/projects/new/templates">Start from a template</Link>, or{" "}
          <Link to="/projects/new/custom">build it by hand from existing objects</Link>.
        </p>
      )}
    </section>
  );
}
