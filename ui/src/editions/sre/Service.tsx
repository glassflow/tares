import * as React from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { ApiError } from "../../api";
import {
  Banner, Button, Checkbox, Dialog, EmptyState, Input, LineChart, ListRow, ListSection, Loading, Panel, Select, Skeleton,
  StatusDot, StatusLabel, Switch, Tabs,
} from "../../components/edition";
import { useSample } from "../sample";
import { usePagePolling } from "../usePagePolling";
import {
  saveServiceSettings, sreService, type SreService, type SreServiceSettingsBody, type SreWindow,
} from "./Services.api";
import { isServiceSample, sampleSave, sampleService } from "./sample/services";
import { serviceHref, STATE_TONE, up } from "./serviceWords";

// One service: what state it is in, a chart of its errors and traffic with its deploys, its
// incidents, what the watcher raised, its blind spots, the repos its deploys come from, and its
// settings. One read per window (GET /api/sre/services/{name}?window=); a save answers the page.
// A service that has gone silent opens on the last week, so the stop shows. Names, display names,
// repos and incident titles are the person's own text.

const POLL_MS = 30_000;
const LONG_NAME = 40;

const WINDOWS: { value: SreWindow; label: string }[] = [
  { value: "1h", label: "Last hour" },
  { value: "24h", label: "Last day" },
  { value: "7d", label: "Last week" },
];

type Loaded = { page: SreService } | { missing: true };

function ServiceSkeleton() {
  return (
    <Loading className="grid gap-6" data-testid="service-loading">
      <div className="grid gap-3"><Skeleton width="30%" /><Skeleton width="55%" /></div>
      <div className="border border-border bg-card p-4"><Skeleton width="100%" height={160} /></div>
      <div className="border border-border bg-card p-4 grid gap-3"><Skeleton width="40%" /><Skeleton width="70%" /><Skeleton width="55%" /></div>
    </Loading>
  );
}

function BackLink() {
  return (
    <Link to="/services" className="text-[13px] text-muted-foreground no-underline! hover:text-foreground" data-testid="back-to-services">
      Services
    </Link>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="grid min-w-0 content-start gap-1 [overflow-wrap:anywhere]">
      <dt className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">{label}</dt>
      <dd className="m-0 text-sm">{children}</dd>
    </div>
  );
}

function Header({ page }: { page: SreService }) {
  const shownName = page.about || page.name;
  const showReal = page.about !== null || page.name.length > LONG_NAME;
  return (
    <header className="grid gap-3" data-testid="service-header">
      <BackLink />
      <div className="min-w-0">
        <h1 className="m-0! mb-1.5! text-[22px]! [overflow-wrap:anywhere] md:text-[28px]!" data-testid="service-title"
          {...(page.about ? { "data-user-text": "" } : {})}>
          {shownName}
        </h1>
        {showReal && (
          <p className="m-0 mb-2 font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]" data-testid="service-real-name" data-user-text>
            {page.name}
          </p>
        )}
        <p className="m-0 flex flex-wrap items-center gap-x-2 gap-y-1 text-[15px]">
          <StatusDot tone={STATE_TONE[page.state]} />
          <StatusLabel tone={STATE_TONE[page.state]} data-testid="service-state"
            className="font-sans text-[15px] font-medium normal-case tracking-normal">
            {page.state_words}
          </StatusLabel>
          {page.hidden && <span className="text-[13px] text-muted-foreground" data-testid="service-hidden">Hidden</span>}
        </p>
      </div>
      <dl className="m-0 grid grid-cols-1 gap-x-8 gap-y-4 border-y border-border py-4 sm:grid-cols-2 lg:grid-cols-4" data-testid="service-facts">
        <Fact label="Events">{page.events_24h_words}</Fact>
        <Fact label="Watching">{page.watch_words}</Fact>
        <Fact label="Last check">{up(page.last_words)}</Fact>
        <Fact label="Sends">{page.signals_words}, <span data-user-text>{page.feeds_words}</span></Fact>
      </dl>
    </header>
  );
}

