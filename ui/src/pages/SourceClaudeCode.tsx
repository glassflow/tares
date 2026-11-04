import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { usePolling } from "../components/bits";
import { copyText } from "../editions/copyText";

function Copy({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button
      className="copybtn"
      onClick={() => {
        copyText(text).then(() => { setDone(true); setTimeout(() => setDone(false), 1500); }, () => {});
      }}
    >
      {done ? "copied" : "copy"}
    </button>
  );
}

const INSTALL = "/plugin marketplace add glassflow/tares\n/plugin install tares@tares";

// Claude Code is fed by the Tares plugin (push), not by tailing files — so this page shows the
// install command wired to THIS instance's URL, identical for local and remote.
export default function SourceClaudeCode() {
  const origin = window.location.origin;
  const { data: sources } = usePolling(() => api.sources(), 10000);
  const [enforced, setEnforced] = useState<boolean>();

  useEffect(() => {
    api.health().then((h) => setEnforced(h.auth_required)).catch(() => setEnforced(undefined));
  }, []);

  const connected = sources?.some((s) => s.connector === "claude_code") ?? false;

  return (
    <>
      <h1>Connect Claude Code</h1>
      <p className="subtitle">
        Install the Tares plugin for Claude Code; it streams your sessions into the{" "}
        <span className="mono">claude_code</span> source (keyed by session, with repo / branch /
        model labels; secrets redacted server-side) and adds MCP read-back. Same steps local or remote.
      </p>

      {connected && (
        <div className="panel">
          <div className="pagehead">
            <h2 style={{ margin: 0 }}>Status</h2>
            <span className="badge ok">connected</span>
          </div>
          <p className="help" style={{ marginTop: 8 }}>
            A <span className="mono">claude_code</span> source exists; sessions are streaming in.{" "}
            <Link to="/sources/claude_code">Manage source</Link>.
          </p>
        </div>
      )}

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>1. Install the plugin</h2>
        <p className="help" style={{ marginTop: 0 }}>In Claude Code, run:</p>
        <div className="codeblock">
          <div className="codeblock-head">
            <span>claude code</span>
            <Copy text={INSTALL} />
          </div>
          <pre className="payload">{INSTALL}</pre>
        </div>
      </div>

      <div className="panel">
        <h2 style={{ marginTop: 0 }}>2. Point it at this Tares</h2>
        <p className="help" style={{ marginTop: 0 }}>When the installer prompts you, enter:</p>

        <label className="field">
          <span className="lbl">Tares URL</span>
          <div className="btnrow" style={{ alignItems: "center" }}>
            <code className="payload" style={{ flex: 1, margin: 0 }}>{origin}</code>
            <Copy text={origin} />
          </div>
        </label>

        <label className="field" style={{ marginTop: 12 }}>
          <span className="lbl">
            Auth token{enforced === false && <span className="dim"> (not required)</span>}
          </span>
          {enforced ? (
            <span className="help">
              Create an API key with the <span className="mono">read</span> +{" "}
              <span className="mono">ingest</span> scopes on the{" "}
              <Link to="/settings">Settings page</Link> and paste it here; it lets the plugin
              stream sessions <em>and</em> query Tares over MCP, without full admin rights.
            </span>
          ) : (
            <span className="help">
              This instance is open (no auth configured); leave it blank.
            </span>
          )}
        </label>
      </div>

      <div className="panel">
        <p className="help" style={{ marginTop: 0 }}>
          Sessions stream <strong>from install onward</strong> (existing sessions aren&rsquo;t
          backfilled). Once you run a session, the <span className="mono">claude_code</span> source
          shows up under <Link to="/sources">Sources</Link>.
        </p>
      </div>
    </>
  );
}
