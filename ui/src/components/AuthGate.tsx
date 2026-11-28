import { useEffect, useState } from "react";

import { api, auth } from "../api";

// Gates the console when the instance requires a token (TARES_AUTH_TOKEN). The SPA shell is served
// publicly, so we ask /health whether auth is required; any 401 from an API call (token missing or
// expired) flips us back to the login screen via the "tares-auth-required" event.
// A daemon that is up but broken (no database) still answers /health — it just doesn't say "ok".
// A daemon that is down doesn't answer at all. Neither may leave the console blank, so the gate
// always resolves: on timeout or failure we render the app and say what we know.
const HEALTH_TIMEOUT_MS = 6000;

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return Promise.race([
    p,
    new Promise<T>((_, rej) => setTimeout(() => rej(new Error("timeout")), ms)),
  ]);
}

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<"loading" | "ok" | "login">("loading");
  const [banner, setBanner] = useState<string>();

  useEffect(() => {
    let alive = true;
    (async () => {
      const url = new URL(window.location.href);
      const strip = (k: string) => {
        url.searchParams.delete(k);
        window.history.replaceState({}, "", url.pathname + url.search + url.hash);
      };

      // Self-host: `tares up --auth` prints a login URL (…/?token=<root>). Capture it and strip it
      // from the address bar so it doesn't linger in history/bookmarks. A bad token just 401s on the
      // first protected call and bounces to the login screen.
      const t = url.searchParams.get("token");
      if (t) { auth.set(t.trim()); strip("token"); }

      const h = await withTimeout(api.health(), HEALTH_TIMEOUT_MS).catch(() => null);

      // Cloud: the control plane redirects back here with a one-time ?code=. Swap it for the real
      // cell key at the control plane (login_url tells us where). On failure we fall through to the
      // normal auth check below, which re-initiates login.
      const code = url.searchParams.get("code");
      if (code && h?.login_url) {
        try { auth.set(await api.exchange(h.login_url, code)); } catch { /* fall through to login */ }
        strip("code");
      }

      if (!alive) return;

      if (!h) {
        setBanner("Can’t reach the Tares daemon; it may be starting, stopped, or blocked "
                  + "between this browser and the server. Pages will keep retrying.");
      } else if (h.status && h.status !== "ok") {
        setBanner(h.detail
          ? `Tares is ${h.status}: ${h.detail}`
          : `Tares reports status “${h.status}”.`);
      }

      if (h && h.auth_required && !auth.get()) {
        // Cloud cell: bounce to the hosted login, telling it which cell to return to. Self-host:
        // show the paste-token form.
        if (h.login_url) {
          window.location.assign(`${h.login_url}?return=${encodeURIComponent(window.location.host)}`);
          return; // navigating away; leave state as "loading"
        }
        setState("login");
      } else {
        setState("ok");
      }
    })();

    const onAuth = () => setState("login");
    window.addEventListener("tares-auth-required", onAuth);
    return () => { alive = false; window.removeEventListener("tares-auth-required", onAuth); };
  }, []);

  if (state === "loading") {
    return (
      <div className="login-wrap">
        <p className="dim">Connecting to Tares…</p>
      </div>
    );
  }
  if (state === "login") return <Login onAuthed={() => setState("ok")} />;
  return (
    <>
      {children}
      {/* Fixed, not in flow: #root is the sidebar/content flex row, and a banner must not
          become a third column. */}
      {banner && (
        <div className="daemon-banner alert error" role="status">
          {banner}
          <button className="btn" onClick={() => window.location.reload()}>Reload</button>
        </div>
      )}
    </>
  );
}

function Login({ onAuthed }: { onAuthed: () => void }) {
  const [token, setToken] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr("");
    auth.set(token.trim());
    try {
      // /api/whoami takes a credential of any scope: a read or an ingest key signs in too, and a
      // bad one answers 401
      await api.whoami();
      onAuthed();
    } catch {
      auth.clear();
      setErr("Invalid token.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login" onSubmit={submit}>
        <div className="brand">
          <img className="brand-mark" src="/tares-mark.svg" alt="" />
          tares
        </div>
        <p className="muted">This instance requires an access token.</p>
        <input
          type="password"
          placeholder="access token"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          autoFocus
        />
        {err && <div className="login-err">{err}</div>}
        <button className="btn" disabled={busy || !token.trim()}>
          {busy ? "…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