function ServiceChart({ page, stale }: { page: SreService; stale: boolean }) {
  const c = page.chart;
  const t0 = Date.parse(c.since);
  const step = c.bucket_seconds * 1000;
  // A page without a start time, or a bad one, cannot place its points: say so instead of throwing,
  // since one bad timestamp must not blank the console.
  if (!Number.isFinite(t0 + step)) {
    return (
      <p className="m-0 text-sm text-muted-foreground" data-testid="service-chart-unplaced">
        The chart has no times to draw yet.
      </p>
    );
  }
  const at = (i: number) => new Date(t0 + i * step).toISOString();
  const errors = c.errors.map((v, i) => [at(i), v] as [string, number]);
  const traffic = c.traffic.map((v, i) => [at(i), v] as [string, number]);
  return (
    <div className={stale ? "opacity-60 transition-opacity" : "transition-opacity"} data-testid="service-chart" data-window={c.window}
      data-deploys={c.deploys.length}>
      <LineChart
        series={[{ label: "Errors", points: errors }, { label: "Traffic", points: traffic }]}
        marks={c.deploys.map((d) => ({ at: d.at, label: d.label }))}
        marksList
        height={180}
        aria-label={`Errors and traffic of ${page.about || page.name}, ${WINDOWS.find((w) => w.value === c.window)?.label.toLowerCase()}`}
      />
    </div>
  );
}

function Incidents({ page }: { page: SreService }) {
  const closed = Math.max(0, page.incidents_total - page.incidents_open);
  const aside = page.incidents_total ? `${page.incidents_open} open, ${closed} closed` : undefined;
  return (
    <ListSection title="Incidents" aside={aside} data-testid="service-incidents">
      {page.incidents.length === 0 ? (
        <EmptyState title="No incidents">Nothing has gone wrong with this service.</EmptyState>
      ) : (
        <ul className="m-0! list-none! p-0!">
          {page.incidents.map((i) => (
            <li key={i.id}>
              <ListRow to={`/incidents/${encodeURIComponent(i.id)}`} data-testid="incident-row" data-state={i.state}>
                <span className="min-w-0 flex-1 basis-60 font-medium" data-user-text>{i.title}</span>
                <span className="text-[13px] text-muted-foreground">{i.state_words}, {i.opened_words.toLowerCase()}</span>
              </ListRow>
            </li>
          ))}
        </ul>
      )}
      {page.incidents_total > page.incidents.length && (
        <p className="m-0 border-t border-border px-[18px] py-3 text-xs text-muted-foreground" data-testid="incidents-showing">
          Showing {page.incidents.length} of {page.incidents_total}
        </p>
      )}
    </ListSection>
  );
}

function Raised({ page }: { page: SreService }) {
  return (
    <ListSection title="Raised about it" data-testid="service-raised">
      {page.raised.length === 0 ? (
        <EmptyState title="Nothing raised">The watcher has not flagged anything about this service.</EmptyState>
      ) : (
        <ul className="m-0! list-none! p-0!">
          {page.raised.map((r) => (
            <li key={r.id}>
              <ListRow data-testid="raised-row" className="items-start">
                <span className="w-32 shrink-0 text-xs text-muted-foreground">{up(r.when_words)}</span>
                <span className="grid min-w-0 flex-1 basis-60 gap-0.5">
                  <span data-user-text>{r.what}</span>
                  <span className="text-[13px] text-muted-foreground">{r.outcome_words}</span>
                </span>
                <span className="text-[13px] text-muted-foreground" data-testid="raised-worth">{r.worth}</span>
              </ListRow>
            </li>
          ))}
        </ul>
      )}
      {page.raised_total > page.raised.length && (
        <p className="m-0 border-t border-border px-[18px] py-3 text-xs text-muted-foreground" data-testid="raised-showing">
          Showing {page.raised.length} of {page.raised_total}
        </p>
      )}
    </ListSection>
  );
}

