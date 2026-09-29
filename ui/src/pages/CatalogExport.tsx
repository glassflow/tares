import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import type { Source } from "../types";

const REDACTED = "••••••••";   // matches connectors.REDACTED_SECRET; a set secret comes back as this
const hasSecret = (s: Source) => Object.values(s.config ?? {}).includes(REDACTED);

// Export the catalog to portable YAML. Defaults mirror the API/agent call (all sources, no secrets);
// this page adds source selection + an opt-in to include secrets.
export default function CatalogExport() {
  const [sources, setSources] = useState<Source[]>();
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [includeSecrets, setIncludeSecrets] = useState(false);
  const [yaml, setYaml] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    api.sources().then((ss) => { setSources(ss); setPicked(new Set(ss.map((s) => s.name))); })
      .catch((e) => setErr(String((e as Error).message ?? e)));
  }, []);

  const anySecret = useMemo(
    () => (sources ?? []).some((s) => picked.has(s.name) && hasSecret(s)), [sources, picked]);
  const allPicked = !!sources && picked.size === sources.length;

  const toggle = (name: string) => setPicked((p) => {
    const n = new Set(p); n.has(name) ? n.delete(name) : n.add(name); return n;
  });

  const run = async () => {
    setBusy(true); setErr(undefined); setYaml(undefined);
    try {
      const all = sources?.length === picked.size;
      setYaml(await api.exportYaml({
        sources: all ? undefined : [...picked],   // omit param when all → matches the default call
        includeSecrets,
      }));
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const copy = () => { if (yaml) { navigator.clipboard.writeText(yaml); setCopied(true); setTimeout(() => setCopied(false), 1500); } };
  const download = () => {
    if (!yaml) return;
    const url = URL.createObjectURL(new Blob([yaml], { type: "application/yaml" }));
    const a = document.createElement("a");
    a.href = url; a.download = "tares-catalog.yaml"; a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <div className="pagehead">
        <div>
          <p className="subtitle" style={{ marginBottom: 4 }}><Link to="/sources">Sources</Link> ›</p>
          <h1>Export catalog</h1>
          <p className="subtitle">the portable form of your catalog; sources, triggers, agents and projects as YAML</p>
        </div>
      </div>

      {err && <div className="alert error">{err}</div>}
      {!sources ? <div className="dim">loading…</div> : (
        <>
          <div className="panel">
            <div className="pagehead" style={{ marginBottom: 8 }}>
              <h2 style={{ margin: 0 }}>Sources</h2>
              <button onClick={() => setPicked(allPicked ? new Set() : new Set(sources.map((s) => s.name)))}>
                {allPicked ? "Deselect all" : "Select all"}
              </button>
            </div>
            <p className="help" style={{ marginTop: 0 }}>
              Triggers that read only the selected sources are included automatically.
            </p>
            {sources.length === 0 && <div className="empty">no sources to export</div>}
            {sources.map((s) => (
              <label key={s.name} className="explore-item" style={{ cursor: "pointer" }}>
                <input type="checkbox" checked={picked.has(s.name)} onChange={() => toggle(s.name)}
                       style={{ marginRight: 8 }} />
                <span className="mono">{s.name}</span>
                <span className="chip mono" style={{ marginLeft: 8 }}>{s.connector}</span>
                {hasSecret(s) && <span className="badge push" style={{ marginLeft: 8 }} title="carries a connector secret">secret</span>}
              </label>
            ))}
          </div>

          <div className="panel">
            <label style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
              <input type="checkbox" checked={includeSecrets} onChange={(e) => setIncludeSecrets(e.target.checked)} />
              <span>Include connector secrets (tokens, DSNs)</span>
            </label>
            <p className="help" style={{ margin: "6px 0 0" }}>
              {includeSecrets
                ? <span style={{ color: "var(--err)" }}>⚠️ The YAML will contain credentials in plaintext; don't commit or share it.</span>
                : <>Secrets are left out (empty); re-enter them on the target when importing. {anySecret && "Some selected sources carry a secret."}</>}
            </p>
          </div>

          <div className="btnrow">
            <button className="primary" onClick={run} disabled={busy || picked.size === 0}>
              {busy ? "Exporting…" : `Export ${picked.size} source${picked.size === 1 ? "" : "s"}`}
            </button>
            <Link className="btn" to="/sources/import">Import instead →</Link>
          </div>

          {yaml && (
            <div className="panel" style={{ marginTop: 14 }}>
              <div className="pagehead" style={{ marginBottom: 8 }}>
                <h2 style={{ margin: 0 }}>Result</h2>
                <span className="btnrow">
                  <button onClick={copy}>{copied ? "Copied ✓" : "Copy"}</button>
                  <button onClick={download}>Download .yaml</button>
                </span>
              </div>
              <pre className="payload">{yaml}</pre>
            </div>
          )}
        </>
      )}
    </>
  );
}
