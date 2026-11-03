import * as React from "react";

import { Button, Checkbox, Dialog, Textarea } from "../../components/edition";
import type { HomeAction, HomeItem } from "./home.types";

// The panels and dialogs behind Answer, Sign off and Decline on the team home. Each is told what
// to show and calls back; the page (FactoryHome.tsx) makes the call and shows a refusal under the row.

const ANSWER_MAX = 4000;
const REASON_MAX = 1000;

/** The words a person gives an answer, and whether it stands for next time. */
export interface AnswerGiven { words: string; standing: boolean }

/** Answer, open under its row: one button per option, or a text box and Send. */
export function AnswerPanel({ item, action, busy, onSend, onCancel }: {
  item: HomeItem; action: HomeAction; busy: boolean;
  onSend: (given: AnswerGiven) => void; onCancel: () => void;
}) {
  const options = Array.isArray(action.options) ? action.options.map(String) : [];
  // Only a crew question can stand for next time; a spec question is answered once.
  const canStand = typeof action.desk === "string";
  const [text, setText] = React.useState("");
  const [standing, setStanding] = React.useState(false);
  const words = text.trim();
  return (
    <div data-testid="answer-panel" className="basis-full pl-10">
      <div className="flex max-w-[640px] flex-col gap-3 border border-border bg-muted p-3">
        {options.length > 0 ? (
          <div className="flex flex-wrap gap-2" role="group" aria-label={`Answers to ${item.title}`}>
            {options.map((o) => (
              <Button key={o} variant="secondary" className="h-8 text-[13px]!" disabled={busy}
                onClick={() => onSend({ words: o, standing })} data-user-text>
                {o}
              </Button>
            ))}
          </div>
        ) : (
          <Textarea label="Your answer" rows={3} value={text} disabled={busy} maxLength={ANSWER_MAX} data-testid="answer-text"
            onChange={(e) => setText(e.target.value)} />
        )}
        {canStand && (
          <Checkbox label="Let this answer stand for next time" checked={standing} disabled={busy} data-testid="answer-standing"
            onCheckedChange={(v) => setStanding(v === true)} />
        )}
        <div className="flex gap-2">
          {options.length === 0 && (
            <Button className="h-8 text-[13px]!" busy={busy} disabled={!words} onClick={() => onSend({ words, standing })}>Send</Button>
          )}
          <Button variant="secondary" className="h-8 text-[13px]!" disabled={busy} onClick={onCancel}>Cancel</Button>
        </div>
      </div>
    </div>
  );
}

/** Sign off: the tickets the person is vouching for, then the button. */
export function SignOffDialog({ item, repo, tickets, busy, error, onClose, onConfirm }: {
  item: HomeItem; repo: string; tickets: string[]; busy: boolean; error: string | null;
  onClose: () => void; onConfirm: () => void;
}) {
  const name = item.project?.name ?? item.title;
  return (
    <Dialog open onOpenChange={(o) => { if (!o && !busy) onClose(); }} data-testid="signoff-dialog"
      title={`Sign off ${name}?`}
      description={`You are saying the tickets in ${repo} are right. The project can go to a crew once everyone who owns a repo in it has signed off.`}
      footer={(
        <>
          <Button variant="secondary" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button busy={busy} onClick={onConfirm} data-testid="signoff-confirm">Sign off</Button>
        </>
      )}>
      <div className="flex min-w-0 flex-col gap-3">
        {tickets.length > 0 && (
          <ul className="m-0 max-h-48 list-disc overflow-auto pl-5 text-sm" data-testid="signoff-tickets">
            {tickets.map((t, i) => <li key={i} className="[overflow-wrap:anywhere]" data-user-text>{t}</li>)}
          </ul>
        )}
        {error && <p role="alert" data-testid="dialog-error" className="m-0 text-[13px] text-[color:var(--err)]" data-words-exempt>{error}</p>}
      </div>
    </Dialog>
  );
}

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
