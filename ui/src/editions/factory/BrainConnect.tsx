import * as React from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError } from "../../api";
import { Banner, Button, Collapse, EmptyState, Panel, Skeleton } from "../../components/edition";
import { Checkbox } from "@paper-kit/ui/checkbox";
import { Input as TextInput } from "@paper-kit/ui/input";
import { cn } from "../../lib/utils";
import { useEdition } from "../edition";
import { availableRepos, connectBrainRepos, type AvailableData, type AvailableRepo } from "./BrainConnect.api";
import { availableFolders, type FolderScan } from "./BrainFolders.api";
import { FolderList } from "./BrainFolders";
import { factoryPeople } from "./FactoryPeople.api";
import {
  adminNames, ALL_CONNECTED_WORDS, ASK_ADMIN_WORDS, choosable, connectLabel, connectedNames, GITHUB_SETTINGS, hiddenChosen, hiddenWords,
  matching, MAX_CONNECT, NO_GITHUB_WORDS, NO_REPOS_WORDS, NOT_ADMIN_WORDS, NOT_DONE, NOT_SET_UP_WORDS, NOTICE_WORDS, picked, plainError, SLOW_WORDS,
  SUBTITLE_WORDS, TOO_MANY_WORDS, visibility,
} from "./brainConnectText";

// Connect a repo (TR-1205), an admin page of the Second Brain: the repos the GitHub connection can read,
// to tick and connect (GET /api/brain/repos/available, then POST /api/brain/repos). A key that cannot
// write sees who the admins are and nothing to press, and the list is not even read. Repo names, the
// names of admins and the cell's error words are a person's or the cell's own text, marked data-user-text.
// The notice that everyone on the cell can ask is page chrome: it is under the heading in every state.

const MUTED = "m-0 text-[13px] text-muted-foreground";
const LINK = "text-brand-deep underline";
const GENERIC_READ = "The cell did not answer. Try again in a moment.";
const FOLDERS_READ = "Tares could not look for decision folders. Add one below if your decisions live in the repo.";

function Head({ children }: { children?: React.ReactNode }) {
  return (
    <div className="grid max-w-[860px] gap-5" data-testid="brain-connect">
      <div>
        <Link to="/brain" className="text-[13px] text-muted-foreground" data-testid="brain-connect-back">Back to Second Brain</Link>
        <h1 className="m-0 mb-1 mt-2 text-[26px] font-bold">Connect a repo</h1>
        <p className="m-0 max-w-[640px] text-sm leading-normal text-muted-foreground">{SUBTITLE_WORDS}</p>
      </div>
      <Notice />
      {children}
    </div>
  );
}

function Notice() {
  return (
    <div className="border border-foreground bg-brand-soft px-4 py-3 text-sm leading-normal" data-testid="brain-connect-notice">{NOTICE_WORDS}</div>
  );
}

function Names({ names, mono = false }: { names: string[]; mono?: boolean }) {
  return <>{names.map((n, i) => <React.Fragment key={n}>{i > 0 && ", "}<span className={mono ? "font-mono text-[12.5px]" : "font-medium"} data-user-text>{n}</span></React.Fragment>)}</>;
}

/** One repo to tick. `under` is the room below it while it is ticked: the folders of its decisions (TR-1454). */
function RepoRow({ r, checked, disabled, onToggle, under }: {
  r: AvailableRepo; checked: boolean; disabled: boolean; onToggle: (repo: string, on: boolean) => void; under?: React.ReactNode;
}) {
  const id = React.useId();   // never the repo's name: "a/b-c" and "a-b/c" would share one
  return (
    <li className="flex items-start gap-3 border-b border-border px-4 py-3" data-testid="brain-connect-row" data-repo={r.repo}>
      <Checkbox id={id} checked={checked} disabled={disabled} className="mt-0.5" onCheckedChange={(v) => onToggle(r.repo, v === true)} />
      <div className="min-w-0 flex-1">
        <label htmlFor={id} className="block cursor-pointer font-mono text-[13px] font-medium [overflow-wrap:anywhere]" data-user-text>{r.repo}</label>
        {/* A28: the cell's list has no file counts, so this line says Private or Public, not "N files on main" as the mock does. */}
        <div className="text-[13px] text-muted-foreground" data-testid="brain-connect-kind">{visibility(r)}</div>
        {checked && <div className="empty:hidden" data-testid="brain-connect-under" data-repo={r.repo}>{under}</div>}
      </div>
    </li>
  );
}


