// Tares enterprise licence, see ee/LICENSE
import * as React from "react";

import { Button, Dialog, Textarea } from "@/components/edition";
import type { HomeAction, HomeItem, HomeTeamActionsProps } from "./home.types";
import { answerQuestion, consentGrant, declineGrant } from "./api";

// What the team's buttons on the home do: agree to a grant, decline one (with the dialog that asks
// why), and send the answer to a question a project's spec asked. The page owns the rows and the
// call that shows work, a refusal and the toast; this component is handed `run` and gives the page
// its handler through `onHandler`.

const REASON_MAX = 1000;

/** Decline: an optional reason the giver can read. */
export function DeclineDialog({ item, busy, error, onClose, onConfirm }: {
  item: HomeItem; busy: boolean; error: string | null; onClose: () => void; onConfirm: (reason: string) => void;
}) {
  const [text, setText] = React.useState("");
  return (
    <Dialog open onOpenChange={(o) => { if (!o && !busy) onClose(); }} data-testid="decline-dialog"
      title="Decline this grant?" description="It ends as a revoked one, and the person who gave it can see that you said no."
      footer={(
        <>
          <Button variant="secondary" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button busy={busy} onClick={() => onConfirm(text.trim())} data-testid="decline-confirm">Decline</Button>
        </>
      )}>
      <div className="flex min-w-0 flex-col gap-3">
        <p className="m-0 text-sm [overflow-wrap:anywhere]" data-user-text>{item.title}</p>
        <Textarea label="Why (optional)" rows={3} value={text} disabled={busy} maxLength={REASON_MAX} data-testid="decline-reason"
          onChange={(e) => setText(e.target.value)} />
        {error && <p role="alert" data-testid="dialog-error" className="m-0 text-[13px] text-[color:var(--err)]" data-words-exempt>{error}</p>}
      </div>
    </Dialog>
  );
}

export default function TeamActions({ run, clearError, rows, onHandler, onDialog }: HomeTeamActionsProps) {
  const [declining, setDeclining] = React.useState<{ item: HomeItem; grant: string } | null>(null);
  // the handler is given once; it reads the page's latest functions through this ref
  const latest = React.useRef({ run, clearError });
  latest.current = { run, clearError };

  React.useEffect(() => {
    onHandler({
      act: (a: HomeAction, item: HomeItem) => {
        if (typeof a.grant !== "string") return a.do === "agree" || a.do === "decline";
        const grant = a.grant;
        if (a.do === "agree") {
          void latest.current.run(item, "agree", () => consentGrant(grant),
            "Agreed. The grant starts once everyone it waits for has agreed.", () => {});
          return true;
        }
        if (a.do === "decline") {
          latest.current.clearError(item.id);
          setDeclining({ item, grant });
          return true;
        }
        return false;
      },
      answer: (project, question, words) => answerQuestion(project, question, words),
    });
    return () => onHandler(null);
  }, [onHandler]);
  React.useEffect(() => { onDialog(declining?.item.id ?? null); }, [declining, onDialog]);

  if (!declining) return null;
  const { item, grant } = declining;
  return (
    <DeclineDialog key={item.id} item={item} busy={rows[item.id]?.busy === "decline"} error={rows[item.id]?.error ?? null}
      onClose={() => { latest.current.clearError(item.id); setDeclining(null); }}
      onConfirm={(why) => void latest.current.run(item, "decline", () => declineGrant(grant, why),
        "Declined. The grant is revoked.", () => setDeclining(null))} />
  );
}
