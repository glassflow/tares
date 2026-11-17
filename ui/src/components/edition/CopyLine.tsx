import { useEffect, useRef, useState } from "react";

import { copyText } from "../../editions/copyText";
import { cn } from "../../lib/utils";
import { Button } from "./Button";

export interface CopyLineProps {
  /** The literal to paste somewhere else: an address, a command, a key. Shown as is. */
  text: string;
  /** What it is, in a word or two ("address", "commands"): the button reads "Copy <what>". */
  what: string;
  /** Keep the text's own line breaks (a command of several lines). */
  multiline?: boolean;
  className?: string;
  "data-testid"?: string;
}

/** A literal with its copy button. The text stays on screen and selectable, so when the clipboard is
 *  refused (a page served over plain http has none) the line says so and it can be copied by hand.
 *  The text is the system's, not page words, so the words check skips it. */
export function CopyLine({ text, what, multiline, className, ...props }: CopyLineProps) {
  const [copy, setCopy] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  const doCopy = async () => {
    clearTimeout(timer.current);
    try {
      await copyText(text);
      setCopy("copied");
      timer.current = setTimeout(() => setCopy("idle"), 1500);
    } catch { setCopy("failed"); }
  };
  return (
    <div className={cn("grid gap-1.5", className)}>
      <div className="flex min-w-0 flex-wrap items-start gap-2">
        <code data-words-exempt {...props}
              className={cn("min-w-0 basis-full border border-border bg-background px-3 py-2 font-mono text-[13px] [overflow-wrap:anywhere] sm:basis-0 sm:flex-1",
                            multiline && "whitespace-pre-wrap")}>
          {text}
        </code>
        <Button variant="secondary" onClick={() => void doCopy()}>{copy === "copied" ? "Copied" : `Copy ${what}`}</Button>
      </div>
      {copy === "failed" && <p className="m-0 text-[13px] text-err" role="alert">Could not copy. Select the {what} and copy it by hand.</p>}
    </div>
  );
}
