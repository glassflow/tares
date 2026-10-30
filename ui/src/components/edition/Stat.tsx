import * as React from "react";

import { cn } from "@/lib/utils";

export interface StatProps extends React.HTMLAttributes<HTMLDivElement> {
  label: string;
  value: React.ReactNode;
  /** One short line under the value. */
  note?: string;
}

export function Stat({ label, value, note, className, ...props }: StatProps) {
  return (
    <div className={cn("grid content-start gap-1", className)} {...props}>
      <div className="font-mono text-[11px] uppercase tracking-[0.08em] text-muted-foreground">{label}</div>
      <div className="text-[22px] font-semibold leading-tight">{value}</div>
      {note && <div className="text-xs text-muted-foreground">{note}</div>}
    </div>
  );
}
