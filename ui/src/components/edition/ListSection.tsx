import { useAutoAnimate } from "@formkit/auto-animate/react";
import * as React from "react";

import { DUR, EASE_OUT } from "@/lib/motion";
import { cn } from "@/lib/utils";

// Rows that come, go or change places glide there instead of jumping (TR-1435): a poll that adds a ticket
// or re-sorts a list moves the rows the reader is looking at. auto-animate does it (FLIP: it measures
// before and after a change and animates the difference) and stands down under reduced motion. Past
// MOVING_MAX rows it is off: measuring every row on every change costs more than the glide gives.
const MOVING_MAX = 80;

export interface ListSectionProps extends Omit<React.HTMLAttributes<HTMLElement>, "title"> {
  title: string;
  /** Short text or a link at the right of the title. */
  aside?: React.ReactNode;
  /** On the paper (TR-1274): a 1px rule over the title and the rows flush with it, no box. Rows that
   *  pad with `--row-x` (ListRow) lose their inset. Off by default, so
   *  pages not yet moved keep the box. */
  paper?: boolean;
}

/** A titled box of rows, or with `paper` a titled section. Put ListRow, SkeletonRows or an EmptyState inside. */
export function ListSection({ title, aside, paper, className, children, ...props }: ListSectionProps) {
  const [rows, moving] = useAutoAnimate<HTMLDivElement>({ duration: DUR[3], easing: EASE_OUT });
  const count = React.Children.count(children);
  React.useEffect(() => { moving(count <= MOVING_MAX); }, [count, moving]);
  return (
    <section className={cn("min-w-0 [overflow-wrap:anywhere]", paper ? "[--row-x:0px]" : "border border-border bg-card", className)} {...props}>
      <div className={cn("flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1",
                         paper ? "border-t border-border pb-1 pt-3" : "border-b border-border px-[18px] py-3")}>
        <h2 className="m-0 text-[15px]">{title}</h2>
        {aside && <div className="text-xs text-muted-foreground">{aside}</div>}
      </div>
      <div ref={rows}>{children}</div>
    </section>
  );
}
