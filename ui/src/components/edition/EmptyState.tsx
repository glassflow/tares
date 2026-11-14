import * as React from "react";

import { cn } from "@/lib/utils";

export interface EmptyStateProps extends Omit<React.HTMLAttributes<HTMLElement>, "title"> {
  /** With a title, the empty state is a block: a page or a list with nothing in it. Without one it is a
   *  line: a section that has nothing yet, said in one sentence where its rows would be. */
  title?: string;
  /** One sentence: what is missing and what makes it appear. */
  children?: React.ReactNode;
  /** Usually a Button. */
  action?: React.ReactNode;
}

/** The one empty state (.impeccable.md, States): a sentence in the voice, never an illustration. */
export function EmptyState({ title, children, action, className, ...props }: EmptyStateProps) {
  if (!title) {
    return (
      <div data-empty="line" className={cn("min-w-0 py-2 text-sm text-muted-foreground [overflow-wrap:anywhere]", className)} {...props}>
        {children}
        {action && <div className="mt-3">{action}</div>}
      </div>
    );
  }
  return (
    <div data-empty="block" className={cn("grid min-w-0 [overflow-wrap:anywhere] justify-items-center gap-2 px-6 py-10 text-center", className)} {...props}>
      <h3 className="m-0! text-[15px]!">{title}</h3>
      {children && <p className="m-0 max-w-md text-sm text-muted-foreground">{children}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
