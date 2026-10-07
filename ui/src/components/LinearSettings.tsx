import { useEffect, useState } from "react";

import { api } from "../api";
import { TimeAgo, usePolling } from "./bits";

// Settings, Linear (TR-408): one Linear connection for the whole cell. Projects whose tickets live
// in Linear use it to keep their ticket list in sync; Tares only reads from Linear.

export default function LinearSettings() {
  const { data, error, reload } = usePolling(() => api.linear(), 30000);
  const [back] = useState(() => {
    const q = new URLSearchParams(window.location.search);
    return { event: q.get("event") ?? "", error: q.get("error") ?? "" };
  });
  useEffect(() => {   // shown once; a reload starts clean
    const url = new URL(window.location.href);
    if (!url.searchParams.has("event") && !url.searchParams.has("error")) return;
    url.searchParams.delete("event"); url.searchParams.delete("error");
    window.history.replaceState(null, "", url.toString());
  }, []);
  const [key, setKey] = useState("");
  const [showApp, setShowApp] = useState(false);
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState<string>();
  const [err, setErr] = useState<string>();
  const callback = `${window.location.origin}/api/linear/oauth/callback`;

  const run = async (what: string, fn: () => Promise<unknown>) => {
    setBusy(what); setErr(undefined);
    try { await fn(); reload(); } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(undefined);
  };
  const signIn = () => run("signin", async () => {
    window.location.href = (await api.linearOauthStart()).url;
  });

  if (error && !data) return <div className="alert error">{String(error)}</div>;
  if (!data) return <div className="panel"><div className="muted">loading…</div></div>;
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Linear</h3>
      <p className="help" style={{ marginTop: 0 }}>
        Projects can keep their tickets in Linear. Tares reads them and keeps each project's ticket
        list in sync, about once a minute; it never writes to Linear.
      </p>
      {back.event === "connected" && <div className="alert ok" role="status">Linear is connected.</div>}
      {back.error && <div className="alert error">{back.error}</div>}
      {err && <div className="alert error">{err}</div>}
      {data.connected ? (
        <>
          <p>
            Connected{data.account?.org && <> to <strong>{data.account.org}</strong></>}
            {data.account?.name && <> as {data.account.name}</>}
            {" "}with {data.kind === "oauth" ? "Linear sign-in" : "a personal API key"}
            {data.connected_at && <span className="help"> · since <TimeAgo ts={data.connected_at} /></span>}.
          </p>
          <div className="btnrow">
            <button type="button" className="danger" disabled={!!busy}
                    onClick={() => run("disconnect", () => api.linearDisconnect())}>
              {busy === "disconnect" ? "disconnecting…" : "Disconnect"}</button>
          </div>
          <p className="help">Projects linked to Linear keep their tickets and stop syncing until Linear is connected again.</p>
        </>
      ) : (
        <>
          {data.oauth_available && (
            <div className="btnrow" style={{ marginBottom: 14 }}>
              <button type="button" className="primary" disabled={!!busy} onClick={signIn}>
                {busy === "signin" ? "opening Linear…" : "Sign in with Linear"}</button>
            </div>
          )}
          <label className="field" style={{ maxWidth: 520 }}>
            <span className="lbl">{data.oauth_available ? "or paste a personal API key" : "personal API key"}</span>
            <input type="password" className="mono" placeholder="lin_api_…" value={key}
                   onChange={(e) => setKey(e.target.value)} autoComplete="off" />
            <span className="help">In Linear: Settings, Security and access, Personal API keys. Read access is enough.</span>
          </label>
          <div className="btnrow">
            <button type="button" className={data.oauth_available ? "" : "primary"} disabled={!key.trim() || !!busy}
                    onClick={() => run("key", async () => { await api.linearConnectKey(key.trim()); setKey(""); })}>
              {busy === "key" ? "checking with Linear…" : "Connect"}</button>
          </div>
          {!data.client_id_from_env && (
            <div style={{ marginTop: 18 }}>
              {!showApp ? (
                <button type="button" className="linklike" onClick={() => { setShowApp(true); setClientId(data.client_id); }}>
                  {data.oauth_available ? "Change the Linear OAuth application" : "Set up sign-in with Linear instead"}
                </button>
              ) : (
                <div className="panel" style={{ marginTop: 8 }}>
                  <p className="help" style={{ marginTop: 0 }}>
                    Sign-in with Linear needs an OAuth application in Linear (Settings, API, OAuth
                    applications). Give it this callback URL, then paste its client id here.
                  </p>
                  <div className="field"><span className="lbl">callback URL</span>
                    <code className="mono" style={{ userSelect: "all" }}>{callback}</code></div>
                  <label className="field" style={{ maxWidth: 520 }}>
                    <span className="lbl">client id</span>
                    <input type="text" className="mono" value={clientId} onChange={(e) => setClientId(e.target.value)} />
                  </label>
                  <label className="field" style={{ maxWidth: 520 }}>
                    <span className="lbl">client secret (optional)</span>
                    <input type="password" className="mono" value={clientSecret} placeholder="leave empty to keep it as it is"
                           onChange={(e) => setClientSecret(e.target.value)} autoComplete="off" />
                    <span className="help">Sign-in works without it.</span>
                  </label>
                  <div className="btnrow">
                    <button type="button" className="primary" disabled={!!busy}
                            onClick={() => run("app", async () => {
                              await api.linearOauthApp(clientId.trim(), clientSecret.trim() || undefined);
                              setShowApp(false); setClientSecret("");
                            })}>Save</button>
                    <button type="button" onClick={() => setShowApp(false)}>Cancel</button>
                  </div>
                </div>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
