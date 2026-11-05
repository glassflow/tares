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
  /** An https address of the person's picture (TR-1016). Shown round in place of the initials;
   *  the initials come back when there is none or it does not load. */
  picture?: string | null;
}

const BOX = "inline-flex size-9 shrink-0 select-none items-center justify-center font-mono text-xs font-semibold text-foreground";

export function Initials({ name, picture, className, ...props }: InitialsProps) {
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => setFailed(false), [picture]);
  const src = picture && /^https:\/\//i.test(picture) && !failed ? picture : null;
  if (src) {
    return (
      <span role="img" aria-label={name} title={name} data-picture="on"
            className={cn(BOX, "overflow-hidden rounded-full bg-muted", className)} {...props}>
        {/* no-referrer: Google's avatar addresses refuse a request that names another site */}
        <img src={src} alt="" referrerPolicy="no-referrer" loading="lazy" draggable={false}
             onError={() => setFailed(true)} className="size-full object-cover" />
      </span>
    );
  }
  return (
    <span
      role="img"
      aria-label={name}
      title={name}
      data-picture="off"
      className={cn(BOX, "border border-border bg-muted", className)}
      {...props}
    >
      {initialsOf(name)}
    </span>
  );
}
