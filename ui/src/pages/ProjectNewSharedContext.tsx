import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { api } from "../api";
import { Combo, Picker } from "../components/bits";
import InfoDialog, { HelpButton } from "../components/InfoDialog";
import type { GithubCredential, Template, Project } from "../types";

// The shared code context wizard: pick the GitHub repos that are the sources of context, pick
// the repo the agent maintains, choose when it runs, click Start. Four steps, then Tares creates
// one commits source per repo, a trigger keyed by repo, the GitHub MCP server the agent
// writes through, and the agent itself. Also serves Edit: with ?edit=<id> the same steps come up
// filled from the instance's params and Start becomes Save.

const RECIPE = "shared_code_context";

type Repo = { full_name: string; default_branch: string; private: boolean; pushed_at: string | null };
type SourceRepo = { repo: string; branch: string };

const STEPS = ["GitHub access", "Source repos", "Context repo", "Trigger and agent"];

const REPO_RE = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

// Mirrors the template's naming (tares/projects/shared_code_context.py): objects are named after the
// context repo, slugged to 24 chars; sources append the source repo slugged to 48.
function slugOf(text: string, n = 24) {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, n) || "project";
}

export default function ProjectNewSharedContext() {
  const navigate = useNavigate();
  const params = useParams();
  const editId = new URLSearchParams(window.location.search).get("edit") ?? undefined;
  const templateKey = params.template ?? RECIPE;

  const [template, setRecipe] = useState<Template>();
  const [templateErr, setRecipeErr] = useState<string>();
  const [existing, setExisting] = useState<Project>();
  const [step, setStep] = useState(0);
  const [err, setErr] = useState<string>();
  const [busy, setBusy] = useState(false);

  // step 1: credential
  const [credentials, setCredentials] = useState<GithubCredential[]>();
  const [credential, setCredential] = useState("");
  const [newCred, setNewCred] = useState(false);
  const [credName, setCredName] = useState("github");
  const [credToken, setCredToken] = useState("");
  const [credApi, setCredApi] = useState("");
  const [tokenHelp, setTokenHelp] = useState(false);
  const [credTest, setCredTest] = useState<{ busy?: boolean; ok?: boolean; login?: string; error?: string }>();

  // step 2: source repos
  const [repos, setRepos] = useState<Repo[]>();
  const [reposErr, setReposErr] = useState<string>();
  const [query, setQuery] = useState("");
  const [sources, setSources] = useState<SourceRepo[]>([]);
  const [paste, setPaste] = useState("");

  // step 3: context repo
  const [contextRepo, setContextRepo] = useState("");
  const [contextBranch, setContextBranch] = useState("");
  const [contextPath, setContextPath] = useState("");
  const [layout, setLayout] = useState<"existing" | "per_repo">("existing");
  const [tree, setTree] = useState<{ path: string; dirs: string[]; markdown: string[]; files: string[]; exists: boolean }>();
  const [treeErr, setTreeErr] = useState<string>();
  const [treeBusy, setTreeBusy] = useState(false);
  const [writeMode, setWriteMode] = useState<"pull_request" | "commit_to_branch">("pull_request");

  // step 4: trigger and agent
  const [trigger, setTrigger] = useState("every_commit");
  const [name, setName] = useState("Shared code context");
  const [model, setModel] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [maxRounds, setMaxRounds] = useState("12");
  const [bootstrap, setBootstrap] = useState(true);

  useEffect(() => {
    api.templates()
      .then((r) => {
        const found = r.templates.find((x) => x.key === templateKey);
        if (!found) setRecipeErr(`this instance has no project named ${templateKey}`);
        setRecipe(found);
        const d = found?.params ?? {};
        if (typeof d.context_path?.default === "string") setContextPath(d.context_path.default as string);
        if (d.max_rounds?.default != null) setMaxRounds(String(d.max_rounds.default));
        if (typeof d.write_mode?.default === "string") setWriteMode(d.write_mode.default as "pull_request");
        if (found?.title && !editId) setName(found.title);
      })
      .catch((e) => setRecipeErr(String((e as Error).message ?? e)));
    api.builtinAgents().then((r) => { setModels(r.models); setDefaultModel(r.default_model); }).catch(() => {});
  }, [templateKey, editId]);

  const loadCredentials = () =>
    api.githubCredentials().then((r) => {
      setCredentials(r.credentials);
      if (!credential && r.credentials.length > 0) setCredential(r.credentials[0].name);
    }).catch((e) => setErr(String((e as Error).message ?? e)));
  useEffect(() => { loadCredentials(); }, []);   // eslint-disable-line react-hooks/exhaustive-deps

  // Edit: fill from the instance's params.
  useEffect(() => {
    if (!editId) return;
    api.project(editId).then((u) => {
      setExisting(u);
      const p = u.params as Record<string, unknown>;
      setName(u.name);
      if (typeof p.credential === "string") setCredential(p.credential);
      if (Array.isArray(p.source_repos)) {
        setSources((p.source_repos as { repo: string; branch?: string }[])
          .map((r) => ({ repo: r.repo, branch: r.branch ?? "" })));
      }
      if (typeof p.context_repo === "string") setContextRepo(p.context_repo);
      if (typeof p.context_branch === "string") setContextBranch(p.context_branch);
      if (typeof p.context_path === "string") setContextPath(p.context_path);
      if (p.layout === "per_repo") setLayout("per_repo");
      if (p.bootstrap === false) setBootstrap(false);
      if (p.write_mode === "commit_to_branch") setWriteMode("commit_to_branch");
      if (typeof p.trigger === "string") setTrigger(p.trigger);
      if (typeof p.model === "string") setModel(p.model);
      if (p.max_rounds != null) setMaxRounds(String(p.max_rounds));
    }).catch((e) => setErr(String((e as Error).message ?? e)));
  }, [editId]);

  // The repo list needs a credential. Failing to list is not fatal: the paste box always works.
  useEffect(() => {
    if (!credential) { setRepos(undefined); return; }
    let live = true;
    setRepos(undefined); setReposErr(undefined);
    api.githubCredentialRepos(credential)
      .then((r) => { if (live) setRepos(r.repos); })
      .catch((e) => { if (live) setReposErr(String((e as Error).message ?? e)); });
    return () => { live = false; };
  }, [credential]);

  const addCredential = async () => {
    setBusy(true); setErr(undefined);
    try {
      await api.createGithubCredential({ name: credName.trim(), token: credToken,
                                         api_url: credApi.trim() || undefined });
      setCredential(credName.trim());
      setNewCred(false); setCredToken("");
      await loadCredentials();
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const testCredential = async () => {
    if (!credential) return;
    setCredTest({ busy: true });
    try {
      const r = await api.testGithubCredential(credential);
      setCredTest({ ok: r.ok, login: r.login, error: r.error });
    } catch (e) { setCredTest({ ok: false, error: String((e as Error).message ?? e) }); }
  };

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (repos ?? []).filter((r) => !q || r.full_name.toLowerCase().includes(q));
  }, [repos, query]);

  const selected = (full: string) => sources.some((s) => s.repo === full);
  const toggle = (r: Repo) => {
    setSources((cur) => selected(r.full_name)
      ? cur.filter((s) => s.repo !== r.full_name)
      : [...cur, { repo: r.full_name, branch: r.default_branch }]);
  };
  const setBranch = (repo: string, branch: string) =>
    setSources((cur) => cur.map((s) => (s.repo === repo ? { ...s, branch } : s)));
  const removeRepo = (repo: string) => setSources((cur) => cur.filter((s) => s.repo !== repo));

  const addPasted = () => {
    const lines = paste.split(/[\n,\s]+/).map((l) => l.trim()).filter(Boolean);
    const bad = lines.filter((l) => !REPO_RE.test(l));
    if (bad.length) { setErr(`not owner/name: ${bad.slice(0, 3).join(", ")}`); return; }
    setErr(undefined);
    setSources((cur) => {
      const have = new Set(cur.map((s) => s.repo));
      const add = lines.filter((l) => !have.has(l)).map((l) => {
        const known = repos?.find((r) => r.full_name === l);
        return { repo: l, branch: known?.default_branch ?? "" };
      });
      return [...cur, ...add];
    });
    setPaste("");
  };

  const contextCandidates = useMemo(() =>
    (repos ?? []).filter((r) => !selected(r.full_name)).map((r) => r.full_name),
    [repos, sources]);   // eslint-disable-line react-hooks/exhaustive-deps

  const slug = slugOf(contextRepo);
  const preview = [
    ...sources.map((s) => ({ kind: "source", name: `ctx_${slug}_${slugOf(s.repo, 48)}`,
                              note: `commits on ${s.repo}${s.branch ? ` (${s.branch})` : ""}` })),
    { kind: "trigger", name: `ctx_${slug}_changes`, note: "fires when commits land, once per repo per 5 minutes" },
    { kind: "mcp server", name: `ctx_${slug}_github`, note: `GitHub's hosted MCP with credential ${credential || "?"}; the agent writes to ${contextRepo || "the context repo"} through it` },
    { kind: "agent", name: `ctx_${slug}_maintainer`, note: `reads each diff, updates the pages under ${contextPath.trim() || "/"} in ${contextRepo || "the context repo"}, ${writeMode === "pull_request" ? "opens a pull request" : "commits to the branch"}` },
  ];

  const canNext = [
    !!credential,
    sources.length > 0 && sources.length <= 50 && sources.every((s) => REPO_RE.test(s.repo)),
    REPO_RE.test(contextRepo) && !selected(contextRepo),
    !!name.trim(),
  ];

  // Look inside the context repo once it is picked, so the path and layout default to what is
  // there instead of assuming a fresh folder.
  const treeKey = `${credential}|${contextRepo}|${contextBranch}|${contextPath}`;
  useEffect(() => {
    if (!credential || !REPO_RE.test(contextRepo)) { setTree(undefined); return; }
    let live = true;
    setTreeBusy(true); setTreeErr(undefined);
    api.githubCredentialTree(credential, contextRepo, contextBranch.trim(), contextPath.trim())
      .then((t) => { if (live) setTree(t); })
      .catch((e) => { if (live) { setTree(undefined); setTreeErr(String((e as Error).message ?? e)); } })
      .finally(() => { if (live) setTreeBusy(false); });
    return () => { live = false; };
  }, [treeKey]);   // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async () => {
    setBusy(true); setErr(undefined);
    const p: Record<string, unknown> = {
      credential,
      source_repos: sources.map((s) => (s.branch ? s : { repo: s.repo })),
      context_repo: contextRepo,
      context_path: contextPath.trim(),
      layout,
      bootstrap,
      trigger,
      write_mode: writeMode,
    };
    if (contextBranch.trim()) p.context_branch = contextBranch.trim();
    if (model) p.model = model;
    if (maxRounds.trim()) p.max_rounds = Number(maxRounds);
    try {
      const u = editId
        ? await api.updateProject(editId, { params: p, name: name.trim() })
        : await api.createProject({ template: templateKey, name: name.trim(), params: p });
      navigate(`/projects/${encodeURIComponent(u.id)}`, { replace: true });
    } catch (e) { setErr(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  const credOptions = (credentials ?? []).map((c) => c.name);

  return (
    <>
      <div className="pagehead">
        <div>
          <h1>{editId ? `Edit ${existing?.name ?? "project"}` : template?.title ?? "Shared code context"}</h1>
          <p className="subtitle">
            {template?.description ?? "Pick the repos the context comes from and the repo that holds it. Tares keeps it current."}
          </p>
        </div>
      </div>

      {templateErr && <div className="alert error">{templateErr} · <Link to="/projects">back to projects</Link></div>}

      <ol className="uc-steps" aria-label="steps">
        {STEPS.map((s, i) => (
          <li key={s} className={i === step ? "cur" : i < step ? "done" : ""}
              onClick={() => { if (i < step) setStep(i); }}>
            <span className="n">{i + 1}</span> {s}
          </li>
        ))}
      </ol>

      {err && <div className="alert error">{err}</div>}

      {step === 0 && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>GitHub access</h2>
          <p className="help">
            One token, stored once under <Link to="/settings?tab=github">Settings</Link>, used by every source this project creates and by
            the agent when it writes to the context repo.
          </p>
          {credentials === undefined && <div className="dim">loading…</div>}
          {credentials && credentials.length > 0 && !newCred && (
            <div className="field">
              <span className="lbl">credential</span>
              <Picker value={credential} onChange={(v) => { setCredential(v); setCredTest(undefined); }}
                      options={credential && !credOptions.includes(credential) ? [credential, ...credOptions] : credOptions}
                      labels={{ ...Object.fromEntries(
                        (credentials ?? []).map((c) => [c.name, c.account ? `${c.name} (${c.account})` : c.name])) }}
                      ariaLabel="credential" />
              <div className="btnrow" style={{ marginTop: 8 }}>
                <button onClick={testCredential} disabled={!credential || credTest?.busy}>
                  {credTest?.busy ? "testing…" : "Test"}
                </button>
                <button onClick={() => setNewCred(true)}>Add another</button>
                {credTest && !credTest.busy && (credTest.ok
                  ? <span className="badge ok">signed in as {credTest.login}</span>
                  : <span className="badge error">{credTest.error ?? "failed"}</span>)}
              </div>
            </div>
          )}
          {(newCred || (credentials && credentials.length === 0)) && (
            <div>
              <div className="row2">
                <label className="field">
                  <span className="lbl">name</span>
                  <input type="text" autoComplete="off" data-1p-ignore data-lpignore="true" value={credName} onChange={(e) => setCredName(e.target.value)} />
                </label>
                <label className="field">
                  <span className="lbl">GitHub Enterprise URL</span>
                  <input type="text" autoComplete="off" data-1p-ignore data-lpignore="true" className="mono" value={credApi} placeholder="https://github.example.com/api/v3"
                         onChange={(e) => setCredApi(e.target.value)} />
                  <span className="help">optional; leave empty for github.com</span>
                </label>
              </div>
              <div className="field">
                <span className="lbl"><label htmlFor="uc-token">token</label> <HelpButton onClick={() => setTokenHelp(true)} label="Which permissions does the token need?" /></span>
                <input id="uc-token" type="password" className="mono" autoComplete="new-password" value={credToken}
                       placeholder="github_pat_…" onChange={(e) => setCredToken(e.target.value)} />
                <span className="help">stored as a secret, never shown again; rotate it under Settings</span>
              </div>
              <div className="btnrow">
                <button className="primary" onClick={addCredential}
                        disabled={busy || !credName.trim() || !credToken.trim()}>Save credential</button>
                {credentials && credentials.length > 0 && <button onClick={() => setNewCred(false)}>Cancel</button>}
              </div>
            </div>
          )}
        </div>
      )}

      {step === 1 && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Source repos</h2>
          <p className="help">
            The repos whose commits feed the shared context. One commits source per repo, keyed by repo,
            following one branch each. Up to 50.
          </p>
          {reposErr && (
            <div className="alert warn">
              Could not list repos with this credential ({reposErr}). Paste repo names below instead.
            </div>
          )}
          {repos && (
            <div className="field">
              <span className="lbl">pick from your repos <span className="help">({repos.length} visible to the token)</span></span>
              <input type="text" autoComplete="off" data-1p-ignore data-lpignore="true" className="search" placeholder="filter by name" value={query}
                     onChange={(e) => setQuery(e.target.value)} style={{ marginBottom: 8 }} />
              <div className="uc-repolist">
                {filtered.slice(0, 200).map((r) => (
                  <label key={r.full_name} className="uc-repo">
                    <input type="checkbox" checked={selected(r.full_name)} onChange={() => toggle(r)} />
                    <span className="mono">{r.full_name}</span>
                    <span className="help">{r.private ? "private" : "public"} · {r.default_branch}</span>
                  </label>
                ))}
                {filtered.length === 0 && <div className="dim" style={{ padding: 8 }}>no repos match</div>}
              </div>
            </div>
          )}
          {!repos && !reposErr && credential && <div className="dim">loading repos…</div>}
          <label className="field">
            <span className="lbl">or paste repos <span className="help">(owner/name, one per line)</span></span>
            <textarea data-1p-ignore data-lpignore="true" className="mono" rows={3} value={paste} onChange={(e) => setPaste(e.target.value)}
                      placeholder={"glassflow/tares\nglassflow/tares-cookbooks"} />
          </label>
          <div className="btnrow" style={{ marginBottom: 12 }}>
            <button onClick={addPasted} disabled={!paste.trim()}>Add pasted repos</button>
          </div>
          {sources.length > 0 && (
            <table>
              <thead><tr><th>repo</th><th>branch</th><th aria-label="remove" /></tr></thead>
              <tbody>
                {sources.map((s) => (
                  <tr key={s.repo}>
                    <td className="mono">{s.repo}</td>
                    <td>
                      <input type="text" autoComplete="off" data-1p-ignore data-lpignore="true" className="mono" value={s.branch} placeholder="default branch"
                             onChange={(e) => setBranch(s.repo, e.target.value)} style={{ width: 160 }} />
                    </td>
                    <td><button onClick={() => removeRepo(s.repo)}>Remove</button></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {sources.length > 50 && <div className="alert error">at most 50 repos in one project</div>}
        </div>
      )}

      {step === 2 && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Context repo</h2>
          <p className="help">
            The repo the agent keeps current. The credential needs write access on this repo only.
          </p>
          <div className="row2">
            <label className="field">
              <span className="lbl">repo</span>
              <Combo className="mono" value={contextRepo} options={contextCandidates} placeholder="owner/name"
                     onChange={(v) => setContextRepo(v.trim())} />
              {selected(contextRepo) && <span className="help" style={{ color: "var(--err)" }}>this repo is one of the sources; pick a different one</span>}
            </label>
            <label className="field">
              <span className="lbl">branch</span>
              <input type="text" autoComplete="off" data-1p-ignore data-lpignore="true" className="mono" value={contextBranch} placeholder="main"
                     onChange={(e) => setContextBranch(e.target.value)} />
              <span className="help">where pull requests target</span>
            </label>
          </div>
          <div className="row2">
            <div className="field">
              <span className="lbl">path</span>
              <input type="text" className="mono" value={contextPath} placeholder="/ (root of the repo)" autoComplete="off" data-1p-ignore data-lpignore="true"
                     onChange={(e) => setContextPath(e.target.value)} />
              <span className="help">folder where the pages live; leave empty for the root</span>
              {REPO_RE.test(contextRepo) && (
                <div className="uc-tree">
                  <span className="lbl">what is at <code>{contextPath.trim() || "/"}</code>{treeBusy ? " (looking)" : ""}</span>
                  {treeErr && <span className="help" style={{ color: "var(--err)" }}>{treeErr}</span>}
                  {tree && !tree.exists && <span className="help">nothing there yet; the agent creates the folder on its first write</span>}
                  {tree && tree.exists && tree.markdown.length === 0 && tree.dirs.length === 0 && (
                    <span className="help">no pages here yet</span>
                  )}
                  {tree && tree.exists && (tree.markdown.length > 0 || tree.dirs.length > 0) && (
                    <ul className="uc-tree-list">
                      {tree.dirs.map((d) => (
                        <li key={"d:" + d}>
                          <button type="button" className="linklike mono"
                                  onClick={() => setContextPath((contextPath.trim().replace(/\/+$/, "") ? contextPath.trim().replace(/\/+$/, "") + "/" : "") + d + "/")}>
                            {d}/
                          </button>
                        </li>
                      ))}
                      {tree.markdown.map((f) => <li key={"f:" + f} className="mono">{f}</li>)}
                      {tree.files.length > tree.markdown.length && (
                        <li className="help">and {tree.files.length - tree.markdown.length} other file{tree.files.length - tree.markdown.length === 1 ? "" : "s"}</li>
                      )}
                    </ul>
                  )}
                  {tree && tree.exists && tree.markdown.length > 0 && layout === "per_repo" && (
                    <span className="help">this folder already has pages; "keep the existing pages" fits it better</span>
                  )}
                </div>
              )}
            </div>
            <div className="field">
              <span className="lbl">pages</span>
              <Picker value={layout} onChange={(v) => setLayout(v as "existing")}
                      options={["existing", "per_repo"]}
                      labels={{ existing: "keep the existing pages, update them in place", per_repo: "one page per source repo plus an index" }}
                      ariaLabel="page layout" />
              <span className="help">
                {layout === "existing"
                  ? "the agent reads what is there and edits the page that covers the change"
                  : "the agent keeps <repo-name>.md pages and a README index under the path"}
              </span>
              <span className="lbl" style={{ marginTop: 14 }}>how it writes</span>
              <Picker value={writeMode} onChange={(v) => setWriteMode(v as "pull_request")}
                      options={["pull_request", "commit_to_branch"]}
                      labels={{ pull_request: "open a pull request (recommended)", commit_to_branch: "commit straight to the branch" }}
                      ariaLabel="write mode" />
            </div>
          </div>
        </div>
      )}

      {step === 3 && (
        <div className="panel">
          <h2 style={{ marginTop: 0 }}>Trigger and agent</h2>
          <div className="row2">
            <label className="field">
              <span className="lbl">name</span>
              <input type="text" autoComplete="off" data-1p-ignore data-lpignore="true" value={name} onChange={(e) => setName(e.target.value)} />
              <span className="help">shown on the Projects page; object names derive from it</span>
            </label>
            <div className="field">
              <span className="lbl">runs</span>
              <Picker value={trigger} onChange={setTrigger}
                      options={["every_commit", "every_merged_pr", "daily"]}
                      labels={{ every_commit: "on every commit to the chosen branch",
                                every_merged_pr: "on every merged pull request (coming soon)",
                                daily: "once a day (coming soon)" }}
                      ariaLabel="trigger" />
              {trigger !== "every_commit" && (
                <span className="help" style={{ color: "var(--warn)" }}>not available yet; commits only for now</span>
              )}
            </div>
          </div>
          <div className="row2">
            <div className="field">
              <span className="lbl">model</span>
              <Picker value={model} onChange={setModel}
                      options={["", ...models.filter((m) => m !== defaultModel)]}
                      labels={{ "": defaultModel ? `${defaultModel} · instance default` : "instance default" }}
                      ariaLabel="model" />
            </div>
            <label className="field">
              <span className="lbl">max rounds per run</span>
              <input type="number" min={1} max={24} value={maxRounds} onChange={(e) => setMaxRounds(e.target.value)} />
              <span className="help">reading diffs, a page, writing it and opening a PR takes about 12</span>
            </label>
          </div>

          <h3 style={{ marginBottom: 6 }}>What Start creates</h3>
          <table>
            <thead><tr><th>kind</th><th>name</th><th>what it does</th></tr></thead>
            <tbody>
              {preview.map((p) => (
                <tr key={p.kind + p.name}>
                  <td className="help">{p.kind}</td>
                  <td className="mono">{p.name}</td>
                  <td className="help">{p.note}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <label className="check" style={{ display: "block", marginTop: 8 }}>
            <input type="checkbox" checked={bootstrap} onChange={(e) => setBootstrap(e.target.checked)} />{" "}
            <strong>First look on start:</strong> run the agent once per source repo over the last 7 days of commits, so the context repo starts current. Turn off if it already is, or to save model spend.
          </label>
          <p className="help">Everything created shows on its own page with a "part of project" badge and stays editable there.</p>
        </div>
      )}

      <div className="btnrow" style={{ marginTop: 6 }}>
        {step > 0 && <button onClick={() => { setErr(undefined); setStep(step - 1); }}>Back</button>}
        {step < STEPS.length - 1 && (
          <button className="primary" disabled={!canNext[step]} onClick={() => { setErr(undefined); setStep(step + 1); }}>
            Next
          </button>
        )}
        {step === STEPS.length - 1 && (
          <button className="primary" disabled={busy || !canNext.every(Boolean) || trigger !== "every_commit"} onClick={submit}>
            {busy ? (editId ? "saving…" : "starting…") : editId ? "Save changes" : "Start"}
          </button>
        )}
        <Link className="btn" to={editId ? `/projects/${encodeURIComponent(editId)}` : "/projects"}>Cancel</Link>
      </div>
      {tokenHelp && (
        <InfoDialog title="Token permissions" onClose={() => setTokenHelp(false)}>
          <p className="help" style={{ margin: 0 }}>
            Create a fine-grained personal access token on GitHub (Settings, Developer settings, Personal access tokens, Fine-grained tokens).
            Under Repository access pick the source repos and the context repo. Then add these repository permissions, and check each one's access level after adding it (GitHub starts every permission at Read-only):
          </p>
          <table className="perm-table">
            <thead><tr><th>permission</th><th>access</th><th>why</th></tr></thead>
            <tbody>
              <tr><td>Contents</td><td><strong>Read and write</strong></td><td>read commits and diffs in the source repos; create branches and files in the context repo</td></tr>
              <tr><td>Pull requests</td><td><strong>Read and write</strong></td><td>open the pull request in the context repo. GitHub defaults this to Read-only when you add it; with Read-only the agent can push the branch but the pull request fails</td></tr>
              <tr><td>Metadata</td><td>Read-only</td><td>added by GitHub automatically; lists the repos</td></tr>
            </tbody>
          </table>
          <p className="help" style={{ margin: 0 }}>
            Write access is only used on the context repo. If you prefer, grant Contents read on the source repos with one token and use a second credential with write access for the context repo later; one token is the simple setup.
          </p>
        </InfoDialog>
      )}
    </>
  );
}
