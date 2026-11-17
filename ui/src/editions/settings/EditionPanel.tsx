import { useEffect, useState } from "react";

import { api } from "../../api";
import type { Edition, EditionSettings } from "../../types";
import { Banner, Button, Dialog, ListRow, Panel, SkeletonRows, StatusLabel } from "../../components/edition";

// The cell's edition (TR-724), on the edition kit (TR-1392): which set of screens the console shows.
// Anyone can see it; only an admin can switch it, after a dialog that says what stays. The data,
// projects and crew stay as they are; the console reloads after. The platform Settings page and
// Settings, Advanced on a factory or AI SRE cell show this; Factory Settings and AI SRE Settings
// have their own Edition group.

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";

// What each edition shows, in words every edition may use: this panel is shared by all three.
const BLURBS: Record<Edition, string> = {
  factory: "Projects, tickets and reviews, with a crew of Claude Code sessions building them.",
  sre: "Services, incidents and runbooks, watched by agents that tell you what needs you.",
  platform: "Every building block on its own page: the data coming in, the rules, the agents and the projects.",
};

const messageOf = (e: unknown) => String((e as Error)?.message ?? e);
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

export function EditionPanel() {
  const [admin, setAdmin] = useState<boolean>();
  const [now, setNow] = useState<string>();   // whoami's name for the edition, for anyone
  const [st, setSt] = useState<EditionSettings>();   // the choices, for an admin
  const [target, setTarget] = useState<Edition>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  useEffect(() => {
    api.whoami()
      .then((w) => {
        const a = w.scopes.includes("admin");
        setAdmin(a);
        setNow(w.edition_name);
        return a ? api.edition().then(setSt) : undefined;
      })
      .catch((e) => setErr(sentence(messageOf(e))));
  }, []);

  const nameOf = (e: Edition) => st?.choices.find((c) => c.id === e)?.name ?? e;
  const doSwitch = async (to: Edition) => {
    setBusy(true); setErr(undefined);
    try { await api.setEdition(to); window.location.reload(); }
    catch (e) { setErr(sentence(messageOf(e))); setTarget(undefined); setBusy(false); }
  };

  const loading = admin === undefined || (admin && !st);
  return (
    <Panel title="Edition" data-testid="edition">
      <div className="grid gap-4">
        <p className={TEXT}>Which screens this workspace shows. Your projects, data and crew are the same in every edition.</p>
        {err && <Banner tone="error" data-testid="edition-error">{err}</Banner>}
        {loading ? (
          !err && <SkeletonRows count={3} />
        ) : admin && st ? (
          <div className="-mt-2 [--row-x:0px]">
            {st.choices.map((c) => (
              <ListRow key={c.id} className="hover:bg-transparent" data-testid="edition-row" data-id={c.id}>
                <div className="grid min-w-0 flex-[1_1_320px] gap-1">
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <span className="text-sm font-semibold">{c.name}</span>
                    {c.id === st.edition && <StatusLabel tone="ready" data-testid="edition-now">Shown now</StatusLabel>}
                  </div>
                  <p className={TEXT}>{BLURBS[c.id]}</p>
                </div>
                {c.id !== st.edition && (
                  <Button variant="secondary" data-testid={`edition-to-${c.id}`} disabled={busy}
                          onClick={() => { setErr(undefined); setTarget(c.id); }}>
                    Switch to {c.name}
                  </Button>
                )}
              </ListRow>
            ))}
          </div>
        ) : (
          now && (
            <p className="m-0 text-sm" data-testid="edition-readonly">
              Now <b>{now}</b>. Only an admin can switch the edition.
            </p>
          )
        )}
      </div>

      {target && st && (
        <Dialog open onOpenChange={(o) => { if (!o && !busy) setTarget(undefined); }} data-testid="edition-dialog"
                title={`Switch to ${nameOf(target)}?`}
                description={`Your projects, data and crew stay as they are. The screens change from ${nameOf(st.edition)} to ${nameOf(target)}, and the console reloads.`}
                footer={<>
                  <Button variant="secondary" disabled={busy} data-testid="edition-cancel" onClick={() => setTarget(undefined)}>Cancel</Button>
                  <Button busy={busy} data-testid="edition-confirm" onClick={() => void doSwitch(target)}>Switch</Button>
                </>} />
      )}
    </Panel>
  );
}
