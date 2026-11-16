import { useCallback, useEffect, useState } from "react";

import { api } from "../../api";
import { type Cloud, cloudLink } from "../../cloud";
import {
  Banner, Button, Dialog, EmptyState, Input, ListRow, Panel, SkeletonRows, StatusDot, StatusLabel,
} from "../../components/edition";
import { ageWords } from "../../lib/time";
import type { GithubAppTest, GithubCredential } from "../../types";

// How Tares reads and acts on GitHub, on the edition kit (TR-1392). Factory Settings > GitHub and
// the platform Settings page (/settings/advanced?tab=github, which the AI SRE edition opens) both
// show this. Two kinds of account, each stored once and picked by name:
//   * the GitHub App: created here with GitHub's manifest flow and installed on an organization;
//     every event of its repositories arrives by webhook, and agents act as the App. On Tares
//     Cloud the App is GlassFlow's, and "Connect GitHub" replaces "Create GitHub App".
//   * a personal token: polls the repositories you add for commits and pull requests.
// No token, key or secret ever comes back from the daemon.
//
// On Tares Cloud (github_connect_url set) an App row is a link the control plane holds: picking
// repositories and disconnecting happen on its pages, which push the result back here, so this
// panel links to them instead of changing the local copy (TR-366, TR-372).

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";
const CODE = "font-mono text-[12px] text-foreground";
const FORM = "grid gap-4 border border-border bg-muted/40 p-4";

const messageOf = (e: unknown) => String((e as Error)?.message ?? e);
/** The daemon's refusal, as a sentence. */
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

const isApp = (c: GithubCredential) => c.kind === "app" || c.kind === "app_broker";

type Back = { event: string; error: string; github: string; account: string; detail: string };
type Test = { busy?: boolean } & Partial<GithubAppTest>;
type Pending = { action: string; manifest: string; warning: string };

/** What GitHub sent the browser back with (?event=created|installed|requested, ?error=). Read once;
 *  the address drops it, so a reload starts clean. */
function useBack(): Back {
  const [back] = useState<Back>(() => {
    const q = new URLSearchParams(window.location.search);
    return { event: q.get("event") ?? "", error: q.get("error") ?? "", github: q.get("github") ?? "",
             account: q.get("account") ?? "", detail: q.get("detail") ?? "" };
  });
  useEffect(() => {
    if (!back.event && !back.error) return;
    const url = new URL(window.location.href);
    for (const k of ["event", "error", "github", "account", "detail"]) url.searchParams.delete(k);
    window.history.replaceState(null, "", url.toString());
  }, [back]);
  return back;
}

/** GitHub's manifest flow: POST the manifest as a form, so the browser lands on GitHub's "create
 *  App" page with everything filled in; GitHub sends it back to the daemon's callback. */
function postManifest({ action, manifest }: Pending) {
  const form = document.createElement("form");
  form.method = "post"; form.action = action;
  const field = document.createElement("input");
  field.type = "hidden"; field.name = "manifest"; field.value = manifest;
  form.appendChild(field); document.body.appendChild(form); form.submit();
}

/** The repositories a Tares Cloud App row follows: names without the owner when it is the
 *  installation's account, the first three and a count, every full name on hover. */
function repoWords(repos: string[], account: string) {
  const own = account.toLowerCase();
  const names = repos.map((r) => {
    const [owner, name] = r.split("/");
    return owner.toLowerCase() === own && name ? name : r;
  });
  return names.slice(0, 3).join(", ") + (names.length > 3 ? ` and ${names.length - 3} more` : "");
}

