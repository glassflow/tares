import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { api } from "../api";
import { usePolling } from "../components/bits";
import type { ConnectorSpec, Project, Template } from "../types";

// The screen a new cell lands on (TR-257): one question, answered by typing. Shown by Home while
// the cell has no project of the user's own; the seeded demo does not count.
//
// It is a conversation that has not started yet, not a form. As the user types, the screen
// answers: the connectors they name light up under the box. Nothing here asks the user to know
// a source from a view. Three doors, one builder: describe it, paste your last incident, or
// start the demo. Templates live behind the sentences; the gallery stays one click away under
// Projects for people who already know what they want.

// Words a person types, mapped to the connector that carries that signal. Only connectors
// installed on this cell light up; the map is the vocabulary, the cell decides what exists.
const WORDS: [RegExp, string][] = [
  [/\b(docker|container|compose)\b/i, "docker_logs"],
  [/\b(prometheus|metrics?|latency|p95|p99|cpu|memory)\b/i, "prometheus"],
  // "alert me" is what the user wants done, not a source; only the product names count here
  [/\b(alertmanager|prometheus alerts?|alerting rules?)\b/i, "prometheus_alerts"],
  [/\b(loki|grafana)\b/i, "loki"],
  [/\b(postgres|postgresql|database|table|sql|rows?)\b/i, "postgres"],
  [/\b(github|repos?|repositor(y|ies)|commits?|pull requests?|prs?)\b/i, "github"],
  [/\b(vercel|next\.?js|deploys?|deployments?)\b/i, "vercel"],
  [/\b(otlp|opentelemetry|otel|traces?|spans?)\b/i, "otlp"],
  [/\b(webhook|payload|http post)\b/i, "webhook"],
  // an API the user reads, not one that calls them: polled, no script on their side
  [/\b(api|apis|endpoint|json|weather|status page|rest)\b/i, "http_poll"],
  [/\b(claude code|claude)\b/i, "claude_code"],
];
// Delivery words: not connectors, but the screen should still show it heard them.
const DELIVERY: [RegExp, string][] = [
  [/\bslack\b/i, "Slack"],
  [/\b(email|e-mail|mail)\b/i, "email"],
  [/\b(webhook|post (it|the finding) to|callback)\b/i, "webhook"],
];

// Goals people arrive with that no template covers; the builder takes them from scratch.
const COMMON: string[] = [
  "watch my payment service logs and wake an agent in Slack when checkouts fail",
  "when a deploy makes my API slower, wake an agent that finds the commit",
  "tell me when a city in Germany gets storm winds, from a public weather API",
];



/** Create new (/projects/new): the landing screen loading its own data. */
export function ProjectNewPage() {
  const { data: rec } = usePolling(() => api.templates(), 60000);
  const { data: uc } = usePolling(() => api.projects(), 30000);
  if (!rec || !uc) return <div className="dim">loading…</div>;
  return <Landing templates={rec.templates} projects={uc.projects} />;
}

