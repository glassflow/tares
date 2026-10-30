import * as React from "react";

import { Skeleton as BaseSkeleton } from "@/components/ui/skeleton";
import { ROW_CLASS } from "@/components/edition/ListRow";
import { cn } from "@/lib/utils";

export interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  /** CSS size: a number is pixels. Default: full width, 12px high. */
  width?: number | string;
  height?: number | string;
}

export function Skeleton({ width, height = 12, className, style, ...props }: SkeletonProps) {
  return <BaseSkeleton aria-hidden className={cn("bg-[var(--wash-2)]", className)} style={{ width: width ?? "100%", height, ...style }} {...props} />;
}

/** Placeholder rows for a list that is loading, the same height as ListRow. */
export function SkeletonRows({ count = 3, className, ...props }: { count?: number } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div role="status" aria-busy="true" aria-label="Loading" className={className} {...props}>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className={cn(ROW_CLASS, "flex-col flex-nowrap items-start justify-center gap-1.5")}>
          <Skeleton width="38%" height={10} />
          <Skeleton width="62%" height={8} />
        </div>
      ))}
    </div>
  );
}
