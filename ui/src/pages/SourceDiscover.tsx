import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api";
import type { EnvScan, ProposedSource } from "../types";

/** Auto-discover: scan the local Docker environment and propose a catalog of sources across
 *  connector types (container logs + a detected Prometheus). The user confirms a checklist. */
export default function SourceDiscover() {
  const nav = useNavigate();
  const [scan, setScan] = useState<EnvScan>();
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [results, setResults] = useState<Record<string, string>>();
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setLoading(true); setError(undefined); setResults(undefined);
    try {
      const s = await api.discoverEnvironment("docker");
      setScan(s);
      setPicked(Object.fromEntries(s.proposed_sources.map((p) => [p.name, p.preselect])));
    } catch (e) { setError(String((e as Error).message ?? e)); }
    setLoading(false);
  };
  useEffect(() => { run(); }, []);

  const create = async () => {
    if (!scan) return;
    setBusy(true);
    const out: Record<string, string> = {};
    for (const p of scan.proposed_sources) {
      if (!picked[p.name]) continue;
      try {
        await api.createSource({ name: p.name, connector: p.connector, config: p.config });
        out[p.name] = "ok";
      } catch (e) { out[p.name] = String((e as Error).message ?? e); }
    }
    setResults(out);
    setBusy(false);
  };

  const chosen = scan?.proposed_sources.filter((p) => picked[p.name]).length ?? 0;
  const created = results ? Object.values(results).filter((v) => v === "ok").length : 0;
  const failed = results ? Object.values(results).filter((v) => v !== "ok").length : 0;
  const allOk = !!results && failed === 0;

  // On a fully successful create, confirm briefly then hand off to Sources (where the new sources
  // are now listed). If any failed, stay put so the user can fix and retry.
  useEffect(() => {
    if (!allOk) return;
    const t = setTimeout(() => nav("/sources"), 1400);
    return () => clearTimeout(t);
  }, [allOk, nav]);

  return (
    <>
      <div className="pagehead">
        <h1 style={{ margin: 0 }}>Auto-discover</h1>
        <button onClick={run} disabled={loading || busy}>rescan</button>
      </div>
      <p className="subtitle">
        Scan the local Docker environment and set up everything Tares can take in. You confirm
        before anything is made.
      </p>

      {loading && <div className="empty">scanning Docker…</div>}
      {error && (
        <div className="alert error">
          {error}
          <div className="help" style={{ marginTop: 6 }}>
            Auto-discover needs the Docker daemon reachable from taresd's host. You can still{" "}
            <a href="#" onClick={(e) => { e.preventDefault(); nav("/sources/new"); }}>add a source by hand</a>.
          </div>
        </div>
      )}

      {scan && (
        <>
          <p className="subtitle">
            {scan.summary.containers} containers · {scan.summary.proposed} ingestable sources proposed
          </p>

          <div className="panel">
            <table>
              <thead><tr><th></th><th>source</th><th>connector</th><th>what Tares saw</th></tr></thead>
              <tbody>
                {scan.proposed_sources.map((p: ProposedSource) => (
                  <tr key={p.name}>
                    <td>
                      <input type="checkbox" checked={!!picked[p.name]}
                             onChange={(e) => setPicked({ ...picked, [p.name]: e.target.checked })} />
                    </td>
                    <td className="mono">{p.name}</td>
                    <td><span className="chip">{p.connector}</span></td>
                    <td>{p.summary} <span className="help">· {p.from}</span>
                      {results?.[p.name] && (
                        <span className={`badge ${results[p.name] === "ok" ? "ok" : "error"}`}
                              style={{ marginLeft: 8 }}>
                          {results[p.name] === "ok" ? "created" : results[p.name]}
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <div className="btnrow" style={{ marginTop: 12, alignItems: "center" }}>
              {allOk ? (
                <>
                  <span className="badge ok">created</span>
                  <span className="help">
                    added {created} source{created === 1 ? "" : "s"}; taking you to Sources…
                  </span>
                </>
              ) : (
                <>
                  <button className="primary" onClick={create} disabled={busy || chosen === 0}>
                    {busy ? "creating…" : `Create ${chosen} selected source${chosen === 1 ? "" : "s"}`}
                  </button>
                  {results && (
                    <span className="help">
                      created {created} of {Object.keys(results).length}
                      {failed ? `, ${failed} failed; fix and retry` : ""} ·{" "}
                      <a href="#" onClick={(e) => { e.preventDefault(); nav("/sources"); }}>see sources</a>
                    </span>
                  )}
                </>
              )}
            </div>
          </div>

          {scan.skipped.length > 0 && (
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>Detected, not ingestable</h2>
              <table>
                <tbody>
                  {scan.skipped.map((s) => (
                    <tr key={s.service}>
                      <td className="mono">{s.service}</td>
                      <td className="help">{s.image}</td>
                      <td className="help">{s.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </>
  );
}
