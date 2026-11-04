import * as React from "react";

import { cn } from "@/lib/utils";

export type BannerTone = "note" | "warn" | "error";

const TONES: Record<BannerTone, string> = {
  note: "border-border bg-card text-foreground",
  warn: "border-[color:var(--warn)] bg-[var(--warn-soft)] text-foreground",
  error: "border-[color:var(--danger-border)] bg-[var(--err-soft)] text-[color:var(--err)]",
};

export interface BannerProps extends React.HTMLAttributes<HTMLDivElement> {
  tone?: BannerTone;
  /** A Button or link at the right. */
  action?: React.ReactNode;
}

/** One sentence. An error is announced at once; a note or warning waits its turn. */
export function Banner({ tone = "note", action, className, children, ...props }: BannerProps) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn("min-w-0 [overflow-wrap:anywhere] flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border px-4 py-2.5 text-sm", TONES[tone], className)}
      {...props}
    >
      <span className="min-w-0">{children}</span>
      {action}
    </div>
  );
}
