import { useEffect, useState } from "react";

import { api, type LicenceState } from "../api";
import { useLicence } from "../licence";
import { ENTERPRISE_URL } from "./LicenceNeeded";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "2026-11-03" as "3 Nov 2026". Plain, and the same in every time zone. */
export function licenceDate(iso: string | null | undefined): string {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso ?? "");
  if (!m) return iso ?? "";
  return `${Number(m[3])} ${MONTHS[Number(m[2]) - 1] ?? m[2]} ${m[1]}`;
}

const SOURCE: Record<LicenceState["source"], string> = {
  env: "Set in the environment (TARES_LICENSE)",
  settings: "Saved here",
  none: "None",
};

function daysWords(st: LicenceState): string {
  const d = st.days_left;
  if (d === null) return "";
  if (st.expired) return " (ended)";
  if (d <= 0) return " (ends today)";
  return ` (${d} ${d === 1 ? "day" : "days"} left)`;
}

function Line({ label, value, testid }: { label: string; value: string; testid?: string }) {
  return (
    <div className="grid grid-cols-1 gap-x-4 py-1.5 text-sm sm:grid-cols-[160px_minmax(0,1fr)]">
      <span className="help" style={{ margin: 0 }}>{label}</span>
      <span className="min-w-0 [overflow-wrap:anywhere]" data-testid={testid}>{value}</span>
    </div>
  );
}

/** Settings, Enterprise licence: what the cell's licence says, and for an admin the box to paste a key
 *  into. The key text is write-only: it is never shown, here or anywhere. */
export default function LicencePanel() {
  const { state, loading, failed, reload } = useLicence();
  // true or false from whoami; null when whoami did not answer
  const [admin, setAdmin] = useState<boolean | null>();
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const [note, setNote] = useState<string>();

  useEffect(() => {
    api.whoami().then((w) => setAdmin(w.scopes.includes("admin"))).catch(() => setAdmin(null));
  }, []);

  const save = async () => {
    const text = key.trim();
    if (!text) { setErr("Paste the licence key first."); return; }
    setBusy(true); setErr(undefined); setNote(undefined);
    try {
      const r = await api.setLicence(text);
      setKey("");
      if (r.env_in_use) setNote("The key is saved. The one set in the environment is still the one in use.");
      else setNote("The key is saved.");
      await reload();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };
  const remove = async () => {
    setBusy(true); setErr(undefined); setNote(undefined);
    try {
      await api.deleteLicence();
      setNote("The saved key is removed.");
      await reload();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const st = state;
  return (
    <div className="panel" data-testid="licence-panel">
      <h2 style={{ marginTop: 0 }}>Enterprise licence</h2>
      <p className="help" style={{ marginTop: 0 }}>
        The licence turns on the team features: more than one person on a cell, and the screens built for a team.{" "}
        <a href={ENTERPRISE_URL} target="_blank" rel="noreferrer">About the enterprise licence</a>
      </p>
      {!st ? (
        loading && !failed ? <div className="muted">loading…</div> : <div className="alert error">The cell did not answer.</div>
      ) : (
        <>
          <div data-testid="licence-state">
            {st.valid && <>
              <Line label="Organisation" value={st.org ?? ""} testid="licence-org" />
              <Line label="Seats" value={String(st.seats ?? "")} testid="licence-seats" />
              <Line label="People in use" value={String(st.used)} testid="licence-used" />
              <Line label={st.expired ? "Ended" : "Expires"} value={`${licenceDate(st.expires)}${daysWords(st)}`}
                    testid="licence-expires" />
            </>}
            {!st.valid && <p style={{ margin: "0 0 6px" }} data-testid="licence-none">
              {st.source === "none" ? "This cell has no enterprise licence." : "The licence key in use is not valid."}
            </p>}
            <Line label="Key comes from" value={SOURCE[st.source]} testid="licence-source" />
          </div>
          {st.reason && <div className="alert error" role="alert" data-testid="licence-reason">{st.reason}</div>}
          {st.source === "env" && (
            <p className="help" data-testid="licence-env-note">
              The key set in the environment wins over one saved here. To use a saved key, take TARES_LICENSE out of the environment and restart.
            </p>
          )}
        </>
      )}
      {note && <p className="help" role="status" data-testid="licence-note">{note}</p>}
      {admin === false && <p className="help" data-testid="licence-admin-only">Only an admin can change the licence.</p>}
      {admin === null && <p className="help" data-testid="licence-who-failed">The cell did not say who you are, so the licence box is hidden. Reload to try again.</p>}
      {admin && (
        <>
          <label className="field">
            <span className="lbl">licence key</span>
            <textarea rows={4} value={key} data-testid="licence-key" autoComplete="off" spellCheck={false}
                      placeholder="Paste the key you were sent" aria-label="Licence key"
                      onChange={(e) => { setKey(e.target.value); setErr(undefined); }} />
          </label>
          {err && <div className="alert error" role="alert" data-testid="licence-error">{err}</div>}
          <div className="btnrow">
            <button className="primary" data-testid="licence-save" disabled={busy || !key.trim()} onClick={save}>Save</button>
            {st?.stored && <button className="danger" data-testid="licence-remove" disabled={busy} onClick={remove}>Remove</button>}
          </div>
          <p className="help">The key is kept on this cell and never shown again.</p>
        </>
      )}
    </div>
  );
}
