import { useState } from "react";

import { api } from "../api";
import { type Cloud, cloudLink } from "../cloud";
import { TimeAgo, usePolling } from "./bits";
import OwnerOnly from "./OwnerOnly";

// Settings, Linear (TR-408): one Linear connection for the whole cell. Projects whose tickets live
// in Linear use it to keep their ticket list in sync; Tares only reads from Linear.
//
// Two ways in: a personal API key, everywhere; and on Tares Cloud (linear_connect_url set) Connect
// Linear, the GlassFlow Linear app. The control plane runs that sign-in, hands the token to this
// cell and renews it; disconnecting goes through it too, so it can revoke the token (TR-537).

export default function LinearSettings({ cloud }: { cloud: Cloud }) {
  const { data, error, reload } = usePolling(() => api.linear(), 30000);
  const connectUrl = cloud.health?.linear_connect_url || undefined;
  const canConnect = cloud.isOwner !== false;   // unknown: show, the control plane checks
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState<string>();
  const [err, setErr] = useState<string>();

  const run = async (what: string, fn: () => Promise<unknown>) => {
    setBusy(what); setErr(undefined);
    try { await fn(); reload(); } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(undefined);
  };

  if (error && !data) return <div className="alert error">{String(error)}</div>;
  if (!data) return <div className="panel"><div className="muted">loading…</div></div>;
  const viaCloud = data.kind === "cloud";
  return (
    <div className="panel">
      <h3 style={{ marginTop: 0 }}>Linear</h3>
      <p className="help" style={{ marginTop: 0 }}>
        Projects can keep their tickets in Linear. Tares reads them and keeps each project's ticket
        list in sync, about once a minute; it never writes to Linear.
      </p>
      {err && <div className="alert error">{err}</div>}
      {data.connected ? (
        <>
          <p>
            Connected{data.account?.org && <> to <strong>{data.account.org}</strong></>}
            {data.account?.name && <> as {data.account.name}</>}
            {!viaCloud && " with a personal API key"}
            {data.connected_at && <span className="help"> · since <TimeAgo ts={data.connected_at} /></span>}.
          </p>
          <div className="btnrow">
            {viaCloud && connectUrl ? (
              canConnect
                ? <a className="btn danger" href={cloudLink(connectUrl, { action: "disconnect" }, "linear")}>Disconnect</a>
                : <OwnerOnly cloud={cloud} what="Linear" />
            ) : (
              <button type="button" className="danger" disabled={!!busy}
                      onClick={() => run("disconnect", () => api.linearDisconnect())}>
                {busy === "disconnect" ? "disconnecting…" : "Disconnect"}</button>
            )}
          </div>
          <p className="help">Projects linked to Linear keep their tickets and stop syncing until Linear is connected again.</p>
        </>
      ) : (
        <>
          {data.error && <div className="alert warn">{data.error}</div>}
          {connectUrl && (
            <div className="btnrow" style={{ marginBottom: 14 }}>
              {canConnect
                ? <a className="btn primary" href={cloudLink(connectUrl, {}, "linear")}>Connect Linear</a>
                : <OwnerOnly cloud={cloud} what="Linear" />}
            </div>
          )}
          <label className="field" style={{ maxWidth: 520 }}>
            <span className="lbl">{connectUrl ? "or paste a personal API key" : "personal API key"}</span>
            <input type="password" className="mono" placeholder="lin_api_…" value={key}
                   onChange={(e) => setKey(e.target.value)} autoComplete="off" />
            <span className="help">In Linear: Settings, Security and access, Personal API keys. Read access is enough.</span>
          </label>
          <div className="btnrow">
            <button type="button" className={connectUrl ? "" : "primary"} disabled={!key.trim() || !!busy}
                    onClick={() => run("key", async () => { await api.linearConnectKey(key.trim()); setKey(""); })}>
              {busy === "key" ? "checking with Linear…" : "Connect"}</button>
          </div>
        </>
      )}
    </div>
  );
}
