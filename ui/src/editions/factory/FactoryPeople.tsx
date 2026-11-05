import * as React from "react";
import { Link } from "react-router-dom";

import { ApiError, cloudApi, type CloudResult } from "../../api";
import { signInLink, slugFromHost, useCloud, useWorkspaceOverview } from "../../cloud";
import { Banner, Button, Input, ListRow, ListSection, Skeleton, StatusDot } from "../../components/edition";
import { cn } from "../../lib/utils";
import { usePagePolling } from "../usePagePolling";
import { addPerson, factoryPeople, type FactoryPeopleData, type PersonRow } from "./FactoryPeople.api";

// People (TR-1011): who is on the cell, and the one way to add a teammate. One read
// (GET /api/factory/people), repeated every 30 seconds. On a self-hosted cell adding someone is
// POST /api/factory/people. On Tares Cloud the console invites through the workspace with the
// person's own Cloud session, the call Settings > Workspace makes (cloudApi.invite, TR-1012), and
// only the workspace owner sees the form. Names, emails, project names and repos are the person's
// own text and marked data-user-text.

const POLL_MS = 30_000;
const MUTED = "text-[13px] text-muted-foreground";
const LINK = "text-foreground! underline! decoration-border! underline-offset-[3px]";

function Row({ p }: { p: PersonRow }) {
  return (
    <ListRow className="grid grid-cols-1 items-start gap-x-4 gap-y-1.5 md:grid-cols-[1fr_auto]" data-testid="person-row"
             data-person={p.id} data-state={p.state}>
      <div className="min-w-0">
        <div className="font-semibold [overflow-wrap:anywhere]">
          <span data-user-text data-testid="person-name">{p.name}</span>
          {p.you && <span className="font-normal text-muted-foreground"> (you)</span>}
          {p.admin && <span className="ml-2 text-[12px] font-normal text-muted-foreground" data-testid="person-admin">Admin</span>}
        </div>
        {p.email && <div className={cn(MUTED, "[overflow-wrap:anywhere]")} data-user-text data-testid="person-email">{p.email}</div>}
      </div>
      <div className={cn(MUTED, "flex items-center gap-1.5 md:justify-end")} data-testid="person-state">
        <StatusDot tone={p.state === "active" ? "ready" : "quiet"} className="rounded-full" />{p.state_words}
      </div>
      {(p.leads.length > 0 || p.owns.length > 0) && (
        <div className={cn("col-span-full grid gap-1", MUTED)}>
          {p.leads.length > 0 && (
            <div data-testid="person-leads">
              Leads{" "}
              {p.leads.map((l, i) => (
                <React.Fragment key={l.id}>
                  {i > 0 && ", "}
                  <Link to={`/projects/${encodeURIComponent(l.id)}`} className={LINK} data-user-text>{l.name}</Link>
                </React.Fragment>
              ))}
            </div>
          )}
          {p.owns.length > 0 && (
            <div data-testid="person-owns">Owns <span data-user-text>{p.owns.join(", ")}</span></div>
          )}
        </div>
      )}
    </ListRow>
  );
}

/** How a person is added here: the cell itself, or the Tares Cloud workspace. */
type Adder =
  | { kind: "cell" }
  | { kind: "cloud"; invite: (email: string) => Promise<CloudResult<unknown>>; loginUrl?: string };

