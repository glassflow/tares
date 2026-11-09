// Tares enterprise licence, see ee/LICENSE
import * as React from "react";
import { Link } from "react-router-dom";

import { ApiError, cloudApi, isLicenceRequired, type CloudResult } from "../../api";
import { signInLink, slugFromHost, useCloud, useWorkspaceOverview } from "../../cloud";
import LicenceNeeded from "../../components/LicenceNeeded";
import { Button, Input, ListSection } from "../../components/edition";
import type { FactoryPeopleData, PersonRow } from "../../editions/factory/FactoryPeople.api";
import { LINK, MUTED, sentence } from "../../editions/factory/peopleText";
import { cn } from "../../lib/utils";
import { addPerson } from "../api";

// The team half of the People page (TR-930): the projects a person leads, the repos they own, and
// adding someone. On a self-hosted cell adding is POST /api/factory/people. On Tares Cloud the
// invite goes through the workspace with the person's own Cloud session, the call Settings >
// Workspace makes, and only the workspace owner sees the form.

/** How a person is added here: the cell itself, or the Tares Cloud workspace. */
export type Adder =
  | { kind: "cell" }
  | { kind: "cloud"; invite: (email: string) => Promise<CloudResult<unknown>>; loginUrl?: string };

/** The Leads and Owns lines under a person's row. */
export function PersonTeamLines({ p }: { p: PersonRow }) {
  if (p.leads.length === 0 && p.owns.length === 0) return null;
  return (
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
  );
}

/** The add form: an email (and on a self-hosted cell a name, optional), then Add or Send invite. */
export function AddPerson({ data, adder, onAdded }: { data: FactoryPeopleData; adder: Adder; onAdded: (words: string) => void }) {
  const [email, setEmail] = React.useState("");
  const [name, setName] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [signedOut, setSignedOut] = React.useState(false);
  const [licence, setLicence] = React.useState(false);
  const cloud = adder.kind === "cloud";
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const to = email.trim();
    if (!to) { setError("Enter an email address."); return; }
    setBusy(true); setError(""); setSignedOut(false); setLicence(false);
    try {
      if (adder.kind === "cloud") {
        const r = await adder.invite(to);
        if (r.status === "ok") { setEmail(""); onAdded(`Invited ${to}. They join this workspace when they sign in.`); }
        else if (r.status === "signed_out") setSignedOut(true);
        else if (r.code === 402) setLicence(true);   // the workspace has no licence for teams
        else setError(sentence(r.detail));
      } else {
        const p = await addPerson(to, name.trim());
        setEmail(""); setName("");
        onAdded(`Added ${p.name}.`);
      }
    } catch (err) {
      if (isLicenceRequired(err)) setLicence(true);
      else setError(err instanceof ApiError ? sentence(err.message) : "That did not go through. Try again in a moment.");
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
        {licence && <LicenceNeeded what={cloud ? "Inviting people" : "Adding people"} />}
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

/** On Tares Cloud: who may invite, from the workspace's own answer. */
export function useCloudAdder(): { add: Adder | null; refusal: React.ReactNode } | null {
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

/** What stands in the People page where someone is added: the form for who may add, else the reason. */
export function PeopleAdd({ data, onAdded }: { data: FactoryPeopleData; onAdded: (words: string) => void }) {
  const cloudAdder = useCloudAdder();
  const add: Adder | null = cloudAdder ? cloudAdder.add : data.can_add ? { kind: "cell" } : null;
  if (add) return <AddPerson data={data} adder={add} onAdded={onAdded} />;
  return <>{cloudAdder ? cloudAdder.refusal
    : <p className={cn("m-0", MUTED)} data-testid="people-add-text">{data.add_text}</p>}</>;
}
