import * as React from "react";

import { cn } from "@/lib/utils";

export interface ListSectionProps extends Omit<React.HTMLAttributes<HTMLElement>, "title"> {
  title: string;
  /** Short text or a link at the right of the title. */
  aside?: React.ReactNode;
}

/** A titled box of rows. Put ListRow, SkeletonRows or an EmptyState inside. */
export function ListSection({ title, aside, className, children, ...props }: ListSectionProps) {
  return (
    <section className={cn("min-w-0 border border-border bg-card [overflow-wrap:anywhere]", className)} {...props}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1 border-b border-border px-[18px] py-3">
        <h2 className="m-0! text-[15px]!">{title}</h2>
        {aside && <div className="text-xs text-muted-foreground">{aside}</div>}
      </div>
      <div>{children}</div>
    </section>
  );
}
