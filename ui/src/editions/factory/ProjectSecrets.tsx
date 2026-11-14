import * as React from "react";

import { ApiError, request } from "../../api";
import { Button, Dialog, Input } from "../../components/edition";
import { cn } from "../../lib/utils";

// A project's secrets (TR-775): the names, who set each and when, and when a builder last used one, as
// the project page's one read gives them (`secrets`, null for someone not on the project, who sees no
// section). No read gives a value back, so this list is all anyone sees; a value goes in once, from Add
// or Replace (PUT /api/projects/{id}/secrets/{name}, which answers with the new list), and builders fetch
// it themselves when a command needs it.

export interface SecretRow {
  name: string;
  set_by: string | null;
  set_at: string | null;
  used_at: string | null;
  used_by: string | null;
  readable: boolean;
}
export interface SecretsDoc { enabled: boolean; off: string; secrets: SecretRow[] }

const NAME_RE = /^[A-Z_][A-Z0-9_]{0,63}$/;
const MUTED = "text-xs text-muted-foreground";

function ago(at: string | null): string {
  if (!at) return "";
  const s = Math.max(0, (Date.now() - new Date(at).getTime()) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} minutes ago`;
  if (s < 86400 * 1.5) return `${Math.round(s / 3600)} hours ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function url(project: string, name?: string): string {
  const base = `/api/projects/${encodeURIComponent(project)}/secrets`;
  return name ? `${base}/${encodeURIComponent(name)}` : base;
}

function SecretDialog({ project, name, onClose, onSaved }: {
  project: string; name: string | null; onClose: () => void; onSaved: (d: SecretsDoc) => void;
}) {
  const [n, setN] = React.useState(name ?? "");
  const [value, setValue] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const nameBad = n !== "" && !NAME_RE.test(n);
  const save = async () => {
    setBusy(true);
    try {
      onSaved(await request<SecretsDoc>(url(project, n), { method: "PUT", body: JSON.stringify({ value }) }));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Could not reach the cell. Try again in a moment.");
      setBusy(false);
    }
  };
  const ready = NAME_RE.test(n) && value.trim() !== "" && !busy;
  return (
    <Dialog open onOpenChange={(o) => { if (!o && !busy) onClose(); }} data-testid="secret-dialog"
            title={name ? `Replace ${name}` : "Add a secret"}
            description="Once saved, nobody can see the value again, you included. Builders of this project get it when a command needs it."
            footer={<>
              <Button variant="secondary" disabled={busy} onClick={onClose} data-testid="secret-cancel">Cancel</Button>
              <Button busy={busy} disabled={!ready} onClick={() => void save()} data-testid="secret-save">Save</Button>
            </>}>
      <form className="grid gap-3" onSubmit={(e) => { e.preventDefault(); if (ready) void save(); }}>
        {!name && (
          <Input label="Name" value={n} autoComplete="off" spellCheck={false} placeholder="CELL_READ_KEY"
                 hint="Capital letters, digits and _. Builders read it as $NAME."
                 error={nameBad ? "Use capital letters, digits and _, starting with a letter or _." : undefined}
                 onChange={(e) => setN(e.target.value.toUpperCase())} data-testid="secret-name" />
        )}
        <Input label="Value" type="password" value={value} autoComplete="new-password" spellCheck={false}
               error={error || undefined} onChange={(e) => { setValue(e.target.value); if (error) setError(""); }}
               data-testid="secret-value" />
      </form>
    </Dialog>
  );
}

export function ProjectSecrets({ project, initial }: { project: string; initial: SecretsDoc }) {
  const [doc, setDoc] = React.useState<SecretsDoc>(initial);
  const [editing, setEditing] = React.useState<{ name: string | null } | null>(null);
  const [removing, setRemoving] = React.useState("");
  const [error, setError] = React.useState("");
  React.useEffect(() => { setDoc(initial); }, [initial]);   // the page's next read

  const remove = async (name: string) => {
    setRemoving(name);
    try { setDoc(await request<SecretsDoc>(url(project, name), { method: "DELETE" })); setError(""); }
    catch (err) { setError(err instanceof ApiError ? err.message : "Could not reach the cell. Try again in a moment."); }
    setRemoving("");
  };

  return (
    <section id="secrets" aria-label="Secrets" className="min-w-0 scroll-mt-4" data-testid="project-secrets">
      <div className="mb-2 border-t border-border pt-3">
        <h2 className="m-0! text-base!">Secrets</h2>
      </div>
      <div className="min-w-0">
        {!doc.enabled && <div className="text-[13px]" data-testid="secrets-off">{doc.off}</div>}
        {doc.enabled && (
          <>
            {doc.secrets.length === 0 ? (
              <div className="py-1 text-[13px] text-muted-foreground" data-testid="secrets-empty">
                No secrets yet. Add a key the builders need, such as another cell's read-only key.
              </div>
            ) : doc.secrets.map((s) => (
              <div key={s.name} className="flex flex-wrap items-start justify-between gap-x-3 gap-y-1 border-b border-border py-2 last:border-b-0"
                   data-testid="secret-row">
                <div className="min-w-0">
                  <div className="font-mono text-[13px]" data-user-text>{s.name}</div>
                  <div className={MUTED}>
                    <span data-user-text>{s.set_by ? `Set by ${s.set_by}` : "Set"}</span> {ago(s.set_at)}
                    {s.used_at ? <> · <span data-user-text>last used{s.used_by ? ` by ${s.used_by}` : ""}</span> {ago(s.used_at)}</> : " · not used yet"}
                  </div>
                  {!s.readable && <div className="text-xs text-[color:var(--err)]">This value can't be read any more. Replace it.</div>}
                </div>
                <div className="flex gap-2">
                  <Button variant="secondary" onClick={() => setEditing({ name: s.name })} data-testid="secret-replace">Replace</Button>
                  <Button variant="secondary" busy={removing === s.name} onClick={() => void remove(s.name)} data-testid="secret-remove">Remove</Button>
                </div>
              </div>
            ))}
            <div className="mt-3">
              <Button variant="secondary" onClick={() => setEditing({ name: null })} data-testid="secret-add">Add a secret</Button>
            </div>
            <div className={cn(MUTED, "mt-3")}>Builders use them with</div>
            <code className="mt-1 block [overflow-wrap:anywhere] border border-border bg-background px-2 py-1 text-[13px]"
                  data-words-exempt>tares factory with-secrets -- &lt;command&gt;</code>
          </>
        )}
        {error && <div role="alert" className="mt-2 text-[13px] text-[color:var(--err)]">{error}</div>}
      </div>
      {editing && (
        <SecretDialog project={project} name={editing.name} onClose={() => setEditing(null)}
                      onSaved={(d) => { setDoc(d); setEditing(null); setError(""); }} />
      )}
    </section>
  );
}
