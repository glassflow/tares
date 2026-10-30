import * as React from "react";

import { cn } from "@/lib/utils";

export interface PanelProps extends Omit<React.HTMLAttributes<HTMLElement>, "title"> {
  title?: string;
  /** A button or link at the right of the title. */
  action?: React.ReactNode;
}

export function Panel({ title, action, className, children, ...props }: PanelProps) {
  return (
    <section className={cn("border border-border bg-card", className)} {...props}>
      {(title || action) && (
        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-border px-[18px] py-3">
          {title ? <h3 className="m-0! text-[15px]!">{title}</h3> : <span />}
          {action}
        </div>
      )}
      <div className="p-[18px]">{children}</div>
    </section>
  );
}
