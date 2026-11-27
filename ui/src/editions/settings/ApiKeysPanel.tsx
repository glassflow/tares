import { useCallback, useEffect, useState } from "react";

import { api } from "../../api";
import { Banner, Button, Checkbox, Collapse, Dialog, EmptyState, Input, ListRow, Panel, SkeletonRows, StatusLabel } from "../../components/edition";
import { keyTitle } from "../../components/bits";
import { Slot } from "../../eeSlots";
import { ageWords, formatStampUTC } from "../../lib/time";
import type { ApiKey, Person } from "../../types";
import { copyText } from "../copyText";

// Keys for agents, scripts and services, on the edition kit (TR-1392). Factory Settings > Access keys
// and the platform Settings page (Access tab) both show this. A key carries scopes (read, ingest,
// admin; docs/design/api-keys.md), may be bound to a person (the ee slots pick and rebind one), and a
// project key reads one project only (made on that project's page, listed here). The secret exists
// only in the create response; the daemon keeps its hash, so the panel shows it once.
// Whether keys are checked at all is the Access box's to say (AccessPanels.tsx); both callers show it.

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";
const FORM = "grid gap-4 border border-border bg-muted/40 p-4";

const messageOf = (e: unknown) => String((e as Error)?.message ?? e);
/** The daemon's refusal, as a sentence. */
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const ago = (ts: string) => ageWords(Date.now() - Date.parse(ts));

/** The daemon's scope names, as people say them; the AI SRE Settings page uses the same words
 *  (tares/screens/sre_settings.py SCOPE_WORDS). An id is what the daemon takes, never shown. */
const SCOPES: { id: string; words: string; label: string; hint: string }[] = [
  { id: "read", words: "read", label: "Read", hint: "Look things up. For an agent connecting over MCP." },
  { id: "ingest", words: "send data", label: "Send data", hint: "Send events in. For a script or service that pushes logs or webhooks." }, // words-ok
  { id: "admin", words: "change settings", label: "Change settings", hint: "Everything, settings included." },
];

const joinWords = (xs: string[]) => (xs.length < 2 ? xs.join("") : `${xs.slice(0, -1).join(", ")} and ${xs[xs.length - 1]}`);

/** What a key can do, as a sentence. A project key reads that project only. */
export function scopeWords(k: Pick<ApiKey, "scopes" | "project">, projectName?: string): string {
  if (k.project) {
    const where = projectName ?? "one project";
    return `Can read ${where} only.`;
  }
  const words = SCOPES.filter((s) => k.scopes.includes(s.id)).map((s) => s.words);
  return words.length ? `Can ${joinWords(words)}.` : "No access.";
}

/** When it was made and last used, or revoked. */
function timeWords(k: ApiKey): string {
  if (k.revoked_at) return `Revoked ${ago(k.revoked_at)}.`;
  const used = k.last_used_at ? `Used ${ago(k.last_used_at)}.` : "Never used.";
  return k.created_at ? `${used} Made ${ago(k.created_at)}.` : used;
}

