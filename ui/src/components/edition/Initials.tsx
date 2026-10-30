import * as React from "react";

import { cn } from "@/lib/utils";

/** First letters of a name ("Alex Kim" gives AK) or of an email's name part ("alex.kim@x.dev" gives AK). */
export function initialsOf(nameOrEmail: string): string {
  const base = nameOrEmail.includes("@") ? nameOrEmail.split("@")[0] : nameOrEmail;
  const parts = base.split(/[\s._-]+/).filter(Boolean);
  if (parts.length === 0) return "?";
  const letters = parts.length === 1 ? Array.from(parts[0]).slice(0, 2) : [parts[0], parts[parts.length - 1]].map((p) => Array.from(p)[0]);
  return letters.join("").toUpperCase();
}

export interface InitialsProps extends React.HTMLAttributes<HTMLSpanElement> {
  name: string;
}

export function Initials({ name, className, ...props }: InitialsProps) {
  return (
    <span
      role="img"
      aria-label={name}
      title={name}
      className={cn(
        "inline-flex size-9 shrink-0 select-none items-center justify-center border border-border bg-muted font-mono text-xs font-semibold text-foreground",
        className,
      )}
      {...props}
    >
      {initialsOf(name)}
    </span>
  );
}
