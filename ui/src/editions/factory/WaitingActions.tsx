import * as React from "react";

import { Button, Checkbox, Textarea } from "../../components/edition";
import type { HomeAction, HomeItem } from "./home.types";

// The panel behind Answer on the home. It is told what to show and calls back; the page
// (FactoryHome.tsx) makes the call and shows a refusal under the row.

const ANSWER_MAX = 4000;

/** The words a person gives an answer, the option they picked (if they clicked one), and whether it stands for next time. */
/** `own`: the words were typed in the text box, so the desk keeps them as typed, never as an option they start. */
export interface AnswerGiven { words: string; choice?: string; standing: boolean; own?: boolean }

const LETTERS = "ABCDEFGH";
/** A click on an option. A crew question reads a one-letter reply as a pick by position, so a click sends the position and
 *  the option as the choice; a spec question takes the option's own words. */
export function optionAnswer(options: string[], index: number, desk: boolean): { words: string; choice?: string } {
  const o = options[index];
  return desk && index < LETTERS.length ? { words: LETTERS[index], choice: o } : { words: o, choice: o };
}

/** Answer, open under its row: one button per option, and a text box and Send under them for an
 *  answer none of them gives (TR-1452). */
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
        {options.length > 0 && (
          <div className="flex flex-wrap gap-2" role="group" aria-label={`Answers to ${item.title}`}>
            {options.map((o, i) => (
              <Button key={i} variant="secondary" className="h-8 text-[13px]" disabled={busy}
                onClick={() => onSend({ ...optionAnswer(options, i, canStand), standing })} data-user-text>
                {o}
              </Button>
            ))}
          </div>
        )}
        {/* none of the options fits: the desk takes words for any question (TR-1452) */}
        <Textarea label={options.length > 0 ? "Or in your own words" : "Your answer"} rows={options.length > 0 ? 2 : 3}
          value={text} disabled={busy} maxLength={ANSWER_MAX} data-testid="answer-text" data-user-text
          onChange={(e) => setText(e.target.value)} />
        {canStand && (
          <Checkbox label="Let this answer stand for next time" checked={standing} disabled={busy} data-testid="answer-standing"
            onCheckedChange={(v) => setStanding(v === true)} />
        )}
        <div className="flex gap-2">
          {/* the desk records an option for every question that has them (TR-1018): an answer none of
              them gives is its own choice, kept as typed (`own`), so "Red" never becomes "Redis" */}
          <Button className="h-8 text-[13px]" busy={busy} disabled={!words}
            onClick={() => onSend(options.length > 0 ? { words, choice: words, standing, own: true } : { words, standing })}>Send</Button>
          <Button variant="secondary" className="h-8 text-[13px]" disabled={busy} onClick={onCancel}>Cancel</Button>
        </div>
      </div>
    </div>
  );
}
