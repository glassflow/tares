import * as React from "react";
import { Link, Navigate, useLocation } from "react-router-dom";

import { ApiError } from "../../api";
import { Banner, Button, Dialog, ListRow, Skeleton, StatusDot, Textarea } from "../../components/edition";
import { Switch } from "../../components/ui/switch";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { useSample } from "../sample";
import {
  addGrant, changeCrew, factorySettings, switchEdition,
  type Autonomy, type CrewChange, type SettingsGrant, type SettingsPage, type SettingsTool,
} from "./FactorySettings.api";
import { isSample, sampleLoad } from "./settingsSamples";

// Settings: the tools the crew works with, how far it may go and what it was allowed, the model and
// budget, the keys, the edition and the way to everything else. One read per load; a change goes
// through the route that already exists and the page reads again. A read key sees it all, dimmed.

const POLL_MS = 30_000;
const SHOWN_GRANTS = 5;
const MAX_GRANT = 400;
const SAVE_FAILED = "That change was not saved. Try again in a moment.";

const ROW_TEXT = "min-w-0 flex-[1_1_320px]";
const HEAD = "flex items-center justify-between gap-3 border-b border-border bg-muted px-4 py-3";
const CHOICE = "flex! w-full items-start gap-3 border-0! border-b! border-border! bg-transparent! px-4! py-3.5! text-left font-normal! text-foreground! cursor-pointer disabled:cursor-not-allowed hover:bg-[var(--row-hover)]! last:border-b-0!";
const LINK_BTN = "border-0! bg-transparent! p-0! text-[13px]! font-normal! text-[color:var(--accent-deep)]! underline! cursor-pointer";

function Card({ dim, className, ...props }: { dim?: boolean } & React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("border border-border bg-card", dim && "opacity-50", className)} {...props} />;
}

