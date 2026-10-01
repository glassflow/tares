import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import type { SetupPlanning } from "../../types";

// A plan being written, shown as it happens: what the planner read, then writing the plan (the
// long part, with the seconds it has taken), checking it, fixing what the check found. The
// steps come from the planner itself (GET /api/projects/{id}/setup, `planning`), so what the
// page says is what is going on. The draft is already a project: the person can leave and
// come back from the Projects list.

export function useSeconds(since: string | undefined): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!since) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [since]);
  return since ? Math.max(0, Math.round((now - new Date(since).getTime()) / 1000)) : 0;
}

export function PlanningView({ goal, planning, changing, onRetry, onEditGoal, onKeepPlan }: {
  goal: string;
  planning: SetupPlanning | null | undefined;
  /** A plan exists and is being changed (as opposed to written from the goal). */
  changing?: boolean;
  onRetry: () => void;
  onEditGoal: () => void;
  /** After a failed change: back to the plan as it was. */
  onKeepPlan?: () => void;
}) {
  const steps = planning?.steps ?? [];
  const running = steps.find((s) => s.state === "running");
  const secs = useSeconds(running?.at);
  const failed = planning?.state === "failed";

  return (
    <section className="su-section su-narrow" aria-labelledby="su-planning-h">
      <div className="su-head">
        <h1 id="su-planning-h" className="su-title">
          {failed ? "The plan did not come out" : changing ? "Changing the plan" : "Planning your project"}
        </h1>
        <p className="su-goal-line">{goal}</p>
      </div>

      <ol className="su-progress" aria-live="polite">
        {steps.map((s, i) => (
          <li key={i} className={s.state}>
            <span className="su-progress-mark" aria-hidden="true" />
            <span>
              {s.text}
              {s.state === "running" && !failed && <span className="help"> · {secs} s</span>}
            </span>
          </li>
        ))}
        {!steps.length && !failed && (
          <li className="running"><span className="su-progress-mark" aria-hidden="true" /><span>Starting</span></li>
        )}
      </ol>

      {!failed && (
        <p className="help">
          Writing a plan usually takes under a minute. It is saved as a draft: you can leave this
          page and pick it up again from <Link to="/projects">Projects</Link>.
        </p>
      )}

      {failed && (
        <div className="alert error" role="alert">
          {planning?.error || "Planning stopped."}
          {planning?.no_provider && (
            <div className="btnrow" style={{ marginTop: 8 }}>
              <Link className="btn" to="/settings">Add a model provider</Link>
            </div>
          )}
        </div>
      )}
      {failed && (
        <div className="btnrow">
          <button type="button" className="primary su-cta" onClick={onRetry}>Try again</button>
          <button type="button" className="su-cta" onClick={onEditGoal}>Change the goal</button>
          {onKeepPlan && <button type="button" className="su-cta" onClick={onKeepPlan}>Keep the plan as it was</button>}
        </div>
      )}
    </section>
  );
}