function Loading() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading" className="grid gap-3 border border-border bg-card p-4" data-testid="brain-connect-loading">
      <Skeleton width="40%" height={14} /><Skeleton width="65%" height={10} /><Skeleton width="55%" height={10} /><Skeleton width="60%" height={10} />
    </div>
  );
}

function GithubLink({ children }: { children: React.ReactNode }) {
  return <Link to={GITHUB_SETTINGS} className={LINK} data-testid="brain-connect-settings">{children}</Link>;
}

type Load =
  | { state: "loading" }
  | { state: "none" }
  | { state: "error"; words: string }
  | { state: "ok"; data: AvailableData };

function Chooser({ data, reload }: { data: AvailableData; reload: () => Promise<AvailableData | null> }) {
  const navigate = useNavigate();
  const [query, setQuery] = React.useState("");
  const [chosen, setChosen] = React.useState<ReadonlySet<string>>(new Set());
  const [busy, setBusy] = React.useState(false);
  const [failure, setFailure] = React.useState("");
  // the decision folders of each ticked repo: what Tares found (asked once per repo, when first ticked),
  // and the ticked paths, which go with Connect
  const [scans, setScans] = React.useState<Record<string, FolderScan | { error: string }>>({});
  const [folders, setFolders] = React.useState<Record<string, string[]>>({});
  const asked = React.useRef(new Set<string>());
  const scan = (repo: string) => {
    if (asked.current.has(repo)) return;
    asked.current.add(repo);
    availableFolders(repo).then(
      (s) => {
        setScans((m) => ({ ...m, [repo]: s }));
        // what Tares found is ticked to start with, unless the admin already changed the list
        setFolders((m) => (repo in m ? m : { ...m, [repo]: s.suggested.map((x) => x.path) }));
      },
      (e) => setScans((m) => ({ ...m, [repo]: { error: e instanceof ApiError ? plainError(e.message, FOLDERS_READ) : FOLDERS_READ } })),
    );
  };

  const open = choosable(data.repos);
  const shown = matching(open, query);
  const already = connectedNames(data.repos);
  const toSend = picked(data.repos, chosen);
  const tooMany = toSend.length > MAX_CONNECT;
  const hidden = hiddenChosen(shown, toSend);

  const toggle = (repo: string, on: boolean) => {
    setChosen((prev) => { const next = new Set(prev); if (on) next.add(repo); else next.delete(repo); return next; });
    if (on) scan(repo);
  };
  const submit = async () => {
    setBusy(true); setFailure("");
    try {
      await connectBrainRepos(toSend, folders);
      navigate("/brain");
    } catch (e) {
      setFailure(e instanceof ApiError ? plainError(e.message, NOT_DONE) : NOT_DONE);
      setBusy(false);
      // A failure part way may have connected some: read again, and drop those from the choice.
      const fresh = await reload();
      if (fresh) setChosen((prev) => new Set(picked(fresh.repos, prev)));
    }
  };

  const scanOf = (repo: string): FolderScan | null => { const s = scans[repo]; return s && !("error" in s) ? s : null; };
  const errorOf = (repo: string): string | undefined => { const s = scans[repo]; return s && "error" in s ? s.error : undefined; };
  const trimmed = query.trim();
  return (
    <>
      <Panel className="[&>div]:p-0" data-testid="brain-connect-list" aria-label="Repos">
        <div className="border-b border-border px-4 py-2.5">
          <label htmlFor="brain-connect-find" className="sr-only">Find a repo</label>
          <TextInput id="brain-connect-find" placeholder="Find a repo" autoComplete="off" value={query} onChange={(e) => setQuery(e.target.value)}
                     className="bg-field" data-testid="brain-connect-find" />
        </div>
        {open.length === 0 ? (
          <p className={cn(MUTED, "border-b border-border px-4 py-3")} data-testid="brain-connect-none-left">{ALL_CONNECTED_WORDS}</p>
        ) : shown.length === 0 ? (
          <p className={cn(MUTED, "border-b border-border px-4 py-3")} data-testid="brain-connect-no-match">
            No repo matches <span className="font-mono" data-user-text>{trimmed}</span>.
          </p>
        ) : (
          <ul className="m-0 list-none p-0" data-testid="brain-connect-rows">
            {shown.map((r) => (
              <RepoRow key={r.repo} r={r} checked={chosen.has(r.repo)} disabled={busy} onToggle={toggle} under={chosen.has(r.repo) ? (
                <FolderList scan={scanOf(r.repo)} readError={errorOf(r.repo)} selected={folders[r.repo] ?? []} disabled={busy} now={Date.now()}
                            onChange={(next) => setFolders((m) => ({ ...m, [r.repo]: next }))} data-testid="brain-connect-folders" />
              ) : undefined} />
            ))}
          </ul>
        )}
        {already.length > 0 && (
          <p className={cn(MUTED, "px-4 py-3")} data-testid="brain-connect-already">Already connected: <Names names={already} mono /></p>
        )}
      </Panel>
      <div className="flex flex-wrap items-center gap-3">
        <Button busy={busy} disabled={toSend.length === 0 || tooMany} onClick={submit} data-testid="brain-connect-submit">{connectLabel(toSend.length)}</Button>
        <span className={cn(MUTED, "min-w-0 flex-1 basis-64")} data-testid="brain-connect-slow">{SLOW_WORDS}</span>
      </div>
      {hidden > 0 && <p className={MUTED} data-testid="brain-connect-hidden">{hiddenWords(hidden)}</p>}
      {tooMany && <p className={MUTED} data-testid="brain-connect-too-many">{TOO_MANY_WORDS}</p>}
      <Collapse>{failure && <Banner tone="error" data-testid="brain-connect-error"><span data-user-text>{failure}</span></Banner>}</Collapse>
    </>
  );
}