/** `title` and `intro` for a page with no heading of its own (the platform Settings page). */
export function GithubPanel({ cloud, title, intro = false }: { cloud: Cloud; title: string; intro?: boolean }) {
  const connectUrl = cloud.health?.github_connect_url || undefined;
  const canConnect = cloud.isOwner !== false;   // unknown: show, the control plane checks
  const back = useBack();
  const [creds, setCreds] = useState<GithubCredential[]>();
  const [loadErr, setLoadErr] = useState<string>();
  const [adding, setAdding] = useState<"" | "token" | "app">("");
  const [formErr, setFormErr] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [tests, setTests] = useState<Record<string, Test>>({});
  const [confirmDelete, setConfirmDelete] = useState<GithubCredential>();
  const [rotating, setRotating] = useState<string>();
  const [pending, setPending] = useState<Pending>();

  const load = useCallback(() => api.githubCredentials()
    .then((r) => { setCreds(r.credentials); setLoadErr(undefined); })
    .catch((e) => setLoadErr(messageOf(e))), []);
  useEffect(() => { void load(); }, [load]);

  const open = (kind: "token" | "app") => { setAdding(kind); setFormErr(undefined); };
  const close = () => { setAdding(""); setFormErr(undefined); };

  const install = async (n: string) => {
    try { window.location.href = (await api.githubAppInstallLink(n)).url; }
    catch (e) { setLoadErr(messageOf(e)); }
  };

  const test = async (n: string) => {
    setTests((t) => ({ ...t, [n]: { busy: true } }));
    try {
      const r = await api.testGithubCredential(n);
      setTests((t) => ({ ...t, [n]: r }));
      if (r.ok) void load();
    } catch (e) {
      setTests((t) => ({ ...t, [n]: { ok: false, error: messageOf(e) } }));
    }
  };

  const remove = async (n: string) => {
    setBusy(true);
    try { await api.deleteGithubCredential(n); await load(); setConfirmDelete(undefined); }
    catch (e) { setLoadErr(messageOf(e)); setConfirmDelete(undefined); }
    setBusy(false);
  };

  const backCred = back.github ? creds?.find((c) => c.name === back.github) : undefined;
  const action = !adding && (
    <div className="flex flex-wrap gap-2">
      {connectUrl
        ? canConnect && <Button asChild><a href={cloudLink(connectUrl, {}, "github")} data-testid="github-connect">Connect GitHub</a></Button>
        : <Button onClick={() => open("app")} data-testid="github-create-app">Create GitHub App</Button>}
      <Button variant="secondary" onClick={() => open("token")} data-testid="github-add-token">Add a personal token</Button>
    </div>
  );

  return (
    <Panel title={title} action={action || undefined} data-testid="github">
      <div className="grid gap-4">
        {intro && (
          <p className={TEXT}>
            With the GitHub App, every event of the repositories it is installed on reaches Tares as it happens:
            pull requests, pushes, reviews, comments, issues, releases and CI results. Agents act as the App. A personal
            token polls the repositories you add for commits and pull requests opened, merged or closed. Either one is
            picked by name on connections and MCP servers, and no token or key is ever shown again.
          </p>
        )}
        {connectUrl && !canConnect && (
          <p className={TEXT} data-testid="github-owner-only">
            {cloud.current?.owner_email
              ? <>Only the workspace owner, <span className="text-foreground" data-user-text>{cloud.current.owner_email}</span>, can connect GitHub.</>
              : "Only the workspace owner can connect GitHub."}
          </p>
        )}

        {back.error && <Banner tone="error">{`GitHub said: ${back.error}`}</Banner>}
        {back.event === "created" && (
          <Banner action={backCred && <Button onClick={() => void install(back.github)}>Install on GitHub</Button>}>
            <span>The GitHub App <code className={CODE} data-user-text>{back.github}</code> is created. Install it on the
              organization whose repositories Tares should watch.</span>
          </Banner>
        )}
        {back.event === "installed" && (
          <Banner>
            <span><code className={CODE} data-user-text>{back.github}</code> is installed
              {back.account && <> on <code className={CODE} data-user-text>{back.account}</code></>}. Its events
              arrive in the GitHub connection.</span>
          </Banner>
        )}
        {back.event === "requested" && <Banner>{`Install requested: ${back.detail}.`}</Banner>}
        {loadErr && creds && <Banner tone="error">{loadErr}</Banner>}

        {adding === "app" && (
          <AppForm busy={busy} error={formErr} onCancel={close}
                   onSubmit={async (body) => {
                     setBusy(true); setFormErr(undefined);
                     try {
                       const r = await api.createGithubApp(body);
                       const p = { action: r.action, manifest: r.manifest, warning: r.warning ?? "" };
                       if (p.warning) { setPending(p); setBusy(false); } else postManifest(p);
                     } catch (e) { setFormErr(sentence(messageOf(e))); setBusy(false); }
                   }} />
        )}
        {adding === "token" && (
          <TokenForm busy={busy} error={formErr} onCancel={close}
                     onSubmit={async (body) => {
                       setBusy(true); setFormErr(undefined);
                       try { await api.createGithubCredential(body); close(); await load(); }
                       catch (e) { setFormErr(sentence(messageOf(e))); }
                       setBusy(false);
                     }} />
        )}

        {!creds ? (
          loadErr ? <Banner tone="error">{loadErr}</Banner>
            : <SkeletonRows count={2} />
        ) : creds.length === 0 ? (
          !adding && (
            <EmptyState data-testid="github-empty">
              {`Nothing connected yet. ${!connectUrl ? "Create the GitHub App, or add a personal token."
                : canConnect ? "Connect GitHub, or add a personal token." : "You can add a personal token."}`}
            </EmptyState>
          )
        ) : (
          <div className="-mt-2 [--row-x:0px]">
            {creds.map((c) => (
              <AccountRow key={c.name} c={c} connectUrl={connectUrl} canConnect={canConnect}
                          test={tests[c.name]} onTest={() => void test(c.name)} onInstall={() => void install(c.name)}
                          rotating={rotating === c.name} onRotate={() => setRotating(rotating === c.name ? undefined : c.name)}
                          onRotated={async () => { setRotating(undefined); await load(); }}
                          onDelete={() => setConfirmDelete(c)} />
            ))}
          </div>
        )}
      </div>

      {confirmDelete && (
        <Dialog open onOpenChange={(o) => { if (!o && !busy) setConfirmDelete(undefined); }} data-testid="github-delete-dialog"
                title={`Delete ${confirmDelete.name}?`}
                description={(confirmDelete.sources.length + confirmDelete.mcp_servers.length > 0
                  ? `${usedBy(confirmDelete)} use it. They stop signing in to GitHub until you pick another account for them.`
                  : "Nothing uses it.")
                  + (confirmDelete.kind === "app" ? " The App itself stays on GitHub; uninstall or delete it there too." : "")}
                footer={<>
                  <Button variant="secondary" disabled={busy} onClick={() => setConfirmDelete(undefined)}>Cancel</Button>
                  <Button busy={busy} onClick={() => void remove(confirmDelete.name)} data-testid="github-delete-confirm">Delete</Button>
                </>} />
      )}
      {pending && (
        <Dialog open onOpenChange={(o) => { if (!o) setPending(undefined); }} data-testid="github-app-warning"
                title="Create the App anyway?" description={pending.warning}
                footer={<>
                  <Button variant="secondary" onClick={() => setPending(undefined)}>Cancel</Button>
                  <Button onClick={() => postManifest(pending)}>Continue on GitHub</Button>
                </>} />
      )}
    </Panel>
  );
}

