import { createContext, useContext, useEffect, useState } from "react";

import { api } from "../../api";
import { Banner, Button, Input, ListRow, Panel, SkeletonRows } from "../../components/edition";
import LicenceNeeded from "../../components/LicenceNeeded";
import { EE_FAILED_WORDS, LICENCE_NO_ANSWER_WORDS, Slot, useEeFailed, useLicenceNoAnswer, useEePending } from "../../eeSlots";
import { extraEmails, personDraft, type PersonDraft } from "../../personDraft";
import type { Person, Whoami } from "../../types";

// Who may reach this cell, and who is on it, on the edition kit (TR-1392). The platform Settings page
// (Access tab) shows the Access box, People and the keys; Factory Settings shows People under Team and
// the Access box above its keys, only when the cell is open (TerseSettings).

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";
const CODE = "font-mono text-[12px] text-foreground";
const FORM = "grid gap-4 border border-border bg-muted/40 p-4";

const messageOf = (e: unknown) => String((e as Error)?.message ?? e);
/** The daemon's refusal, as a sentence. */
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** On the factory Settings page the sidebar already says where you are, so these panels drop their
 *  introductions and take the sidebar's words for their headings; the Access box shows only on an open cell. */
export const TerseSettings = createContext(false);

// Access, people and keys share one load: who is asking (admin or not) and who is on the cell, so
// a person added in People is there to pick when a key is made. Only an admin credential manages
// people and keys (the daemon enforces it); the page just does not offer what it would refuse.
/** Who is asking and who is on the cell: what the people and key panels read. */
export function useAccessData() {
  const [me, setMe] = useState<Whoami>();
  const [people, setPeople] = useState<Person[]>();
  const [err, setErr] = useState<string>();
  const reload = () => api.people().then((r) => { setPeople(r.people); setErr(undefined); })
    .catch((e) => setErr(messageOf(e)));
  useEffect(() => {
    api.whoami().then(setMe).catch(() => undefined);
    reload();
  }, []);
  return { me, people, err, reload, admin: !!me?.scopes.includes("admin") };
}

/** Who signed in, as a sentence, or nothing on an open cell. */
export function signedInWords(me?: Whoami): string | undefined {
  if (!me) return undefined;
  if (me.kind === "system") return "You signed in with the root token printed by tares up --auth.";
  if (me.kind === "project") return `You signed in with a project key for ${me.project_name ?? me.project}.`;
  if (me.kind === "open") return undefined;
  return `You signed in with the API key ${me.name}, which belongs to ${me.person ? me.person.name : "no one"}.`;
}

// Auth is set at launch: `tares up` is open; `tares up --auth` requires a sign-in (a root token printed
// to the terminal). There is no switch at runtime, so this says which it is; it changes nothing.
export function AccessPanel({ me }: { me?: Whoami }) {
  const terse = useContext(TerseSettings);
  const [authRequired, setAuthRequired] = useState<boolean>();
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    api.health().then((h) => setAuthRequired(h.auth_required)).catch(() => setFailed(true));
  }, []);

  if (terse && authRequired !== false) return null;   // only an open cell has something to say here
  const who = signedInWords(me);
  return (
    <Panel title="Access" data-testid="access">
      {authRequired === undefined ? (
        failed ? <Banner tone="error">The cell did not answer. Reload the page to see who can reach it.</Banner> : <SkeletonRows count={1} />
      ) : authRequired ? (
        <p className="m-0 text-[13px] leading-relaxed" data-testid="access-on">
          The console and the API ask for a sign-in.{" "}
          {who && <span data-user-text>{who} </span>}
          <span className="text-muted-foreground">Give each machine its own API key below, never the root token.</span>
        </p>
      ) : (
        <Banner tone="warn" data-testid="access-open">
          This cell is open: anyone who can reach it can read and change everything, and API keys are not checked. Start
          it with <code className={CODE}>tares up --auth</code> to ask for a sign-in.
        </Banner>
      )}
    </Panel>
  );
}

/** People on the cell: the enterprise list when licensed, else the one person, you. */
export function PeopleBlock({ access }: { access: ReturnType<typeof useAccessData> }) {
  const pending = useEePending();
  const eeFailed = useEeFailed();
  const noAnswer = useLicenceNoAnswer();
  const { me, people, err, reload, admin } = access;
  const fallback = pending ? <Panel title="People" data-testid="people-pending"><SkeletonRows count={1} /></Panel>
    : eeFailed ? <Panel title="People"><Banner tone="error" data-testid="ee-failed">{EE_FAILED_WORDS}</Banner></Panel>
    : noAnswer ? <Panel title="People"><Banner tone="error" data-testid="licence-no-answer">{LICENCE_NO_ANSWER_WORDS}</Banner></Panel>
    : <YouPanel people={people} admin={admin} err={err} onChanged={reload} me={me} />;
  return <Slot name="settings.people" people={people} admin={admin} err={err} onChanged={reload} me={me} fallback={fallback} />;
}