function AdminConnect() {
  const [load, setLoad] = React.useState<Load>({ state: "loading" });
  const alive = React.useRef(true);

  /** Reads the list. A quiet read keeps what is on the screen when it fails. Returns the data, or null. */
  const read = React.useCallback(async (quiet: boolean): Promise<AvailableData | null> => {
    if (!quiet) setLoad({ state: "loading" });
    try {
      const data = await availableRepos();
      if (alive.current) setLoad(data ? { state: "ok", data } : { state: "none" });
      return data;
    } catch (e) {
      if (!quiet && alive.current) setLoad({ state: "error", words: e instanceof ApiError && e.status === 502 ? plainError(e.message, GENERIC_READ) : GENERIC_READ });
      return null;
    }
  }, []);
  React.useEffect(() => { alive.current = true; void read(false); return () => { alive.current = false; }; }, [read]);

  if (load.state === "loading") return <Head><Loading /></Head>;
  if (load.state === "error") {
    return (
      <Head>
        <Banner tone="error" action={<Button variant="secondary" onClick={() => void read(false)} data-testid="brain-connect-retry">Retry</Button>} data-testid="brain-connect-read-error">
          <b>The repo list did not load.</b> <span className="text-foreground" data-user-text>{load.words}</span>
        </Banner>
      </Head>
    );
  }
  if (load.state === "none") {
    return <Head><Panel><EmptyState title="Not set up" data-testid="brain-connect-not-set-up">{NOT_SET_UP_WORDS}</EmptyState></Panel></Head>;
  }
  const { data } = load;
  if (!data.github) {
    return (
      <Head>
        <Panel data-testid="brain-connect-no-github">
          <p className="m-0 text-sm">{NO_GITHUB_WORDS}</p>
          <p className="m-0 mt-2 text-sm"><GithubLink>Set up the GitHub connection</GithubLink></p>
        </Panel>
      </Head>
    );
  }
  if (data.repos.length === 0) {
    return (
      <Head>
        <Panel>
          <EmptyState title="No repos to show" data-testid="brain-connect-empty"
                      action={<GithubLink>Open the GitHub connection</GithubLink>}>{NO_REPOS_WORDS}</EmptyState>
        </Panel>
      </Head>
    );
  }
  return <Head><Chooser data={data} reload={() => read(true)} /></Head>;
}

function OthersConnect() {
  const [names, setNames] = React.useState<string[] | null | undefined>(undefined);   // undefined: reading, null: could not read
  React.useEffect(() => {
    let live = true;
    factoryPeople().then((p) => { if (live) setNames(adminNames(p.people)); }, () => { if (live) setNames(null); });
    return () => { live = false; };
  }, []);
  return (
    <Head>
      <Panel data-testid="brain-connect-admin-only">
        <p className="m-0 text-sm font-semibold">{NOT_ADMIN_WORDS}</p>
        {names === undefined ? null : names && names.length > 0 ? (
          <p className="m-0 mt-2 text-sm" data-testid="brain-connect-admins">Admins on this cell: <Names names={names} />.</p>
        ) : (
          <p className="m-0 mt-2 text-sm" data-testid="brain-connect-admins">{ASK_ADMIN_WORDS}</p>
        )}
      </Panel>
    </Head>
  );
}

export default function BrainConnect() {
  const { admin } = useEdition();
  // The cell has not said yet who this key is: nothing to ask for, nothing to press.
  if (admin === undefined) return <Head><Loading /></Head>;
  return admin ? <AdminConnect /> : <OthersConnect />;
}
