import * as React from "react";

import { cn } from "@/lib/utils";

export interface EmptyStateProps extends Omit<React.HTMLAttributes<HTMLDivElement>, "title"> {
  title: string;
  /** One sentence: what is missing and what to do. */
  children?: React.ReactNode;
  /** Usually a Button. */
  action?: React.ReactNode;
}

export function EmptyState({ title, children, action, className, ...props }: EmptyStateProps) {
  return (
    <div className={cn("grid justify-items-center gap-2 px-6 py-10 text-center", className)} {...props}>
      <h3 className="m-0! text-[15px]!">{title}</h3>
      {children && <p className="m-0 max-w-md text-sm text-muted-foreground">{children}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}
