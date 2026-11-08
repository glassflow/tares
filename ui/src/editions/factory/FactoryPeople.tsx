import * as React from "react";

import { api, ApiError } from "../../api";
import LicenceNeeded from "../../components/LicenceNeeded";
import { Banner, Button, Initials, Input, ListRow, ListSection, Skeleton, StatusDot } from "../../components/edition";
import { Slot, useEePending } from "../../eeSlots";
import { cn } from "../../lib/utils";
import { useEdition } from "../edition";
import { usePagePolling } from "../usePagePolling";
import { factoryPeople, type FactoryPeopleData, type PersonRow } from "./FactoryPeople.api";
import { MUTED, sentence } from "./peopleText";

// People (TR-1011): who is on the cell. One read (GET /api/factory/people), repeated every 30
// seconds. The list, your own picture and the sign-in status are here; the projects a person leads,
// the repos they own and adding a teammate belong to the enterprise licence and come in through the
// slots people.teamLines and people.add (src/ee/people, TR-930). Names, emails, project names and
// repos are the person's own text and marked data-user-text.

const POLL_MS = 30_000;

/** Your own row on a self-hosted cell: paste the address of a picture, or remove it (TR-1016). On
 *  Tares Cloud the picture is the one you sign in with, so there is nothing to set here. */
function PictureEdit({ p, onChanged }: { p: PersonRow; onChanged: () => void }) {
  const [open, setOpen] = React.useState(false);
  const [url, setUrl] = React.useState(p.picture ?? "");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const save = async (picture: string | null) => {
    setBusy(true); setError("");
    try {
      await api.updatePerson(p.id, { picture });
      setOpen(false); onChanged();
    } catch (err) {
      setError(err instanceof ApiError ? sentence(err.message) : "That did not go through. Try again in a moment.");
    } finally {
      setBusy(false);
    }
  };
  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)} data-testid="picture-open"
              className="cursor-pointer! border-0! bg-transparent! p-0! text-[13px]! font-normal! text-foreground! underline! decoration-border! underline-offset-[3px]">
        {p.picture ? "Change your picture" : "Add your picture"}
      </button>
    );
  }
  return (
    <form className="grid gap-2 md:grid-cols-[1fr_auto_auto] md:items-end" noValidate
          onSubmit={(e) => { e.preventDefault(); save(url.trim() || null); }}>
      <Input label="Picture address" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://"
             hint="An https address of a picture of you." error={error || undefined} data-testid="picture-url" />
      <Button type="submit" busy={busy} data-testid="picture-save">Save</Button>
      {p.picture && <Button type="button" variant="secondary" disabled={busy} onClick={() => save(null)} data-testid="picture-remove">Remove</Button>}
    </form>
  );
}

function Row({ p, canSetPicture, onChanged }: { p: PersonRow; canSetPicture: boolean; onChanged: () => void }) {
  return (
    <ListRow className="grid grid-cols-1 items-start gap-x-4 gap-y-1.5 md:grid-cols-[1fr_auto]" data-testid="person-row"
             data-person={p.id} data-state={p.state}>
      <div className="flex min-w-0 items-start gap-3">
      <Initials name={p.name} picture={p.picture} className="size-8" aria-hidden data-testid="person-avatar" />
      <div className="min-w-0">
        <div className="font-semibold [overflow-wrap:anywhere]">
          <span data-user-text data-testid="person-name">{p.name}</span>
          {p.you && <span className="font-normal text-muted-foreground"> (you)</span>}
          {p.admin && <span className="ml-2 text-[12px] font-normal text-muted-foreground" data-testid="person-admin">Admin</span>}
        </div>
        {p.email && <div className={cn(MUTED, "[overflow-wrap:anywhere]")} data-user-text data-testid="person-email">{p.email}</div>}
        {p.you && canSetPicture && <div className="mt-1"><PictureEdit p={p} onChanged={onChanged} /></div>}
      </div>
      </div>
      <div className={cn(MUTED, "flex items-center gap-1.5 md:justify-end")} data-testid="person-state">
        <StatusDot tone={p.state === "active" ? "ready" : "quiet"} className="rounded-full" />{p.state_words}
      </div>
      <Slot name="people.teamLines" p={p} />
    </ListRow>
  );
}

function Loading() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading" className="border border-border bg-card p-4" data-testid="people-loading">
      {[0, 1, 2].map((i) => <div key={i} className="grid gap-2 pb-4 pt-1"><Skeleton width="40%" /><Skeleton width="25%" /></div>)}
    </div>
  );
}

export function FactoryPeopleView({ data, onAdded, note, onPeopleChanged = () => {} }: {
  data: FactoryPeopleData; onAdded: (words: string) => void; note: string;
  /** A row changed (your picture): read the page again. */
  onPeopleChanged?: () => void;
}) {
  const { people } = data;
  return (
    <PeopleHead>
      {note && <Banner tone="note" data-testid="people-note">{note}</Banner>}
      <ListSection title="On this cell" aside={<span className="font-mono" data-testid="people-count">{people.length}</span>} data-testid="people-list">
        {people.map((p) => <Row key={p.id} p={p} canSetPicture={!data.cloud} onChanged={onPeopleChanged} />)}
      </ListSection>
      <AddSlot data={data} onAdded={onAdded} />
    </PeopleHead>
  );
}

/** Adding someone is the enterprise licence's: its form, else the licence line (never while the
 *  licensed part is still loading, so the line does not flash on a licensed cell). */
function AddSlot({ data, onAdded }: { data: FactoryPeopleData; onAdded: (words: string) => void }) {
  const pending = useEePending();
  return <Slot name="people.add" data={data} onAdded={onAdded}
               fallback={pending ? null : <LicenceNeeded what="Adding people" />} />;
}

export default function FactoryPeople() {
  const { data, error, reload } = usePagePolling<FactoryPeopleData>(factoryPeople, POLL_MS);
  const [note, setNote] = React.useState("");
  const edition = useEdition();
  const banner = error && (
    <Banner tone="error" action={<Button variant="secondary" onClick={reload}>Retry</Button>}>
      <b>People did not load.</b> <span className="text-foreground">The cell did not answer. Try again in a moment.</span>
    </Banner>
  );
  if (!data) return <PeopleHead>{banner || <Loading />}</PeopleHead>;
  return (
    <>
      {banner && <div className="mb-6">{banner}</div>}
      <FactoryPeopleView data={data} note={note} onAdded={(w) => { setNote(w); reload(); }} onPeopleChanged={() => { reload(); edition.reload?.(); }} />
    </>
  );
}

function PeopleHead({ children }: { children?: React.ReactNode }) {
  return (
    <div className="grid gap-5 md:gap-6" data-testid="factory-people">
      <div>
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[28px]!">People</h1>
        <p className="m-0! max-w-[640px] text-[15px] text-muted-foreground">Who works on this cell.</p>
      </div>
      {children}
    </div>
  );
}