export function ApiKeysPanel({ title, intro, people, admin, me }: {
  /** Factory Settings says "Access keys"; the platform Settings page "API keys". */
  title: string;
  /** The platform page explains what the scopes are for; Factory Settings keeps it to the list. */
  intro?: boolean;
  people: Person[]; admin: boolean; me?: string;
}) {
  const [keys, setKeys] = useState<ApiKey[]>();
  const [loadErr, setLoadErr] = useState<string>();
  const [rowErr, setRowErr] = useState<{ id: string; text: string }>();
  const [projectNames, setProjectNames] = useState<Record<string, string>>({});
  const [adding, setAdding] = useState(false);
  const [created, setCreated] = useState<{ name: string; secret: string; words: string }>();
  const [revoking, setRevoking] = useState<ApiKey>();
  const [busy, setBusy] = useState<string>();
  const [showRevoked, setShowRevoked] = useState(false);

  const load = useCallback(() => api.keys()
    .then((r) => { setKeys(r.keys); setLoadErr(undefined); })
    .catch((e) => setLoadErr(sentence(messageOf(e)))), []);
  useEffect(() => { void load(); }, [load]);
  // a project key names its project; made on the project's page, it is listed here
  useEffect(() => {
    api.projectsWithHidden().then((r) => setProjectNames(Object.fromEntries(r.projects.map((p) => [p.id, p.name]))))
      .catch(() => undefined);
  }, []);

  const revoke = async (k: ApiKey) => {
    setBusy(`revoke:${k.id}`); setRowErr(undefined);
    try { await api.revokeKey(k.id); }
    catch (e) { setRowErr({ id: k.id, text: sentence(messageOf(e)) }); }
    setBusy(undefined); setRevoking(undefined);
    await load();
  };

  const live = keys?.filter((k) => !k.revoked_at) ?? [];
  const revoked = keys?.filter((k) => k.revoked_at) ?? [];
  const action = !adding && keys && (
    <Button onClick={() => { setAdding(true); setCreated(undefined); }} data-testid="keys-add">Create a key</Button>
  );

  return (
    <Panel title={title} action={action || undefined} data-testid="keys">
      <div className="grid gap-4">
        {intro && (
          <p className={TEXT}>
            Give each agent, script or service its own key, so you can revoke one without touching the others. A key
            can read (an agent connecting over MCP), send data (a script or service pushing logs or webhooks), both (the
            Claude Code plugin), or change settings, which includes everything else.
          </p>
        )}
        <Collapse>{loadErr && keys && <Banner tone="error">{loadErr}</Banner>}</Collapse>
        {created && <CreatedKey created={created} onDone={() => setCreated(undefined)} />}

        {adding && (
          <KeyForm people={people} defaultPerson={me} onCancel={() => setAdding(false)}
                   onCreated={(c) => { setAdding(false); setCreated(c); void load(); }} />
        )}

        {!keys ? (
          loadErr ? <Banner tone="error">{loadErr}</Banner> : <SkeletonRows count={2} />
        ) : live.length === 0 && !showRevoked ? (
          !adding && (
            <EmptyState data-testid="keys-empty">
              No keys yet. Create one for each agent, script or service that talks to this cell.
            </EmptyState>
          )
        ) : (
          <div className="-mt-2 [--row-x:0px]">
            {[...live, ...(showRevoked ? revoked : [])].map((k) => (
              <KeyRow key={k.id} k={k} people={people} admin={admin} projectName={k.project ? projectNames[k.project] : undefined}
                      busy={busy !== undefined} error={rowErr?.id === k.id ? rowErr.text : undefined}
                      onRevoke={() => { setRowErr(undefined); setRevoking(k); }} onChanged={() => void load()}
                      onError={(text) => setRowErr({ id: k.id, text: sentence(text) })} />
            ))}
          </div>
        )}
        {revoked.length > 0 && (
          <div>
            <Button variant="quiet" className="h-8 px-0 text-[13px]" data-testid="keys-revoked-toggle" onClick={() => setShowRevoked((v) => !v)}>
              {showRevoked ? "Hide revoked keys" : `Show ${revoked.length === 1 ? "1 revoked key" : `${revoked.length} revoked keys`}`}
            </Button>
          </div>
        )}
      </div>

      {revoking && (
        <Dialog open onOpenChange={(o) => { if (!o && !busy) setRevoking(undefined); }} data-testid="keys-revoke-dialog"
                title={`Revoke ${revoking.name}?`}
                description="It stops working at once and its subscriptions are removed. Agents, scripts or services still using it start failing."
                footer={<>
                  <Button variant="secondary" disabled={busy !== undefined} onClick={() => setRevoking(undefined)}>Cancel</Button>
                  <Button busy={busy === `revoke:${revoking.id}`} data-testid="keys-revoke-confirm" onClick={() => void revoke(revoking)}>Revoke</Button>
                </>} />
      )}
    </Panel>
  );
}

function KeyRow({ k, people, admin, projectName, busy, error, onRevoke, onChanged, onError }: {
  k: ApiKey; people: Person[]; admin: boolean; projectName?: string; busy: boolean; error?: string;
  onRevoke: () => void; onChanged: () => void; onError: (text: string) => void;
}) {
  const named = keyTitle(k.name);
  return (
    <ListRow className={k.revoked_at ? "text-muted-foreground hover:bg-transparent" : "hover:bg-transparent"}
             data-testid="keys-row" data-id={k.id}>
      <div className="grid min-w-0 flex-[1_1_320px] gap-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-sm font-semibold" data-user-text={named ? undefined : true}>{named ?? k.name}</span>
          {k.revoked_at && <StatusLabel tone="quiet" data-testid="keys-revoked">Revoked</StatusLabel>}
          <span className="font-mono text-xs text-muted-foreground" data-words-exempt title="The start of the key">{k.prefix}…</span>
        </div>
        <p className="m-0 text-[13px]" data-testid="keys-can">{scopeWords(k, projectName)}</p>
        <p className="m-0 text-[13px] text-muted-foreground" data-testid="keys-used"
           title={k.last_used_at ? `Last used ${formatStampUTC(k.last_used_at)}` : k.created_at ? `Made ${formatStampUTC(k.created_at)}` : undefined}>
          {timeWords(k)}
        </p>
        <div className="flex flex-wrap items-center gap-2 text-[13px]" data-testid="keys-person">
          <span className="text-muted-foreground">Belongs to</span>
          <Slot name="settings.keyPerson" k={k} people={people} admin={admin} onChanged={onChanged} onError={onError}
                fallback={k.person ? <span data-user-text>{k.person.name}</span> : <span>no one</span>} />
        </div>
        {error && <p className="m-0 text-[13px] text-err" role="alert" data-testid="keys-row-error">{error}</p>}
      </div>
      {!k.revoked_at && (
        <Button variant="quiet" className="text-err hover:text-err" disabled={busy} data-testid="keys-revoke" onClick={onRevoke}>Revoke</Button>
      )}
    </ListRow>
  );
}

