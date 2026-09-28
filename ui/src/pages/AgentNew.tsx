import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { api } from "../api";
import AgentForm from "../components/AgentForm";
import type { ModelProvider, AgentPreset } from "../types";

// Create a Tares agent. Reachable from the Agents section (trigger via dropdown) or from a
// trigger's page ("Add a Tares agent" → ?trigger=<name>, preselected).
export default function AgentNew() {
  const nav = useNavigate();
  const [params] = useSearchParams();
  const presetTrigger = params.get("trigger") ?? undefined;

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
    api.triggers().then((ts) => setTriggers(ts.map((t) => t.name)))
      .catch((e) => setErr(String((e as Error).message ?? e)));
    api.builtinAgents().then((d) => {
      setPresets(d.presets); setKeyOk(d.key_configured);
      setModels(d.models); setDefaultModel(d.default_model);
      setProviders(d.providers ?? []); setDefaultProvider(d.default_provider ?? null);
      setDefaultModels(d.default_models ?? {});
      setSlackWorkspace(d.slack_workspace);
      setRounds({ d: d.default_max_rounds, m: d.default_max_rounds_with_mcp, l: d.max_rounds_limit });
    }).catch(() => {});
  }, []);

  return (
    <>
      <h1>Create Tares agent</h1>
      <p className="subtitle">
        a prompt on a trigger; it reads the correlated timeline when the trigger fires and writes a
        finding back onto the entity's timeline
      </p>

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
            No triggers yet; an agent runs on a trigger. Create one under{" "}
            <Link to="/triggers">Triggers</Link> first.
          </div>
        ) : (
          <AgentForm
            presetTrigger={presetTrigger}
            triggers={triggers}
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
            onCancel={() => nav(presetTrigger ? `/triggers/${encodeURIComponent(presetTrigger)}` : "/agents")}
          />
        )}
    </>
  );
}
