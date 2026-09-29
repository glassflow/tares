import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api";
import AgentForm from "../components/AgentForm";
import ProjectBadge from "../components/ProjectBadge";
import type { ModelProvider, AgentPreset, Trigger } from "../types";

// Create a Tares agent inside a project (?project=<id>). An agent wakes on a trigger of its own
// project, so the trigger picker offers only those. Reachable from the Agents list (which asks for
// the project first), a project's page, or a trigger's page (?trigger=<name>, preselected; the
// project is then the trigger's). With neither, the Default project is used.
export default function AgentNew() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const presetTrigger = params.get("trigger") ?? undefined;
  const askedProject = params.get("project") ?? undefined;

  const [project, setProject] = useState<string>();
  const [triggers, setTriggers] = useState<string[]>();
  const [presets, setPresets] = useState<AgentPreset[]>([]);
  const [models, setModels] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [defaultProvider, setDefaultProvider] = useState<string | null>(null);
  const [defaultModels, setDefaultModels] = useState<Record<string, string>>({});
  const [slackWorkspace, setSlackWorkspace] = useState(false);
  const [rounds, setRounds] = useState<{ d: number; m: number; l: number }>();
  const [keyOk, setKeyOk] = useState(true);
  const [err, setErr] = useState<string>();

  useEffect(() => {
    let live = true;
    Promise.all([api.triggers(), askedProject ? Promise.resolve(null) : api.projects()])
      .then(([ts, ps]: [Trigger[], { projects: { id: string; default?: boolean }[] } | null]) => {
        if (!live) return;
        const fromTrigger = presetTrigger ? ts.find((t) => t.name === presetTrigger)?.project : undefined;
        const p = askedProject ?? fromTrigger ?? ps?.projects.find((x) => x.default)?.id ?? "";
        setProject(p);
        setTriggers(ts.filter((t) => !p || !t.project || t.project === p).map((t) => t.name));
      })
      .catch((e) => { if (live) setErr(String((e as Error).message ?? e)); });
    api.builtinAgents().then((d) => {
      if (!live) return;
      setPresets(d.presets); setKeyOk(d.key_configured);
      setModels(d.models); setDefaultModel(d.default_model);
      setProviders(d.providers ?? []); setDefaultProvider(d.default_provider ?? null);
      setDefaultModels(d.default_models ?? {});
      setSlackWorkspace(d.slack_workspace);
      setRounds({ d: d.default_max_rounds, m: d.default_max_rounds_with_mcp, l: d.max_rounds_limit });
    }).catch(() => {});
    return () => { live = false; };
  }, [askedProject, presetTrigger]);

  const back = presetTrigger ? `/triggers/${encodeURIComponent(presetTrigger)}`
    : askedProject ? `/projects/${encodeURIComponent(askedProject)}?view=agents` : "/agents";

  return (
    <>
      <h1>Create Tares agent</h1>
      <p className="subtitle">
        a prompt on a trigger; it reads the correlated timeline when the trigger fires and writes a
        finding back onto the entity's timeline
      </p>
      {project && <p className="help">in <ProjectBadge ownedBy={project} compact /></p>}

      {err && <div className="alert error">{err}</div>}
      {!keyOk && (
        <div className="alert">
          Model access is not configured; you can create the agent now, but it won't run until a key
          is set under <Link to="/settings">Settings</Link>. It also starts disabled.
        </div>
      )}

      {!triggers ? <div className="dim">loading…</div>
        : triggers.length === 0 ? (
          <div className="alert">
            This project has no triggers yet; an agent runs on a trigger.{" "}
            {project
              ? <Link to={`/triggers/new?project=${encodeURIComponent(project)}`}>Add a trigger</Link>
              : <Link to="/triggers">Add a trigger</Link>} first.
          </div>
        ) : (
          <AgentForm
            presetTrigger={presetTrigger}
            triggers={triggers}
            project={project || undefined}
            presets={presets}
            models={models}
            defaultModel={defaultModel}
            providers={providers}
            defaultProvider={defaultProvider}
            defaultModels={defaultModels}
            slackWorkspace={slackWorkspace}
            defaultMaxRounds={rounds?.d} defaultMaxRoundsWithMcp={rounds?.m}
            maxRoundsLimit={rounds?.l}
            onSaved={(name) => nav(`/agents/${encodeURIComponent(name)}`)}
            onCancel={() => nav(back)}
          />
        )}
    </>
  );
}
