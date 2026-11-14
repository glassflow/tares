import * as React from "react";

import { cn } from "@/lib/utils";

export interface StatProps extends React.HTMLAttributes<HTMLDivElement> {
  label: string;
  value: React.ReactNode;
  /** One short line under the value. */
  note?: string;
}

/** A headline numeral: a mono label, the value in Doto (the dot-matrix face Rius uses; .impeccable.md, Type), an
 *  optional note in Inter. One or two per page; a number in a table or a row stays in the text face. */
export function Stat({ label, value, note, className, ...props }: StatProps) {
  return (
    <div className={cn("grid min-w-0 content-start gap-1 [overflow-wrap:anywhere]", className)} {...props}>
      <div className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">{label}</div>
      <div className="font-pixel text-[26px] font-black leading-none tabular-nums [font-variant-ligatures:none]">{value}</div>
      {note && <div className="text-xs text-muted-foreground">{note}</div>}
    </div>
  );
}