function BlindSpots({ page }: { page: SreService }) {
  const spots = page.blind_spots;
  return (
    <ListSection title="Blind spots" aside={spots.length ? page.blind_spots_words : undefined} data-testid="service-blind-spots">
      {spots.length === 0 ? (
        <p className="m-0 px-[18px] py-4 text-sm text-muted-foreground" data-testid="blind-spots-words">{page.blind_spots_words}</p>
      ) : (
        <ul className="m-0! list-none! p-0!">
          {spots.map((b, i) => (
            <li key={`${b.kind}-${i}`}>
              <ListRow className="items-start" data-testid="blind-spot-row">
                <span className="grid min-w-0 flex-1 basis-60 gap-0.5">
                  <span className="font-medium">{b.title}</span>
                  <span className="text-[13px] text-muted-foreground" data-user-text>{b.detail}</span>
                </span>
                <Link to={b.action.to} className="text-[13px]" data-testid="blind-spot-action">{b.action.label}</Link>
              </ListRow>
            </li>
          ))}
        </ul>
      )}
    </ListSection>
  );
}

function DeploysFrom({ page }: { page: SreService }) {
  return (
    <ListSection title="Deploys from" data-testid="service-repos">
      {page.repos.length === 0 ? (
        <p className="m-0 px-[18px] py-4 text-sm text-muted-foreground">No repo is linked, so the chart shows no deploys. Link one under Settings below.</p>
      ) : (
        <ul className="m-0! list-none! p-0!">
          {page.repos.map((r) => (
            <li key={r.name}>
              <ListRow data-testid="repo-row">
                <span className="min-w-0 flex-1 basis-48 font-medium" data-user-text>{r.name}</span>
                <span className="text-[13px] text-muted-foreground">{r.linked_by}, {r.linked_words.toLowerCase()}</span>
              </ListRow>
            </li>
          ))}
        </ul>
      )}
    </ListSection>
  );
}

type SaveFn = (body: SreServiceSettingsBody) => Promise<SreService>;

