import { Link, useNavigate } from "react-router-dom";

import { api } from "../api";
import { ErrorState, usePolling } from "../components/bits";
import type { Template } from "../types";

// The template gallery, at /projects/new/templates: one card per template, the deterministic
// path. Create new itself (/projects/new) is the landing screen (Landing.tsx), which links here
// and, per template sentence, straight to the wizard. Templates the console sets up with a
// dedicated wizard; anything else the API lists still gets a card, routed to the generic form.
export const WIZARDS: Record<string, string> = {
  shared_code_context: "/projects/new/shared_code_context",
};

/** Where a template's step-by-step setup lives. */
export const wizardPath = (key: string) => WIZARDS[key] ?? `/projects/new/${encodeURIComponent(key)}`;

// What each template wires up, in the user's words. The daemon's describe() may carry mode-aware
// facts (ai_sre_demo says different things against a hosted stack than against local docker);
// this map is the fallback for templates that don't, and for older daemons.
const TEMPLATE_FACTS: Record<string, { you: string[]; tares: string[] }> = {
  ai_sre_demo: {
    you: ["start the demo stack with docker compose", "give the agent an Anthropic key", "cause an incident from the project page"],
    tares: ["three sources keyed by service: Prometheus metrics, the api-server's logs, the alerts Prometheus fires", "one timeline per service to explore", "a trigger that wakes the agent when an alert fires", "an agent that writes the first incident note back onto the timeline"],
  },
  shared_code_context: {
    you: ["pick the code repos to watch", "pick the repo that holds the shared context", "choose when it runs and how it writes"],
    tares: ["a source for each repo, so its commits flow into Tares", "a timeline per repo you can explore and query", "a trigger that wakes the agent when new commits land", "an agent that reads each change and updates the context repo, opening a pull request"],
  },
};

function TemplateCard({ r }: { r: Template }) {
  const navigate = useNavigate();
  const to = wizardPath(r.key);
  const facts = r.facts ?? TEMPLATE_FACTS[r.key];
  return (
    <div className="panel uc-card">
      <div className="uc-card-main">
        <div className="uc-card-title">
          {r.title}
          {(r.tags ?? []).map((t) => <span key={t} className="badge" style={{ marginLeft: 8, verticalAlign: "middle" }}>{t}</span>)}
        </div>
        <div className="help" style={{ marginTop: 2 }}>Tares template</div>
        <p className="help uc-card-desc">
          {r.description}
          {r.guide && <> Follows the <a href={r.guide.url} target="_blank" rel="noreferrer">{r.guide.label}</a>.</>}
        </p>
        {facts && (
          <div className="uc-card-facts">
            <div>
              <div className="lbl">you</div>
              <ul>{facts.you.map((t) => <li key={t}>{t}</li>)}</ul>
            </div>
            <div>
              <div className="lbl">Tares sets up</div>
              <ul>{facts.tares.map((t) => <li key={t}>{t}</li>)}</ul>
            </div>
          </div>
        )}
      </div>
      <div className="uc-card-side">
        <button className="primary" onClick={() => navigate(to)}>Set up</button>
      </div>
    </div>
  );
}

export default function ProjectTemplates() {
  const navigate = useNavigate();
  const { data: rec, error: recError, reload: reloadRec } = usePolling(() => api.templates(), 60000);
  const templates = rec?.templates ?? [];

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>Templates</h1>
          <p className="subtitle">
            the step-by-step path: answer a few questions and Tares creates the objects behind it,
            each visible and editable on its own page. Or assemble one from objects you already
            have. To describe what you need in your own words instead, <Link to="/projects/new">create a project</Link>.
          </p>
        </div>
      </div>

      {recError && <ErrorState error={recError} what="the project catalog" onRetry={reloadRec} />}
      {!rec && !recError && <div className="dim">loading…</div>}
      {rec && templates.length === 0 && (
        <div className="empty">No templates are registered on this instance yet.</div>
      )}
      <div className="uc-cards">
        {templates.map((r) => <TemplateCard key={r.key} r={r} />)}
        {rec && (
          <div className="panel uc-card">
            <div className="uc-card-main">
              <div className="uc-card-title">From existing objects</div>
              <p className="help uc-card-desc">
                Assemble a project from sources, views, triggers, agents and MCP servers you already have.
                Nothing is created; the project page shows their runs and firings.
              </p>
            </div>
            <div className="uc-card-side">
              <button className="primary" onClick={() => navigate("/projects/new/custom")}>Assemble</button>
            </div>
          </div>
        )}
      </div>
    </>
  );
}