export default function Landing({ templates, projects }: { templates: Template[]; projects: Project[] }) {
  const navigate = useNavigate();
  const [goal, setGoal] = useState("");
  const [picked, setPicked] = useState("");   // the template behind the sentence in the box, if any
  const [paste, setPaste] = useState("");
  const [pasting, setPasting] = useState(false);
  const [specs, setSpecs] = useState<Record<string, ConnectorSpec>>({});
  const box = useRef<HTMLTextAreaElement>(null);
  useEffect(() => { api.connectors().then(setSpecs).catch(() => {}); }, []);
  useEffect(() => { box.current?.focus(); }, []);

  // what the screen heard: installed connectors the words point at, and where the finding goes
  const heard = useMemo(() => {
    const out: string[] = [];
    for (const [re, key] of WORDS) if (re.test(goal) && specs[key] && !specs[key].internal && !out.includes(key)) out.push(key);
    return out;
  }, [goal, specs]);
  const delivery = useMemo(() => DELIVERY.filter(([re]) => re.test(goal)).map(([, d]) => d), [goal]);

  // A fixed list, in a fixed order. It used to reorder by overlap with the typed words, which
  // moved the row the user had just clicked; the sentence is already in the box, that is enough.
  const sentences = useMemo(() => {
    const fromTemplates = templates.filter((t) => t.sentence).map((t) => ({ text: t.sentence!, template: t.key }));
    return [...fromTemplates, ...COMMON.map((text) => ({ text, template: "" }))];
  }, [templates]);
  // the starter whose sentence is in the box, unedited: shown as the one picked
  const selected = sentences.find((x) => x.text === goal.trim())?.text ?? "";

  const demo = projects.find((p) => p.template === "ai_sre_demo");
  const demoTemplate = templates.find((t) => t.key === "ai_sre_demo");
  // The demo is an offer, never seeded: one click creates it here, in the background, from
  // what detection finds, and lands on its page. When the demo stack is not reachable the
  // wizard takes over with the steps to start it.
  const [demoBusy, setDemoBusy] = useState(false);
  const [demoErr, setDemoErr] = useState<string>();
  const startDemo = async () => {
    if (!demoTemplate) return;
    setDemoBusy(true); setDemoErr(undefined);
    try {
      const d = await api.detectRecipe(demoTemplate.key);
      const required = Object.entries(demoTemplate.params).filter(([, p]) => p.required).map(([k]) => k);
      const missing = required.filter((k) => d.params[k] === undefined || d.params[k] === "");
      if (missing.length) { navigate(`/projects/new/${demoTemplate.key}`); return; }
      const made = await api.createProject({ template: demoTemplate.key, params: d.params });
      navigate(`/projects/${encodeURIComponent(made.id)}`);
    } catch (e) {
      setDemoErr(String((e as Error).message ?? e));
      setDemoBusy(false);
    }
  };

  const go = () => {
    if (!goal.trim()) return;
    // a sentence picked and left as it was names its template; an edited one is free text
    const tpl = sentences.find((x) => x.template && x.text === goal.trim())?.template ?? picked;
    navigate("/projects/new/assist", { state: { goal: goal.trim(), template: tpl || undefined } });
  };
  const goIncident = () => {
    if (!paste.trim()) return;
    navigate("/projects/new/assist", { state: { goal: paste.trim(), incident: true } });
  };

  return (
    <div className="landing">
      <div className="landing-head">
        <div>
          <h1 className="landing-title">Always-on agents for your systems.</h1>
          <p className="landing-sub">Give it your data, tell it what to watch for, see it act.</p>
        </div>
        {/* the deterministic path: the template gallery, one button, top right */}
        <Link className="btn" to="/projects/new/templates">Use a guided template</Link>
      </div>

      <form className="landing-ask" onSubmit={(e) => { e.preventDefault(); go(); }}>
        <label className="landing-q" htmlFor="landing-goal">What do you want to build?</label>
        <textarea id="landing-goal" ref={box} rows={3} value={goal}
                  placeholder="watch my checkout service logs and wake an agent in Slack when payments fail"
                  onChange={(e) => { setGoal(e.target.value); setPicked(""); }}
                  onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); go(); } }} />
        {/* what the screen heard, before anything is pressed */}
        <div className="landing-heard">
          {heard.map((k) => <span key={k} className="chip">{specs[k]?.label ?? k}</span>)}
          {delivery.map((d) => <span key={d} className="chip lbl">finding to {d}</span>)}
          {/* the nudge is for typed text; a picked starter is our sentence, and the row must
              not grow a second line under it and shift the list the user is clicking in */}
          {goal.trim() && heard.length === 0 && !selected && (
            <span className="help">say where it runs: a container, a Prometheus URL, a repo, a table, or an API you can post from</span>
          )}
        </div>
        <div className="btnrow">
          <button className="primary cta" disabled={!goal.trim()}>Build it</button>
          {!pasting && (
            <button type="button" className="dim" onClick={() => setPasting(true)}>or paste your last alert or incident thread</button>
          )}
        </div>
      </form>

      {pasting && (
        <form className="landing-ask" onSubmit={(e) => { e.preventDefault(); goIncident(); }}>
          <label className="landing-q" htmlFor="landing-paste">Paste the alert, the Slack thread, or the postmortem.</label>
          <textarea id="landing-paste" rows={6} value={paste} autoFocus className="mono"
                    placeholder={"[FIRING] HighErrorRate on checkout-api\n502s at 14:02, found by a customer at 15:30 ..."}
                    onChange={(e) => setPaste(e.target.value)} />
          <span className="help">It goes to the assistant like any message and is not stored beyond the project's goal.</span>
          <div className="btnrow">
            <button className="primary cta" disabled={!paste.trim()}>Build what would have caught it</button>
            <button type="button" onClick={() => setPasting(false)}>Cancel</button>
          </div>
        </form>
      )}

      <div className="landing-starters">
        <div className="help">Or start from one of these</div>
        {/* A click picks: the sentence lands in the box above, editable, and the row shows as
            the one picked. Show me (or Enter in the box) is the one way to start; nothing starts
            on the click alone. */}
        {sentences.map((s) => (
          <button key={s.text} type="button"
                  className={"starter" + (selected === s.text ? " on" : selected ? " off" : "")}
                  onClick={() => { setGoal(s.text); setPicked(s.template); box.current?.focus(); }}>
            {s.text}
          </button>
        ))}
      </div>

      {demoTemplate && (
        <div className="panel landing-demo">
          <div>
            <strong>Want to see one run before you bring your data?</strong>
            <p className="help" style={{ margin: "4px 0 0", whiteSpace: "normal" }}>
              The demo is a small service, an agent watching it, and a button to break it. Remove it any time.
            </p>
          </div>
          {demo
            ? <Link className="btn" to={`/projects/${encodeURIComponent(demo.id)}`}>Open the demo</Link>
            : <button onClick={startDemo} disabled={demoBusy}>{demoBusy ? "starting…" : "Start the demo"}</button>}
        </div>
      )}
      {demoErr && (
        <div className="alert error">
          could not start the demo: {demoErr}. <Link to="/projects/new/ai_sre_demo">Set it up step by step</Link> instead.
        </div>
      )}

      <p className="help landing-foot">
        Or <Link to="/projects/new/custom">assemble a project from existing objects</Link>, or{" "}
        <Link to="/sources/new">add a source</Link> on its own.
      </p>
    </div>
  );
}