function SettingsPanel({ page, save, readOnly, onReadOnly }: {
  page: SreService; save: SaveFn; readOnly: boolean; onReadOnly: () => void;
}) {
  const s = page.settings;
  const linked = React.useMemo(() => page.repos.map((r) => r.name), [page.repos]);
  const [name, setName] = React.useState(s.display_name);
  const [hidden, setHidden] = React.useState(s.hidden);
  const [repos, setRepos] = React.useState<string[]>(linked);
  const [mergeInto, setMergeInto] = React.useState("");
  const [confirm, setConfirm] = React.useState(false);
  const [busy, setBusy] = React.useState(false);
  const [failed, setFailed] = React.useState<SreServiceSettingsBody | null>(null);
  const [saved, setSaved] = React.useState(false);

  const sameRepos = repos.length === linked.length && repos.every((r) => linked.includes(r));
  const dirty = name !== s.display_name || hidden !== s.hidden || !sameRepos;
  const options = [...new Set([...s.repo_options, ...linked])].sort();

  const run = async (body: SreServiceSettingsBody) => {
    setBusy(true);
    setFailed(null);
    setSaved(false);
    try {
      const next = await save(body);
      if (!("merged_into" in body)) {
        setName(next.settings.display_name);
        setHidden(next.settings.hidden);
        setRepos(next.repos.map((r) => r.name));
        setSaved(true);
      }
    } catch (e) {
      if (e instanceof ApiError && e.status === 403) onReadOnly();
      else setFailed(body);
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  };

  const changes = (): SreServiceSettingsBody => ({
    ...(name !== s.display_name ? { display_name: name.trim() || null } : {}),
    ...(hidden !== s.hidden ? { hidden } : {}),
    ...(!sameRepos ? { repos } : {}),
  });

  const off = readOnly || busy;
  return (
    <Panel title="Settings" data-testid="service-settings">
      <div className="grid max-w-xl gap-6">
        {readOnly && (
          <Banner tone="note" data-testid="settings-readonly">
            This key can read but not change settings. Use a key that can write to change them.
          </Banner>
        )}
        {failed && (
          <Banner tone="error" data-testid="settings-failed"
            action={<Button variant="secondary" onClick={() => run(failed)} disabled={busy}>Retry</Button>}>
            <b>Settings were not saved.</b> <span className="text-foreground">The cell did not accept the change. Try again.</span>
          </Banner>
        )}
        <Input
          label="Name shown"
          hint="Leave it empty to show the service's own name."
          value={name}
          maxLength={80}
          disabled={off}
          onChange={(e) => { setName(e.target.value); setSaved(false); }}
          data-testid="setting-name"
        />
        <Switch
          label="Hide this service"
          hint="A hidden service leaves the list and is not checked. You can show it again from the list."
          checked={hidden}
          disabled={off}
          onCheckedChange={(v) => { setHidden(v); setSaved(false); }}
          data-testid="setting-hidden"
        />
        <fieldset className="m-0 grid min-w-0 gap-3 border-0 p-0" disabled={off} data-testid="setting-repos">
          <legend className="mb-1 p-0 text-sm font-medium">Link repos</legend>
          <p className="m-0 text-[13px] text-muted-foreground">Pushes and merges in a linked repo show as deploys on the chart.</p>
          {options.length === 0 ? (
            <p className="m-0 text-sm text-muted-foreground">No repos to link yet. Connect GitHub under Settings, Tools.</p>
          ) : options.map((r) => (
            <div key={r} data-user-text>
              <Checkbox
                label={r}
                checked={repos.includes(r)}
                disabled={off}
                onCheckedChange={(v) => { setRepos((cur) => (v === true ? [...cur, r] : cur.filter((x) => x !== r))); setSaved(false); }}
                data-testid="setting-repo"
                data-repo={r}
              />
            </div>
          ))}
        </fieldset>
        <div className="flex flex-wrap items-center gap-3">
          <Button onClick={() => run(changes())} disabled={!dirty || readOnly} busy={busy} data-testid="settings-save">Save settings</Button>
          {saved && <span role="status" className="text-[13px] text-muted-foreground" data-testid="settings-saved">Saved.</span>}
        </div>

        <div className="grid gap-3 border-t border-border pt-6" data-testid="setting-merge">
          <Select
            label="Count under another service"
            hint="Use this when two names are one service. Events of this one are counted under the one you pick."
            options={s.merge_options.map((o) => ({ value: o, label: o }))}
            value={mergeInto}
            onValueChange={setMergeInto}
            placeholder={s.merge_options.length ? "Choose a service" : "No other service to join"}
            disabled={off || s.merge_options.length === 0}
            data-testid="setting-merge-select"
          />
          <div>
            <Button variant="secondary" disabled={off || !mergeInto} onClick={() => setConfirm(true)} data-testid="merge-open">Join…</Button>
          </div>
          {s.merged_names.length > 0 && (
            <p className="m-0 text-[13px] text-muted-foreground" data-testid="merged-names">
              Counted here too: {s.merged_names.map((n, i) => <React.Fragment key={n}>{i ? ", " : ""}<span data-user-text>{n}</span></React.Fragment>)}
            </p>
          )}
        </div>
      </div>
      <Dialog
        open={confirm}
        onOpenChange={setConfirm}
        title={`Join into ${mergeInto}?`}
        description="This service is counted under the other one from now on. The console has no undo for this."
        data-testid="merge-dialog"
        footer={(
          <>
            <Button variant="secondary" onClick={() => setConfirm(false)} disabled={busy}>Cancel</Button>
            <Button onClick={() => run({ merged_into: mergeInto })} busy={busy} data-testid="merge-confirm">Join</Button>
          </>
        )}
      />
    </Panel>
  );
}

/** Pure: draws the page it is given. `windowValue` is the tab picked; `stale` while the read for it is on its way. */
export function ServiceView({ page, windowValue, onWindow, stale = false, save, readOnly = false, onReadOnly = () => {} }: {
  page: SreService;
  windowValue: SreWindow;
  onWindow: (w: SreWindow) => void;
  stale?: boolean;
  save: SaveFn;
  readOnly?: boolean;
  onReadOnly?: () => void;
}) {
  return (
    <div className="grid gap-8" data-testid="sre-service" data-state={page.state}>
      <Header page={page} />
      <section aria-label="Errors and traffic" className="grid min-w-0 gap-3">
        <Tabs
          aria-label="Time range"
          value={windowValue}
          onValueChange={(v) => onWindow(v as SreWindow)}
          items={WINDOWS.map((w) => ({
            value: w.value,
            label: w.label,
            content: w.value === windowValue ? <div className="pt-3"><ServiceChart page={page} stale={stale} /></div> : null,
          }))}
        />
        <p className="m-0 text-[13px]">
          <Link to={`/timeline?service=${encodeURIComponent(page.name)}`} data-testid="open-timeline">Open the timeline</Link>
        </p>
      </section>
      <div className="grid items-start gap-6 lg:grid-cols-2">
        <Incidents page={page} />
        <Raised page={page} />
        <BlindSpots page={page} />
        <DeploysFrom page={page} />
      </div>
      <SettingsPanel page={page} save={save} readOnly={readOnly} onReadOnly={onReadOnly} />
    </div>
  );
}

function ServiceLoader({ name }: { name: string }) {
  const named = useSample();
  const sample = isServiceSample(named) ? named : null;
  const navigate = useNavigate();
  const [win, setWin] = React.useState<SreWindow | null>(null);
  const askRef = React.useRef<SreWindow>("24h");
  const decided = React.useRef(false);
  const [answered, setAnswered] = React.useState<SreService | null>(null);
  const [readOnly, setReadOnly] = React.useState(false);

  const { data, error, reload } = usePagePolling<Loaded>(
    () => (sample ? sampleService(sample, askRef.current) : sreService(name, askRef.current))
      .then((page): Loaded => ({ page }))
      .catch((e): Loaded => {
        if (e instanceof ApiError && e.status === 404) return { missing: true };
        throw e;
      }),
    POLL_MS);
  React.useEffect(() => { setAnswered(null); }, [data]);

  const choose = (w: SreWindow) => { askRef.current = w; setWin(w); reload(); };
  const page = data && "page" in data ? data.page : undefined;

  // A service that went silent opens on the last week, decided once from the first answer.
  React.useEffect(() => {
    if (!page || decided.current) return;
    decided.current = true;
    if (page.state === "silent") choose("7d");
  }, [page]);

  // A name merged into another answers with the other: show that address.
  React.useEffect(() => {
    if (page && !sample && page.name !== name) navigate(serviceHref(page.name), { replace: true });
  }, [page, name, sample, navigate]);

  const save: SaveFn = async (body) => {
    const next = sample ? await sampleSave(sample, page as SreService, body as Record<string, unknown>) : await saveServiceSettings(name, body);
    if (next.name !== name && !sample) { navigate(serviceHref(next.name), { replace: true }); return next; }
    askRef.current = "24h";
    setWin("24h");
    setAnswered(next);
    return next;
  };

  if (error && !data) {
    return (
      <div className="grid gap-4" data-testid="sre-service">
        <BackLink />
        <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>} data-testid="service-error">
          <b>This service did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
        </Banner>
      </div>
    );
  }
  if (data && "missing" in data) {
    return (
      <div className="grid gap-4" data-testid="sre-service">
        <BackLink />
        <div className="border border-border bg-card" data-testid="service-missing">
          <EmptyState
            title="No service with this name"
            action={<Button asChild variant="secondary"><Link to="/services">Back to Services</Link></Button>}
          >
            The cell has no service called <b data-user-text>{name}</b>. It may have been renamed or joined into another.
          </EmptyState>
        </div>
      </div>
    );
  }
  const shown = answered ?? page;
  const waiting = !shown || (!decided.current && shown.state === "silent");
  if (waiting || !shown) return <ServiceSkeleton />;
  const w = win ?? "24h";
  return (
    <>
      {error && (
        <div className="mb-6">
          <Banner tone="warn" action={<Button variant="secondary" onClick={reload}>Retry</Button>} data-testid="service-stale">
            The cell did not answer the last time. This is the last page it gave.
          </Banner>
        </div>
      )}
      <ServiceView
        page={shown}
        windowValue={w}
        onWindow={choose}
        stale={shown.chart.window !== w}
        save={save}
        readOnly={readOnly}
        onReadOnly={() => setReadOnly(true)}
      />
    </>
  );
}

export default function Service() {
  const { name = "" } = useParams();
  return <ServiceLoader key={name} name={name} />;
}