/** The add form: an email (and on a self-hosted cell a name, optional), then Add or Send invite. */
function AddPerson({ data, adder, onAdded }: { data: FactoryPeopleData; adder: Adder; onAdded: (words: string) => void }) {
  const [email, setEmail] = React.useState("");
  const [name, setName] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [signedOut, setSignedOut] = React.useState(false);
  const cloud = adder.kind === "cloud";
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const to = email.trim();
    if (!to) { setError("Enter an email address."); return; }
    setBusy(true); setError(""); setSignedOut(false);
    try {
      if (adder.kind === "cloud") {
        const r = await adder.invite(to);
        if (r.status === "ok") { setEmail(""); onAdded(`Invited ${to}. They join this workspace when they sign in.`); }
        else if (r.status === "signed_out") setSignedOut(true);
        else setError(sentence(r.detail));
      } else {
        const p = await addPerson(to, name.trim());
        setEmail(""); setName("");
        onAdded(`Added ${p.name}.`);
      }
    } catch (err) {
      setError(err instanceof ApiError ? sentence(err.message) : "That did not go through. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  };
  return (
    <ListSection title="Add a person" data-testid="people-add">
      <form onSubmit={submit} className="grid gap-3 px-4 py-4" noValidate>
        <p className={cn("m-0!", MUTED)} data-testid="people-add-text">{data.add_text}</p>
        <div className={cn("grid gap-3 md:items-end", cloud ? "md:grid-cols-[1fr_auto]" : "md:grid-cols-[1fr_1fr_auto]")}>
          <Input label="Email" type="email" autoComplete="off" value={email} onChange={(e) => setEmail(e.target.value)}
                 placeholder="name@company.com" data-testid="people-email" error={error || undefined} />
          {!cloud && <Input label="Name (optional)" value={name} onChange={(e) => setName(e.target.value)} data-testid="people-name" />}
          <Button type="submit" busy={busy} data-testid="people-submit">{cloud ? "Send invite" : "Add"}</Button>
        </div>
        {signedOut && adder.kind === "cloud" && <SignIn loginUrl={adder.loginUrl} />}
      </form>
    </ListSection>
  );
}

function SignIn({ loginUrl }: { loginUrl?: string }) {
  return (
    <p className={cn("m-0!", MUTED)} data-testid="people-signed-out">
      You are signed out of Tares Cloud.{" "}
      {loginUrl ? <a href={signInLink(loginUrl)} className={LINK}>Sign in to invite people.</a> : "Sign in to invite people."}
    </p>
  );
}

/** A refusal as one sentence that ends with a full stop. */
const sentence = (s: string) => {
  const t = s.trim();
  return t ? t.charAt(0).toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".") : "That did not go through.";
};

function Loading() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading" className="border border-border bg-card p-4" data-testid="people-loading">
      {[0, 1, 2].map((i) => <div key={i} className="grid gap-2 pb-4 pt-1"><Skeleton width="40%" /><Skeleton width="25%" /></div>)}
    </div>
  );
}

export function FactoryPeopleView({ data, onAdded, note, add, refusal }: {
  data: FactoryPeopleData; onAdded: (words: string) => void; note: string;
  /** How to add someone here; null shows `refusal` (or nothing while Tares Cloud is asked). */
  add: Adder | null; refusal?: React.ReactNode;
}) {
  const { people } = data;
  return (
    <PeopleHead>
      {note && <Banner tone="note" data-testid="people-note">{note}</Banner>}
      <ListSection title="On this cell" aside={<span className="font-mono" data-testid="people-count">{people.length}</span>} data-testid="people-list">
        {people.map((p) => <Row key={p.id} p={p} />)}
      </ListSection>
      {add
        ? <AddPerson data={data} adder={add} onAdded={onAdded} />
        : refusal}
    </PeopleHead>
  );
}

/** On Tares Cloud: who may invite, from the workspace's own answer. */
function useCloudAdder(): { add: Adder | null; refusal: React.ReactNode } | null {
  const cloud = useCloud();
  const apiUrl = cloud.health?.workspace_api_url;
  const loginUrl = cloud.health?.login_url;
  const ov = useWorkspaceOverview(apiUrl);
  if (!apiUrl) return null;
  const r = ov.result;
  if (!r) return { add: null, refusal: null };
  if (r.status === "signed_out") return { add: null, refusal: <SignIn loginUrl={loginUrl} /> };
  if (r.status === "error") {
    return { add: null, refusal: <p className={cn("m-0", MUTED)} data-testid="people-add-text">{sentence(r.detail)}</p> };
  }
  if (r.data.role !== "owner") {
    return { add: null, refusal: <p className={cn("m-0", MUTED)} data-testid="people-add-text">Only the workspace owner can invite people.</p> };
  }
  const slug = r.data.slug || slugFromHost();
  return { add: { kind: "cloud", loginUrl, invite: (email) => cloudApi.invite(apiUrl, slug, email) }, refusal: null };
}

export default function FactoryPeople() {
  const { data, error, reload } = usePagePolling<FactoryPeopleData>(factoryPeople, POLL_MS);
  const [note, setNote] = React.useState("");
  const cloudAdder = useCloudAdder();
  const banner = error && (
    <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
      <b>People did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
    </Banner>
  );
  if (!data) return <PeopleHead>{banner || <Loading />}</PeopleHead>;
  return (
    <>
      {banner && <div className="mb-6">{banner}</div>}
      <FactoryPeopleView data={data} note={note} onAdded={(w) => { setNote(w); reload(); }}
                         add={cloudAdder ? cloudAdder.add : data.can_add ? { kind: "cell" } : null}
                         refusal={cloudAdder ? cloudAdder.refusal
                           : <p className={cn("m-0", MUTED)} data-testid="people-add-text">{data.add_text}</p>} />
    </>
  );
}

function PeopleHead({ children }: { children?: React.ReactNode }) {
  return (
    <div className="grid gap-5 md:gap-6" data-testid="factory-people">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">People</h1>
        <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground">Who works on this cell, the projects they lead and the repos they own.</p>
      </div>
      {children}
    </div>
  );
}
