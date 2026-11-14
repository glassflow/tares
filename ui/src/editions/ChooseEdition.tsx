import * as React from "react";
import { Navigate, useNavigate } from "react-router-dom";

import { api } from "../api";
import { Banner, Button, Loading, Skeleton } from "../components/edition";
import { cn } from "../lib/utils";
import type { Edition } from "../types";
import { useEdition } from "./edition";

// TR-755: the first screen of a cell that has no edition yet. It stands alone, with no menu,
// because nothing else is reachable until a choice is made. One read (whoami, through useEdition),
// one write (PUT /api/settings/edition); no polling. A cell that already has an edition sends the
// address to Home. The words are in WORDS.md; the terminal asks the same question.

type Choice = { id: Exclude<Edition, "platform">; name: string; line: string };

const CHOICES: Choice[] = [
  { id: "factory", name: "Software Factory", line: "A crew of Claude Code sessions builds your projects; you review and decide." },
  { id: "sre", name: "AI SRE", line: "Agents watch your services and tell you what broke and why." },
];

const QUESTION = "Which Tares is this?";
const LEAD = "Pick what you want to use it for. You can change this later in Settings.";

// The console's own `button` rules are unlayered and beat plain utilities, so what they also set is
// marked important.
const CARD = "flex! w-full cursor-pointer flex-col items-start gap-1 border-border! bg-card! px-4! py-4! text-left text-foreground! font-normal! disabled:cursor-default disabled:opacity-60";
const CARD_ON = "border-2! border-foreground! bg-[var(--accent-soft)]! px-[15px]! py-[15px]!";

function Frame({ lead, children }: { lead: string; children?: React.ReactNode }) {
  return (
    <div className="flex min-w-0 flex-1 justify-center px-4 py-10 md:py-16">
      <main className="w-full max-w-[720px]" data-testid="choose-edition">
        <h1 className="m-0! mb-1.5! text-[22px]! md:text-[26px]!">{QUESTION}</h1>
        <p className="m-0! mb-5! text-muted-foreground" data-testid="choose-lead">{lead}</p>
        {children}
      </main>
    </div>
  );
}

function Radio({ choice, on, disabled, pick, onKey }: {
  choice: Choice; on: boolean; disabled: boolean; pick: () => void; onKey: (e: React.KeyboardEvent) => void;
}) {
  return (
    <button
      type="button" role="radio" aria-checked={on} disabled={disabled} tabIndex={on ? 0 : -1}
      data-testid={`choice-${choice.id}`} onClick={pick} onKeyDown={onKey}
      className={cn(CARD, on && CARD_ON)}
    >
      <span className="flex items-center gap-2 text-base font-bold">
        <span
          aria-hidden
          className={cn("inline-block size-4 shrink-0 rounded-full border-[1.5px] border-foreground", on && "bg-foreground shadow-[inset_0_0_0_3px_var(--card)]")}
        />
        {choice.name}
      </span>
      <span className="text-muted-foreground">{choice.line}</span>
    </button>
  );
}

export default function ChooseEdition() {
  const { edition, needed, admin, reload } = useEdition();
  const navigate = useNavigate();
  const [picked, setPicked] = React.useState<Choice["id"]>("factory");
  const [saving, setSaving] = React.useState(false);
  const [failed, setFailed] = React.useState(false);
  // the choice was saved but the cell did not answer when the page asked again
  const [stale, setStale] = React.useState(false);

  if (edition === undefined) {
    return (
      <Frame lead="Checking">
        <Loading className="grid gap-3 md:grid-cols-2" data-testid="choose-loading">
          <Skeleton height={84} />
          <Skeleton height={84} />
        </Loading>
      </Frame>
    );
  }
  // a cell that already has an edition has nothing to choose
  if (!needed) return <Navigate to="/" replace />;
  if (!admin) {
    return (
      <Frame lead="An admin of this Tares chooses what it is for first.">
        <p className="m-0! text-[13px] text-muted-foreground" data-testid="choose-wait">
          Ask an admin to open this Tares and choose. You will see it here once they have.
        </p>
      </Frame>
    );
  }

  const save = async () => {
    setSaving(true);
    setFailed(false);
    try {
      await api.setEdition(picked);
    } catch {
      setFailed(true);
      setSaving(false);
      return;
    }
    if (!(await reload())) {
      setStale(true);
      setSaving(false);
      return;
    }
    navigate("/", { replace: true });
  };

  const move = (to: number) => {
    const next = CHOICES[(to + CHOICES.length) % CHOICES.length];
    setPicked(next.id);
    document.querySelector<HTMLElement>(`[data-testid="choice-${next.id}"]`)?.focus();
  };
  const onKey = (i: number) => (e: React.KeyboardEvent) => {
    if (e.key === "ArrowRight" || e.key === "ArrowDown") { e.preventDefault(); move(i + 1); }
    if (e.key === "ArrowLeft" || e.key === "ArrowUp") { e.preventDefault(); move(i - 1); }
  };

  return (
    <Frame lead={LEAD}>
      <div role="radiogroup" aria-label={QUESTION} className="grid gap-3 md:grid-cols-2">
        {CHOICES.map((c, i) => (
          <Radio key={c.id} choice={c} on={picked === c.id} disabled={saving} pick={() => setPicked(c.id)} onKey={onKey(i)} />
        ))}
      </div>
      {stale && (
        <Banner
          tone="warn" className="mt-4" data-testid="choose-stale"
          action={<Button variant="secondary" onClick={() => window.location.reload()}>Reload</Button>}
        >
          <b>Your choice was saved.</b> The cell did not answer when we asked again. Reload to continue.
        </Banner>
      )}
      {failed && (
        <Banner
          tone="error" className="mt-4" data-testid="choose-error"
          action={<Button variant="secondary" onClick={save}>Retry</Button>}
        >
          <b>Your choice was not saved.</b> The cell did not answer.
        </Banner>
      )}
      {!failed && !stale && (
        <div className="mt-5">
          <Button busy={saving} onClick={save} className="w-full sm:w-auto" data-testid="choose-continue">
            {saving ? "Saving" : "Continue"}
          </Button>
        </div>
      )}
    </Frame>
  );
}