function Section({ id, title, why, children }: { id: string; title: string; why: string; children: React.ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-h`} className="mb-7 scroll-mt-4" data-testid={`section-${id}`}>
      <h2 id={`${id}-h`} className="m-0! text-base!">{title}</h2>
      <p className="m-0! mb-2.5! text-[13px] text-muted-foreground">{why}</p>
      {children}
    </section>
  );
}

function Row({ title, text, user, children, ...props }: { title: string; text: string; user?: boolean; children?: React.ReactNode } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <ListRow {...props}>
      <div className={ROW_TEXT}>
        <div className="font-semibold">{title}</div>
        <div className="mt-0.5 text-[13px] text-muted-foreground" {...(user ? { "data-user-text": true } : {})}>{text}</div>
      </div>
      {children && <div className="flex flex-none items-center gap-2">{children}</div>}
    </ListRow>
  );
}

/** A switch with its own words; `label` names it for people who cannot see the row. */
function Toggle({ label, on, off, disabled, onChange, testid }: { label: string; on: boolean; off?: boolean; disabled: boolean; onChange: (v: boolean) => void; testid: string }) {
  return <Switch aria-label={label} checked={on} disabled={disabled || off} onCheckedChange={onChange} data-testid={testid} />;
}

function Tools({ tools, dim }: { tools: SettingsTool[]; dim: boolean }) {
  return (
    <Card dim={dim}>
      {tools.map((t) => (
        <ListRow key={t.key} data-testid={`tool-${t.key}`}>
          <div className={ROW_TEXT}>
            <div className="font-semibold">
              <StatusDot tone={t.connected ? "ready" : "quiet"} className="mr-2 align-baseline" data-testid="tool-dot" data-on={t.connected} />
              {t.name}
            </div>
            <div className="mt-0.5 text-[13px] text-muted-foreground" data-user-text>{t.detail}</div>
          </div>
          <div className="flex flex-none items-center gap-2">
            <Button asChild variant={t.connected ? "secondary" : "primary"} disabled={dim} data-ctl>
              <Link to={t.href}>{t.key === "claude_code" ? (t.connected ? "See how" : "Connect") : t.connected ? "Change" : "Connect"}</Link>
            </Button>
          </div>
        </ListRow>
      ))}
    </Card>
  );
}

const CHOICES: { value: Autonomy; title: string; text: string }[] = [
  { value: "L3-review", title: "Up to merge: you approve each merge", text: "The crew builds and reviews. A pull request waits for you before it merges." },
  { value: "L4-ship", title: "Merges on its own: after the crew's review and green CI", text: "The crew merges a pull request once the reviewer passed it and CI is green." },
];

function GrantRow({ g }: { g: SettingsGrant }) {
  return (
    <div className="border-b border-border px-4 py-3 last:border-b-0 [overflow-wrap:anywhere]" data-testid="grant">
      <div><span data-user-text data-testid="grant-text">{`“${g.text}”`}</span></div>
      {g.question && (
        <div className="mt-1 text-[13px]" data-testid="grant-question">Answer to: <span data-user-text>{g.question}</span></div>
      )}
      <div className="mt-1 text-xs text-muted-foreground" data-user-text data-testid="grant-meta">{[g.scope_text, g.state_text].filter(Boolean).join(", ")}</div>
    </div>
  );
}

function GrantDialog({ open, onOpenChange, onAdd }: { open: boolean; onOpenChange: (o: boolean) => void; onAdd: (words: string) => Promise<string | null> }) {
  const [words, setWords] = React.useState("");
  const [error, setError] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const w = words.trim();
    if (!w) { setError("Write what the crew may do first."); return; }
    setBusy(true);
    const failed = await onAdd(w);
    setBusy(false);
    if (failed) setError(failed);
    else { setWords(""); setError(""); onOpenChange(false); }
  };
  return (
    <Dialog open={open} onOpenChange={(o) => { onOpenChange(o); if (!o) setError(""); }} data-testid="grant-dialog"
            title="Add a grant" description="Say what the crew may do without asking again. It applies to every project."
            footer={<>
              <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
              <Button type="submit" form="grant-form" busy={busy} data-testid="grant-save">Add grant</Button>
            </>}>
      <form id="grant-form" onSubmit={submit}>
        <Textarea label="What the crew may do" value={words} maxLength={MAX_GRANT} rows={4} error={error || undefined}
                  hint={`Up to ${MAX_GRANT} characters.`} onChange={(e) => { setWords(e.target.value); if (error) setError(""); }} data-testid="grant-input" />
      </form>
    </Dialog>
  );
}

export interface ViewProps {
  page: SettingsPage;
  /** What a change that was not saved says; empty when there is none. */
  problem?: string;
  onCrew: (c: CrewChange) => Promise<void>;
  onGrant: (words: string) => Promise<string | null>;
  onSwitch: (edition: string) => Promise<void>;
}

export function FactorySettingsView({ page, problem, onCrew, onGrant, onSwitch }: ViewProps) {
  const { crew, models, people, edition, tools } = page;
  const dim = !page.can_change;
  const [confirm, setConfirm] = React.useState(false);
  const [target, setTarget] = React.useState<{ value: string; label: string } | null>(null);
  const [grantOpen, setGrantOpen] = React.useState(false);
  const [all, setAll] = React.useState(false);
  const [switching, setSwitching] = React.useState(false);
  const shown = all ? crew.grants : crew.grants.slice(0, SHOWN_GRANTS);
  const hidden = crew.grants.length - SHOWN_GRANTS;
  const level = crew.how_far.value;

  return (
    <div className="max-w-[860px]">
      {dim && <Banner tone="warn" className="mb-5" data-testid="read-only">You can look but not change anything here. Ask an admin.</Banner>}
      {problem && <Banner tone="error" className="mb-5" data-testid="problem">{problem}</Banner>}

      <Section id="tools" title="Tools" why="The services the crew works with.">
        <Tools tools={tools} dim={dim} />
      </Section>

      <Section id="crew" title="Crew" why="How far the crew may go, how many builders work at once and what the crew may do on its own.">
        <Card dim={dim} className="mb-3" data-testid="how-far">
          <div className={HEAD}><b className="text-[13px]">How far it may go</b></div>
          <div role="radiogroup" aria-label="How far it may go">
            {CHOICES.map((c) => (
              <button key={c.value} type="button" role="radio" aria-checked={level === c.value} disabled={dim} data-testid={`how-${c.value}`} data-ctl
                      onClick={() => { if (c.value === level) return; if (c.value === "L4-ship") setConfirm(true); else void onCrew({ autonomy: c.value }); }}
                      className={CHOICE}>
                <span aria-hidden className={cn("mt-[3px] grid size-4 flex-none place-items-center rounded-full border-[1.5px] border-foreground bg-[var(--white)]", level === c.value && "after:size-2 after:rounded-full after:bg-foreground")} />
                <span className="grid gap-0.5">
                  <span className="font-semibold">{c.title}</span>
                  <span className="text-[13px] text-muted-foreground">{c.text}</span>
                </span>
              </button>
            ))}
          </div>
          {crew.release_profile && (
            <Row title="Releases" data-testid="row-releases"
                 text={crew.how_far.releases ? "The crew may ship a finished milestone when you say so." : "The crew does not ship. Merged work waits."}>
              <Toggle label="Releases" on={crew.how_far.releases} disabled={dim} testid="toggle-releases" onChange={(v) => void onCrew({ releaser: v })} />
            </Row>
          )}
        </Card>

        <Card dim={dim} className="mb-3">
          <Row title="Builders at once" text={crew.builders_text} data-testid="row-builders">
            <div className="flex items-center border border-foreground bg-[var(--white)]">
              <button type="button" aria-label="One builder fewer" disabled={dim || crew.builders_max <= 1} data-ctl data-testid="builders-less"
                      onClick={() => void onCrew({ builders_max: crew.builders_max - 1 })}
                      className="size-8 border-0! bg-transparent! p-0! text-lg! text-foreground! cursor-pointer disabled:cursor-not-allowed">{"−"}</button>
              <b className="min-w-9 text-center" data-testid="builders-count" aria-live="polite">{crew.builders_max}</b>
              <button type="button" aria-label="One builder more" disabled={dim || crew.builders_max >= 20} data-ctl data-testid="builders-more"
                      onClick={() => void onCrew({ builders_max: crew.builders_max + 1 })}
                      className="size-8 border-0! bg-transparent! p-0! text-lg! text-foreground! cursor-pointer disabled:cursor-not-allowed">+</button>
            </div>
          </Row>
          <Row title="Second check" data-testid="row-challenger"
               text={crew.challenger ? "A second model checks every commit before the review. Findings land on the ticket." : "Off. Only the reviewer checks a pull request."}>
            <Toggle label="Second check" on={crew.challenger} disabled={dim} testid="toggle-challenger" onChange={(v) => void onCrew({ challenger: v })} />
          </Row>
          <Row title="Pacing" text={crew.pace_text} data-testid="row-pace">
            <Toggle label="Pacing" on={crew.pace} disabled={dim} testid="toggle-pace" onChange={(v) => void onCrew({ pace: v })} />
          </Row>
        </Card>

        <Card dim={dim} data-testid="grants">
          <div className={HEAD}>
            <b className="text-[13px]">What you allowed</b>
            <Button variant="primary" disabled={dim} data-ctl data-testid="grant-add" onClick={() => setGrantOpen(true)}>Add a grant</Button>
          </div>
          {crew.grants.length === 0 ? (
            <div className="p-4 text-muted-foreground" data-testid="grants-empty">Nothing allowed yet. The crew asks when it needs something.</div>
          ) : (
            <>
              {shown.map((g) => <GrantRow key={g.id} g={g} />)}
              {!all && hidden > 0 && (
                <div className="px-4 py-2.5">
                  <button type="button" className={LINK_BTN} disabled={dim} data-ctl data-testid="grants-more" onClick={() => setAll(true)}>Show {hidden} more</button>
                </div>
              )}
              {all && crew.grants_total > crew.grants.length && (
                <div className="px-4 py-2.5 text-xs text-muted-foreground">Showing the newest {crew.grants.length} of {crew.grants_total}.</div>
              )}
            </>
          )}
        </Card>
      </Section>

      <Section id="models" title="Models and budget" why="Which model the crew and the agents use and how much they may spend.">
        <Card dim={dim}>
          <Row title="Model provider" text={models.default_provider} user data-testid="row-provider">
            <Button asChild variant="secondary" disabled={dim} data-ctl><Link to="/settings/advanced?tab=anthropic">Change</Link></Button>
          </Row>
          <Row title="Budget" text={models.budget_text} data-testid="row-budget">
            <Button asChild variant="secondary" disabled={dim} data-ctl><Link to="/settings/advanced?tab=agents">Change</Link></Button>
          </Row>
        </Card>
      </Section>

      <Section id="people" title="People and keys" why="Who can reach this cell and the keys that let a tool in.">
        <Card dim={dim}>
          <Row title="Access keys" text={people.keys.text} data-testid="row-keys">
            <Button asChild variant="secondary" disabled={dim} data-ctl><Link to="/settings/advanced?tab=access">Manage keys</Link></Button>
          </Row>
          {people.cloud_team_href && (
            <Row title="Team" text="Team members are managed in your workspace on Tares Cloud." data-testid="row-team">
              <Button asChild variant="secondary" disabled={dim} data-ctl>
                <a href={people.cloud_team_href} target="_blank" rel="noreferrer noopener">Open the workspace</a>
              </Button>
            </Row>
          )}
        </Card>
      </Section>

      <Section id="edition" title="Edition" why="Which set of screens this cell shows.">
        <Card>
          <ListRow data-testid="row-edition">
            <div className={ROW_TEXT}>
              <div className="font-semibold" data-testid="edition-name">{edition.label}</div>
              <div className="mt-0.5 text-[13px] text-muted-foreground">{edition.note}</div>
              {!edition.can_switch && <div className="mt-0.5 text-[13px] text-muted-foreground" data-testid="edition-readonly">Only an admin can switch the edition.</div>}
            </div>
            {edition.can_switch && (
              <div className="flex flex-none flex-wrap items-center gap-2">
                {edition.options.map((o) => (
                  <Button key={o.value} variant="primary" data-testid={`edition-${o.value}`} onClick={() => setTarget(o)}>{`Switch to ${o.label}`}</Button>
                ))}
              </div>
            )}
          </ListRow>
        </Card>
      </Section>

      <Section id="advanced" title="Advanced" why="Every other setting and the building blocks behind the screens.">
        <Card>
          {page.advanced.map((a) => (
            <ListRow key={a.href} to={a.href} className="block! min-h-0! py-3!" data-testid="advanced-link">
              <b className="font-semibold text-[color:var(--accent-deep)] underline">{a.label}</b>
              <span className="mt-0.5 block text-[13px] text-muted-foreground">{a.text}</span>
            </ListRow>
          ))}
        </Card>
      </Section>

      <Dialog open={confirm} onOpenChange={setConfirm} data-testid="confirm-ship"
              title="Let the crew merge on its own?"
              description="It will merge a pull request once the reviewer passed it and CI is green, without waiting for you."
              footer={<>
                <Button variant="secondary" onClick={() => setConfirm(false)}>Keep approving</Button>
                <Button data-testid="confirm-ship-yes" onClick={() => { setConfirm(false); void onCrew({ autonomy: "L4-ship" }); }}>Let it merge</Button>
              </>} />
      <Dialog open={target !== null} onOpenChange={(o) => { if (!o) setTarget(null); }} data-testid="confirm-edition"
              title={`Switch to ${target?.label ?? ""}?`} description={edition.note}
              footer={<>
                <Button variant="secondary" onClick={() => setTarget(null)}>Cancel</Button>
                <Button busy={switching} data-testid="confirm-edition-yes"
                        onClick={async () => { if (!target) return; setSwitching(true); await onSwitch(target.value); setSwitching(false); setTarget(null); }}>
                  {`Switch to ${target?.label ?? ""}`}
                </Button>
              </>} />
      <GrantDialog open={grantOpen} onOpenChange={setGrantOpen} onAdd={onGrant} />
    </div>
  );
}

function Loading() {
  return (
    <Card role="status" aria-busy="true" aria-label="Loading" className="grid max-w-[860px] gap-2 p-4" data-testid="settings-loading">
      {[0, 1, 2, 3, 4].map((i) => (
        <div key={i} className="grid gap-2 pb-4"><Skeleton width="35%" /><Skeleton width="60%" /><Skeleton width="80%" /></div>
      ))}
    </Card>
  );
}

function SettingsPage() {
  const named = useSample();
  const sample = isSample(named) ? named : null;
  const { hash } = useLocation();
  const { data, error, reload } = usePagePolling<SettingsPage>(() => (sample ? sampleLoad(sample) : factorySettings()), POLL_MS);
  // What was just changed shows at once; the next read replaces it.
  const [over, setOver] = React.useState<CrewChange>({});
  const [problem, setProblem] = React.useState("");
  React.useEffect(() => { setOver({}); }, [data]);

  // A link such as /settings#crew lands on its section once the page has drawn.
  const scrolled = React.useRef("");
  React.useEffect(() => {
    if (!data || !hash || scrolled.current === hash) return;
    scrolled.current = hash;
    document.getElementById(hash.slice(1))?.scrollIntoView();
  }, [data, hash]);

  const failed = (e: unknown) => (e instanceof ApiError && e.status === 403 ? "Only an admin can change this." : SAVE_FAILED);
  const onCrew = async (c: CrewChange) => {
    setProblem("");
    if (sample) return;
    setOver((o) => ({ ...o, ...c }));
    try { await changeCrew(c); } catch (e) { setOver({}); setProblem(failed(e)); }
    reload();
  };
  const onGrant = async (words: string) => {
    if (sample) return null;
    try { await addGrant(words); reload(); return null; } catch (e) { return e instanceof ApiError && e.status !== 403 && e.message ? e.message : failed(e); }
  };
  const onSwitch = async (edition: string) => {
    if (sample) return;
    try { await switchEdition(edition); window.location.reload(); } catch (e) { setProblem(failed(e)); }
  };

  const page = data && {
    ...data,
    crew: {
      ...data.crew,
      how_far: { ...data.crew.how_far, value: over.autonomy ?? data.crew.how_far.value, releases: over.releaser ?? data.crew.how_far.releases },
      builders_max: over.builders_max ?? data.crew.builders_max,
      builders_text: over.builders_max ? `Up to ${over.builders_max} ${over.builders_max === 1 ? "builder" : "builders"} at once` : data.crew.builders_text,
      challenger: over.challenger ?? data.crew.challenger,
      pace: over.pace ?? data.crew.pace,
    },
  };

  return (
    <div className="grid gap-6 md:gap-8" data-testid="factory-settings">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">Settings</h1>
        <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground">What the crew may do, which tools it uses and who can reach it.</p>
      </div>
      {error && (
        <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
          <b>Settings did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
        </Banner>
      )}
      {!page && !error && <Loading />}
      {page && <FactorySettingsView page={page} problem={problem} onCrew={onCrew} onGrant={onGrant} onSwitch={onSwitch} />}
    </div>
  );
}

// Old addresses such as /settings?tab=github (the GitHub App return, the Tares Cloud return and the
// console's own links) name a tab of the full settings. They go there with the same query.
export default function FactorySettings() {
  const { search } = useLocation();
  const q = new URLSearchParams(search);
  if (q.has("tab") || q.has("cloud")) return <Navigate to={`/settings/advanced${search}`} replace />;
  return <SettingsPage />;
}