/** "2 connections and 1 MCP server" */
function usedBy(c: GithubCredential) {
  return [c.sources.length > 0 && plural(c.sources.length, "connection", "connections"),
          c.mcp_servers.length > 0 && plural(c.mcp_servers.length, "MCP server", "MCP servers")]
    .filter(Boolean).join(" and ");
}

function AppForm({ busy, error, onCancel, onSubmit }: {
  busy: boolean; error?: string; onCancel: () => void;
  onSubmit: (body: { name: string; org: string; app_name: string; public_url: string }) => Promise<void>;
}) {
  const [org, setOrg] = useState("");
  const [name, setName] = useState("github-app");
  const [publicUrl, setPublicUrl] = useState(window.location.origin);
  const [appName, setAppName] = useState("");
  const ok = Boolean(name.trim()) && !busy;
  return (
    <form className={FORM} data-testid="github-app-form"
          onSubmit={(e) => { e.preventDefault(); if (ok) void onSubmit({ name: name.trim(), org: org.trim(), app_name: appName.trim(), public_url: publicUrl.trim() }); }}>
      <p className={TEXT}>
        Opens GitHub with the App filled in: its permissions, the events it sends and where it sends them. You
        confirm there, then install it on your organization.
      </p>
      <div className="grid items-start gap-4 sm:grid-cols-2">
        <Input label="GitHub organization" hint="Leave it empty for your own account." className="font-mono" spellCheck={false}
               placeholder="acme" value={org} onChange={(e) => setOrg(e.target.value)} />
        <Input label="Name in Tares" hint="What connections and MCP servers pick it by." className="font-mono" spellCheck={false}
               value={name} error={error} onChange={(e) => setName(e.target.value)} />
      </div>
      <details className="grid gap-3">
        <summary className="cursor-pointer text-[13px] text-muted-foreground">Address and App name</summary>
        <div className="mt-3 grid items-start gap-4 sm:grid-cols-2">
          <Input label="This Tares, as GitHub reaches it" className="font-mono" spellCheck={false}
                 value={publicUrl} onChange={(e) => setPublicUrl(e.target.value)} />
          <Input label="App name on GitHub" hint="Unique on GitHub. Empty: suggested from the address." className="font-mono"
                 spellCheck={false} value={appName} onChange={(e) => setAppName(e.target.value)} />
        </div>
      </details>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" busy={busy} disabled={!name.trim()}>Continue on GitHub</Button>
        <Button variant="secondary" disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}

function TokenForm({ busy, error, onCancel, onSubmit }: {
  busy: boolean; error?: string; onCancel: () => void;
  onSubmit: (body: { name: string; token: string; api_url: string }) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [apiUrl, setApiUrl] = useState("");
  const ok = Boolean(name.trim() && token.trim()) && !busy;
  return (
    <form className={FORM} data-testid="github-token-form"
          onSubmit={(e) => { e.preventDefault(); if (ok) void onSubmit({ name: name.trim(), token: token.trim(), api_url: apiUrl.trim() }); }}>
      <p className={TEXT}>
        A fine-grained token with the repositories you want and Metadata, Contents and Pull requests read (write
        on a repository an agent should change). A classic token needs the <code className={CODE}>repo</code> scope
        for private repositories.
      </p>
      <div className="grid items-start gap-4 sm:grid-cols-2">
        <Input label="Name" hint="What connections and MCP servers pick it by." className="font-mono" spellCheck={false}
               placeholder="github" value={name} error={error} onChange={(e) => setName(e.target.value)} />
        <Input label="Token" type="password" autoComplete="new-password" className="font-mono" spellCheck={false}
               hint="Starts with github_pat_ or ghp_. Once saved it is never shown again."
               value={token} onChange={(e) => setToken(e.target.value)} />
      </div>
      <Input label="API URL" hint="GitHub Enterprise only, for example https://github.example.com/api/v3." className="font-mono"
             spellCheck={false} value={apiUrl} onChange={(e) => setApiUrl(e.target.value)} />
      <div className="flex flex-wrap gap-2">
        <Button type="submit" busy={busy} disabled={!name.trim() || !token.trim()}>Save</Button>
        <Button variant="secondary" disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}

function AccountRow({ c, connectUrl, canConnect, test, onTest, onInstall, rotating, onRotate, onRotated, onDelete }: {
  c: GithubCredential; connectUrl?: string; canConnect: boolean; test?: Test;
  onTest: () => void; onInstall: () => void; rotating: boolean; onRotate: () => void;
  onRotated: () => Promise<void>; onDelete: () => void;
}) {
  const insts = c.installations ?? [];
  const d = c.deliveries;
  // a Tares Cloud App row on a cell that knows where the control plane's pages are
  const cloudApp = c.kind === "app_broker" && !!connectUrl;
  const iid = insts[0]?.id;
  const repos = c.repositories ?? [];
  const repoLink = (params: Record<string, string>) =>
    cloudLink(connectUrl!, { installation: String(iid), ...params }, "github");
  const uses = usedBy(c);

  const who = isApp(c)
    ? <>{`GitHub App${c.broker ? " (Tares Cloud)" : ""}`}{insts.length > 0
        ? <> on <span className="font-mono" data-user-text>{insts.map((i) => i.account || i.id).join(", ")}</span></>
        : ", not installed yet"}
      {cloudApp && (repos.length > 0
        ? <>: <span className="font-mono" title={repos.join("\n")} data-user-text>{repoWords(repos, insts[0]?.account || c.account)}</span></>
        : ", no repositories picked yet")}</>
    : <>Personal token{c.account && <> of <span className="font-mono" data-user-text>{c.account}</span></>}</>;

  const events = !isApp(c) ? "Polls the repositories you add."
    : !d ? "No connection receives its events."
    : d.last_at ? `Last event ${ageWords(Date.now() - d.last_at * 1000)}.`
    : "No events yet.";

  return (
    <ListRow className="hover:bg-transparent" data-testid="github-row" data-name={c.name}>
      <div className="grid min-w-0 flex-[1_1_320px] gap-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="font-mono text-sm font-semibold" data-user-text>{c.name}</span>
          {c.api_url && <span className="font-mono text-xs text-muted-foreground" data-user-text>{c.api_url}</span>}
        </div>
        <p className="m-0 text-[13px]" data-testid="github-who">{who}</p>
        <p className="m-0 flex flex-wrap items-baseline gap-x-1 text-[13px] text-muted-foreground" data-testid="github-events">
          <span>{events}</span>
          {d && d.rejected_signature > 0 && (
            <StatusLabel tone="bad" title="Events refused because their signature did not match the App's webhook secret.">
              {`${d.rejected_signature} refused`}
            </StatusLabel>
          )}
          <span title={[...c.sources, ...c.mcp_servers].join("\n") || undefined}>{uses ? `Used by ${uses}.` : "Nothing uses it yet."}</span>
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        {c.kind === "app" && c.slug && (
          <Button variant={insts.length === 0 ? "primary" : "secondary"} onClick={onInstall}>
            {insts.length === 0 ? "Install on GitHub" : "Add an organization"}</Button>
        )}
        {cloudApp && canConnect && iid != null && (
          <Button asChild variant={repos.length === 0 ? "primary" : "secondary"}>
            <a href={repoLink({})}>{repos.length === 0 ? "Pick repositories" : "Change repositories"}</a>
          </Button>
        )}
        <Button variant="secondary" busy={test?.busy} onClick={onTest}>Test</Button>
        {!isApp(c) && <Button variant="secondary" aria-expanded={rotating} onClick={onRotate}>Rotate</Button>}
        {cloudApp
          ? canConnect && iid != null && <Button asChild variant="quiet" className="text-err hover:text-err"><a href={repoLink({ action: "disconnect" })}>Disconnect</a></Button>
          : <Button variant="quiet" className="text-err hover:text-err" onClick={onDelete}>Delete</Button>}
      </div>
      {test && !test.busy && <TestResult t={test} />}
      {rotating && <RotateForm name={c.name} onCancel={onRotate} onDone={onRotated} />}
    </ListRow>
  );
}

function TestResult({ t }: { t: Test }) {
  return (
    <p className="m-0 flex basis-full flex-wrap items-baseline gap-2 text-[13px]" data-testid="github-test" role="status">
      <StatusDot tone={t.ok ? "ready" : "bad"} />
      {!t.ok ? <span>Failed: <span className="font-mono" data-user-text>{t.error}</span></span>
        : t.installations ? (
          <span>{`It works. ${t.installations.map((i) => `${i.account}: ${plural(i.repos, "repository", "repositories")}`).join(", ")}.`}</span>
        ) : (
          <span>It works, signed in as <span className="font-mono" data-user-text>{t.login}</span>
            {t.scopes && t.scopes.length > 0 && <> with the scopes <span className="font-mono" data-user-text>{t.scopes.join(", ")}</span></>}.</span>
        )}
    </p>
  );
}

function RotateForm({ name, onCancel, onDone }: { name: string; onCancel: () => void; onDone: () => Promise<void> }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const save = async () => {
    setBusy(true); setErr(undefined);
    try { await api.updateGithubCredential(name, { name, token: token.trim() }); setToken(""); await onDone(); }
    catch (e) { setErr(sentence(messageOf(e))); }
    setBusy(false);
  };
  return (
    <form className="grid max-w-[560px] basis-full gap-3" data-testid="github-rotate"
          onSubmit={(e) => { e.preventDefault(); if (token.trim() && !busy) void save(); }}>
      <Input label="New token" type="password" autoComplete="new-password" className="font-mono" spellCheck={false}
             hint="Replaces the saved one. Connections and MCP servers that use this account keep working." error={err}
             value={token} onChange={(e) => { setToken(e.target.value); setErr(undefined); }} />
      <div className="flex flex-wrap gap-2">
        <Button type="submit" busy={busy} disabled={!token.trim()}>Save new token</Button>
        <Button variant="secondary" disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
