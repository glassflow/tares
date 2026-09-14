import { useEffect, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";

import { api } from "../api";
import { Picker } from "../components/bits";
import type { Project, Template, RecipeParam } from "../types";

// The fallback wizard: a form rendered straight from a template's PARAMS, for templates without a
// dedicated flow. Strings, numbers, booleans and choices get inputs; lists and objects are JSON.
// With ?edit=<project id> it is the same form over an existing project: prefilled from its
// params, Save updates it in place. Secret params are never shown back; blank means keep.

function initial(p: RecipeParam): string {
  if (p.default == null) return p.type === "bool" ? "false" : "";
  return typeof p.default === "string" ? p.default : JSON.stringify(p.default);
}

/** A stored param value as the form shows it. */
function fromStored(p: RecipeParam, v: unknown): string {
  if (p.secret || v === undefined || v === null) return p.secret ? "" : initial(p);
  if (p.type === "bool") return v ? "true" : "false";
  if (p.type === "list" || p.type === "json") return JSON.stringify(v, null, 2);
  return String(v);
}

function CopyBtn({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button type="button" onClick={() => { navigator.clipboard?.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 1500); }}>
      {copied ? "copied" : "copy"}
    </button>
  );
}

export default function ProjectNewGeneric() {
  const { template: key = "" } = useParams();
  const navigate = useNavigate();
  const [search] = useSearchParams();
  const editId = search.get("edit");
  const [existing, setExisting] = useState<Project>();
  const [template, setRecipe] = useState<Template>();
  const [err, setErr] = useState<string>();
  const [name, setName] = useState("");
  const [vals, setVals] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [detected, setDetected] = useState<{ params: Record<string, unknown>; found: Record<string, string>; missing: Record<string, string>; notes: string[] }>();
  const [detectBusy, setDetectBusy] = useState(false);
  const detect = async (rk: string, current: Record<string, string>) => {
    setDetectBusy(true);
    try {
      const d = await api.detectRecipe(rk);
      setDetected(d);
      setVals((v) => {
        const next = { ...current, ...v };
        for (const [k, val] of Object.entries(d.params)) next[k] = typeof val === "string" ? val : JSON.stringify(val);
        return next;
      });
    } catch { setDetected(undefined); }
    setDetectBusy(false);
  };
  const [models, setModels] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [keyStatus, setKeyStatus] = useState<{ configured: boolean; source: string }>();
  const [keyInput, setKeyInput] = useState("");
  const [keyBusy, setKeyBusy] = useState(false);
  const [keyErr, setKeyErr] = useState<string>();
  const loadKey = () => api.anthropicKeyStatus().then((k) => setKeyStatus(k)).catch(() => setKeyStatus(undefined));
  const saveKey = async () => {
    setKeyBusy(true); setKeyErr(undefined);
    try { await api.setAnthropicKey(keyInput.trim()); setKeyInput(""); await loadKey(); }
    catch (e) { setKeyErr(String((e as Error).message ?? e)); }
    setKeyBusy(false);
  };

  useEffect(() => {
    loadKey();
    api.builtinAgents().then((r) => { setModels(r.models); setDefaultModel(r.default_model); }).catch(() => {});
    // by key, so a hidden template (one a control plane creates over the API) still renders
    // its form when a person edits the project built on it
    api.template(key).then(async (found) => {
      setRecipe(found);
      if (editId) {
        const proj = await api.project(editId);
        setExisting(proj);
        setName(proj.name);
        setVals(Object.fromEntries(Object.entries(found.params).map(([k, p]) => [k, fromStored(p, proj.params[k])])));
        return;   // no detect on edit: the stored values are the truth, Rescan is one click away
      }
      setName(found.title);
      const init = Object.fromEntries(Object.entries(found.params).map(([k, p]) => [k, initial(p)]));
      setVals(init);
      detect(found.key, init);
    }).catch((e) => setErr(String((e as Error).message ?? e)));
  }, [key, editId]);

  const submit = async () => {
    if (!template) return;
    setBusy(true); setErr(undefined);
    const params: Record<string, unknown> = {};
    try {
      for (const [k, p] of Object.entries(template.params)) {
        const v = vals[k] ?? "";
        // a secret left blank on edit keeps the stored value (never shown back to the form)
        if (existing && p.secret && v === "" && existing.params[k] !== undefined) { params[k] = existing.params[k]; continue; }
        if (v === "" && !p.required) continue;
        if (p.type === "number") params[k] = Number(v);
        else if (p.type === "bool") params[k] = v === "true";
        else if (p.type === "list" || p.type === "json") params[k] = v ? JSON.parse(v) : undefined;
        else params[k] = v;
      }
      if (existing) {
        await api.updateProject(existing.id, { params, name: name.trim() || undefined });
        navigate(`/projects/${encodeURIComponent(existing.id)}`, { replace: true });
        return;
      }
      const u = await api.createProject({ template: template.key, name: name.trim() || undefined, params });
      navigate(`/projects/${encodeURIComponent(u.id)}`, { replace: true });
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>{existing ? `Edit ${existing.name}` : (template?.title ?? key)}</h1>
          <p className="subtitle">
            {template?.description}
            {template?.guide && <> Follows the <a href={template.guide.url} target="_blank" rel="noreferrer">{template.guide.label}</a>.</>}
          </p>
        </div>
      </div>
      {err && <div className="alert error">{err}</div>}
      {template && !existing && template.setup && template.setup.length > 0 && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Before you start</h2>
          <ol className="uc-setup">
            {template.setup.map((st, i) => {
              const keyStep = st.check === "anthropic_key";
              const detectStep = st.check === "detect";
              const detectDone = !!detected && Object.keys(detected.found).length > 0 && Object.keys(detected.missing).length === 0;
              const done = (keyStep && keyStatus?.configured) || (detectStep && detectDone);
              return (
              <li key={i} className={done ? "uc-setup-done" : undefined}>
                <div className="uc-setup-title">
                  {st.title}
                  {done && <span className="badge ok" style={{ marginLeft: 8 }}>done</span>}
                </div>
                {done ? (
                  <p className="help" style={{ margin: "2px 0 0" }}>
                    {keyStep
                      ? <>a model provider is set{keyStatus?.source ? ` (${keyStatus.source})` : ""}; the agent can run. Change it under Settings, Model providers.</>
                      : <>running: {Array.from(new Set(Object.values(detected?.found ?? {}).map((v) => v.split(" (")[0].split(" publishes")[0]))).join(", ")}; the fields below were filled from it</>}
                  </p>
                ) : (
                  <>
                    {st.text && <p className="help" style={{ margin: "2px 0 6px" }}>{st.text}</p>}
                    {keyStep && (
                      <div className="btnrow" style={{ alignItems: "center", maxWidth: 640 }}>
                        <input type="password" className="mono" style={{ flex: 1 }} autoComplete="new-password" data-1p-ignore data-lpignore="true"
                               placeholder="sk-ant-..." value={keyInput} onChange={(e) => setKeyInput(e.target.value)} />
                        <button className="primary" disabled={keyBusy || !keyInput.trim()} onClick={saveKey}>{keyBusy ? "saving..." : "Save key"}</button>
                        {keyErr && <span className="help" style={{ color: "var(--err)" }}>{keyErr}</span>}
                      </div>
                    )}
                  </>
                )}
                {st.command && (
                  <div className="uc-setup-cmd">
                    <pre className="mono">{st.command}</pre>
                    <CopyBtn text={st.command} />
                  </div>
                )}
              </li>
              );
            })}
          </ol>
        </div>
      )}
      {template && (
        <div className="panel">
          <label className="field">
            <span className="lbl">name</span>
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} />
          </label>
          {Object.entries(template.params).map(([k, p]) => (
            <label className="field" key={k}>
              <span className="lbl">{p.label ?? k}{p.required && <span className="req"> *</span>}</span>
              {p.type === "bool" ? (
                <Picker value={vals[k] ?? "false"} onChange={(v) => setVals({ ...vals, [k]: v })}
                        options={["true", "false"]} ariaLabel={k} />
              ) : k === "model" ? (
                <Picker value={vals[k] ?? ""} onChange={(v) => setVals({ ...vals, [k]: v })}
                        options={["", ...models.filter((m) => m !== defaultModel)]}
                        labels={{ "": defaultModel ? `${defaultModel} · instance default` : "instance default" }}
                        ariaLabel="model" />
              ) : p.choices?.length ? (
                <Picker value={vals[k] ?? ""} onChange={(v) => setVals({ ...vals, [k]: v })}
                        options={p.choices} ariaLabel={k} />
              ) : p.type === "list" || p.type === "json" ? (
                <textarea className="mono" rows={4} value={vals[k] ?? ""}
                          onChange={(e) => setVals({ ...vals, [k]: e.target.value })} placeholder="JSON" />
              ) : (
                <input type={p.secret ? "password" : p.type === "number" ? "number" : "text"} className="mono" value={vals[k] ?? ""}
                       placeholder={p.secret && existing && existing.params[k] !== undefined ? "configured, leave blank to keep" : undefined}
                       autoComplete={p.secret ? "new-password" : undefined}
                       onChange={(e) => setVals({ ...vals, [k]: e.target.value })} />
              )}
              {p.help && <span className="help">{p.help}</span>}
              {detected?.found[k] && <span className="help" style={{ color: "var(--ok)" }}>detected: {detected.found[k]}</span>}
              {detected?.missing[k] && <span className="help" style={{ color: "var(--warn, var(--err))" }}>not detected: {detected.missing[k]}</span>}
            </label>
          ))}
          {detected && (
            <div className="btnrow" style={{ marginBottom: 12 }}>
              <button onClick={() => template && detect(template.key, vals)} disabled={detectBusy}>{detectBusy ? "scanning…" : "Rescan"}</button>
              {detected.notes.map((n) => <span key={n} className="help">{n}</span>)}
            </div>
          )}
          <div className="btnrow">
            <button className="primary" disabled={busy} onClick={submit}>
              {busy ? (existing ? "saving…" : "starting…") : existing ? "Save" : "Start"}
            </button>
            <Link className="btn" to={existing ? `/projects/${encodeURIComponent(existing.id)}` : "/projects"}>Cancel</Link>
          </div>
        </div>
      )}
    </>
  );
}
