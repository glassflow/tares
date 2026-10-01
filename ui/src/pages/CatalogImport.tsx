import { useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";

// Import a catalog YAML (the portable form). Validated as a whole before anything is written.
export default function CatalogImport() {
  const [text, setText] = useState("");
  const [mode, setMode] = useState<"merge" | "replace">("merge");
  const [msg, setMsg] = useState<{ ok: boolean; text: string }>();
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setBusy(true); setMsg(undefined);
    try {
      const r = await api.importYaml(text, mode);
      setMsg({ ok: true, text: `imported ${r.sources} sources, ${r.triggers} triggers, `
        + `${r.agents} agents, ${r.mcp_servers} mcp servers (${mode})` });
      setText("");
    } catch (e) {
      setMsg({ ok: false, text: String((e as Error).message ?? e) });
    }
    setBusy(false);
  };

  return (
    <>
      <div className="pagehead">
        <div>
          <p className="subtitle" style={{ marginBottom: 4 }}><Link to="/sources">Sources</Link> ›</p>
          <h1>Import catalog</h1>
          <p className="subtitle">Paste a catalog YAML with sources, triggers, agents and MCP servers.</p>
        </div>
      </div>

      {msg && <div className={`alert ${msg.ok ? "ok" : "error"}`}>{msg.text}</div>}

      <div className="panel">
        <label className="field">
          <span className="lbl">catalog YAML</span>
          <textarea className="code" style={{ minHeight: 260 }} value={text}
                    placeholder={"sources:\n  - name: ...\ntriggers:\n  - name: ...\n    project: ...\n    sources: [...]"}
                    onChange={(e) => setText(e.target.value)} />
          <span className="help">secrets left empty in an export must be re-entered here</span>
        </label>
        <div className="btnrow">
          <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <input type="radio" checked={mode === "merge"} onChange={() => setMode("merge")} />
            merge (upsert into current catalog)
          </label>
          <label style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
            <input type="radio" checked={mode === "replace"} onChange={() => setMode("replace")} />
            replace (clear catalog first)
          </label>
          <button className="primary" onClick={run} disabled={busy || !text.trim()}>
            {busy ? "Importing…" : "Import"}
          </button>
          <Link className="btn" to="/sources/export">Export instead →</Link>
        </div>
      </div>
    </>
  );
}