// Without the enterprise licence the cell has one person to manage: you. The row gives the name,
// email and GitHub login, and Edit changes them (an admin all three, anyone else only the name; the
// daemon decides). Adding anyone else is the licence's, so the last line says so.
function YouPanel({ people, admin, err: loadErr, onChanged, me }: {
  people?: Person[]; admin: boolean; err?: string; onChanged: () => void; me?: Whoami;
}) {
  const mine = people?.find((p) => p.id === me?.person?.id);
  const [editing, setEditing] = useState<PersonDraft>();
  // The boot person on a cell that asks for a sign-in is no one's login: say so instead of "You".
  const shown = mine && (mine.id === "local" && mine.name === "You" && me?.kind !== "open" ? "Local (no login)" : mine.name);
  return (
    <Panel title="People" data-testid="you"
           action={mine && !editing ? <Button variant="secondary" data-testid="you-edit" onClick={() => setEditing(personDraft(mine))}>Edit</Button> : undefined}>
      <div className="grid gap-4">
        <p className={TEXT}>
          You are the one person on this cell. Each API key belongs to a person, so you can tell whose agent or script is acting.
        </p>
        {loadErr && <Banner tone="error">{sentence(loadErr)}</Banner>}
        {!people ? (!loadErr && <SkeletonRows count={1} />) : mine && (editing ? (
          <YouForm draft={editing} admin={admin} onCancel={() => setEditing(undefined)}
                   onSaved={() => { setEditing(undefined); onChanged(); }} />
        ) : (
          <div className="-mt-2 [--row-x:0px]">
            <ListRow className="hover:bg-transparent" data-testid="you-row">
              <div className="grid min-w-0 gap-1">
                <span className="text-sm font-semibold" data-user-text>{shown}</span>
                <p className="m-0 text-[13px] text-muted-foreground" data-testid="you-email">
                  {mine.email ? <span data-user-text>{mine.email}</span> : "No email."}
                </p>
                <p className="m-0 text-[13px] text-muted-foreground" data-testid="you-github">
                  {mine.github ? <>GitHub login <span className={CODE} data-user-text>{mine.github}</span></> : "No GitHub login."}
                </p>
              </div>
            </ListRow>
          </div>
        ))}
        <LicenceNeeded what="Adding people" />
      </div>
    </Panel>
  );
}

function YouForm({ draft, admin, onCancel, onSaved }: {
  draft: PersonDraft; admin: boolean; onCancel: () => void; onSaved: () => void;
}) {
  const [d, setD] = useState(draft);
  const [busy, setBusy] = useState(false);
  const [errs, setErrs] = useState<{ name?: string; email?: string; github?: string; other?: string }>({});
  const set = (k: "name" | "email" | "github", v: string) => { setD({ ...d, [k]: v }); setErrs({ ...errs, [k]: undefined }); };
  const submit = async () => {
    if (!d.name.trim()) { setErrs({ name: "Give your name." }); return; }
    setBusy(true); setErrs({});
    try {
      await api.updatePerson(d.id, admin
        ? { name: d.name.trim(), email: d.email.trim(), github: d.github.trim(), emails: extraEmails(d) }
        : { name: d.name.trim() });
      onSaved();
    } catch (e) {
      const text = sentence(messageOf(e));
      if (/github/i.test(text)) setErrs({ github: text });
      else if (/e-?mail/i.test(text)) setErrs({ email: text });
      else if (/^name\b/i.test(text)) setErrs({ name: text });
      else setErrs({ other: text });
      setBusy(false);
    }
  };
  return (
    <form className={FORM} data-testid="you-form" onSubmit={(e) => { e.preventDefault(); void submit(); }}
          onKeyDown={(e) => { if (e.key === "Escape" && !busy) onCancel(); }}>
      <Input label="Name" autoComplete="name" autoFocus value={d.name} error={errs.name} data-testid="you-name"
             onChange={(e) => set("name", e.target.value)} />
      {admin && (
        <>
          <Input label="Email" type="email" autoComplete="email" value={d.email} error={errs.email} data-testid="you-email-input"
                 hint="Where the cell writes to you. Your old address still finds you." onChange={(e) => set("email", e.target.value)} />
          <Input label="GitHub login" className="font-mono" placeholder="octocat" autoComplete="off" spellCheck={false}
                 value={d.github} error={errs.github} data-testid="you-github-input"
                 hint="So the work you do on GitHub is known as yours." onChange={(e) => set("github", e.target.value)} />
        </>
      )}
      {!admin && <p className={TEXT}>Only someone who can change settings can change your email and GitHub login.</p>}
      {errs.other && <Banner tone="error">{errs.other}</Banner>}
      <div className="flex flex-wrap gap-2">
        <Button type="submit" busy={busy} data-testid="you-save">Save</Button>
        <Button type="button" variant="secondary" disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}