/** The secret, once: the daemon keeps only its hash. */
function CreatedKey({ created, onDone }: { created: { name: string; secret: string; words: string }; onDone: () => void }) {
  const [copy, setCopy] = useState<"idle" | "copied" | "failed">("idle");
  const doCopy = async () => {
    try { await copyText(created.secret); setCopy("copied"); setTimeout(() => setCopy("idle"), 1500); }
    catch { setCopy("failed"); }
  };
  return (
    <div className="grid gap-2 border border-border bg-muted px-4 py-3.5" role="status" data-testid="keys-created">
      <p className="m-0 text-sm">
        <b>Key created: <span data-user-text>{created.name}</span>.</b> {created.words} Copy it now, it is not shown again.
      </p>
      <code data-words-exempt data-testid="keys-created-secret"
            className="block max-w-full overflow-x-auto border border-border bg-background px-3 py-2 font-mono text-[13px] [overflow-wrap:anywhere]">
        {created.secret}
      </code>
      {copy === "failed" && <p className="m-0 text-[13px] text-err" role="alert">Could not copy. Select the key and copy it by hand.</p>}
      <div className="flex flex-wrap gap-2">
        <Button variant="secondary" onClick={() => void doCopy()} data-testid="keys-created-copy">{copy === "copied" ? "Copied" : "Copy key"}</Button>
        <Button variant="quiet" onClick={onDone} data-testid="keys-created-done">I have copied it</Button>
      </div>
    </div>
  );
}

function KeyForm({ people, defaultPerson, onCancel, onCreated }: {
  people: Person[]; defaultPerson?: string;
  onCancel: () => void;
  onCreated: (c: { name: string; secret: string; words: string }) => void;
}) {
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read"]);
  // the licensed picker may change it (or pick no one); without it the key is made for you
  const [personId, setPersonId] = useState(defaultPerson ?? "");
  const [busy, setBusy] = useState(false);
  const [nameErr, setNameErr] = useState<string>();
  const [scopeErr, setScopeErr] = useState<string>();
  const [err, setErr] = useState<string>();

  const submit = async () => {
    setNameErr(undefined); setScopeErr(undefined); setErr(undefined);
    if (!name.trim()) { setNameErr("Give the key a name."); return; }
    if (scopes.length === 0) { setScopeErr("Pick at least one thing it can do."); return; }
    setBusy(true);
    try {
      const r = await api.createKey(name.trim(), scopes, personId || undefined);
      onCreated({ name: r.name, secret: r.secret, words: scopeWords({ scopes: r.scopes, project: null }) });
    } catch (e) {
      const text = sentence(messageOf(e));
      if (/^name\b/i.test(text)) setNameErr(text);
      else if (/scope/i.test(text)) setScopeErr(text);
      else setErr(text);
      setBusy(false);
    }
  };

  return (
    <form className={FORM} data-testid="keys-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}
          onKeyDown={(e) => { if (e.key === "Escape" && !busy) onCancel(); }}>
      <Input label="Name" placeholder="otel-prod" autoComplete="off" spellCheck={false} autoFocus value={name}
             hint="Who uses it. One key per agent, script or service, so you can revoke one alone."
             error={nameErr} data-testid="keys-name" onChange={(e) => { setName(e.target.value); setNameErr(undefined); }} />
      <Slot name="settings.keyPersonPick" people={people} value={personId} onChange={setPersonId} />
      <fieldset className="m-0 grid gap-2 border-0 p-0" aria-describedby={scopeErr ? "keys-scope-error" : undefined}>
        <legend className="mb-1 p-0 text-xs font-semibold">What it can do</legend>
        {SCOPES.map((s) => (
          <Checkbox key={s.id} label={s.label} hint={s.hint} data-testid={`keys-scope-${s.id}`} checked={scopes.includes(s.id)}
                    onCheckedChange={(on) => { setScopeErr(undefined); setScopes((v) => on === true ? [...v, s.id] : v.filter((x) => x !== s.id)); }} />
        ))}
        {scopeErr && <p id="keys-scope-error" className="m-0 text-xs text-err" role="alert" data-testid="keys-scope-error">{scopeErr}</p>}
      </fieldset>
      <Collapse>{err && <Banner tone="error">{err}</Banner>}</Collapse>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" busy={busy} data-testid="keys-create">Create key</Button>
        <Button type="button" variant="secondary" disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
