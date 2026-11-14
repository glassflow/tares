import * as React from "react";

import { ROW_CLASS } from "@/components/edition/ListRow";
import { cn } from "@/lib/utils";

export interface SkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  /** CSS size: a number is pixels. Default: full width, 12px high. */
  width?: number | string;
  height?: number | string;
}

/** One bar standing in for text that is loading. It pulses unless the reader asked for less motion. */
export function Skeleton({ width, height = 12, className, style, ...props }: SkeletonProps) {
  return (
    <div
      aria-hidden
      className={cn("animate-pulse bg-[var(--wash-2)] motion-reduce:animate-none", className)}
      style={{ width: width ?? "100%", height, ...style }}
      {...props}
    />
  );
}

export interface SkeletonLinesProps extends React.HTMLAttributes<HTMLDivElement> {
  /** One bar per entry, as wide as the line it stands in for: ["30%", "75%", "50%"]. */
  widths: (number | string)[];
  height?: number | string;
}

/** A block of lines that is loading: a title and a sentence or two. */
export function SkeletonLines({ widths, height, className, ...props }: SkeletonLinesProps) {
  return (
    <div className={cn("grid gap-2", className)} {...props}>
      {widths.map((w, i) => <Skeleton key={i} width={w} height={height} />)}
    </div>
  );
}

export interface LoadingProps extends React.HTMLAttributes<HTMLDivElement> {
  /** What a screen reader hears. Default "Loading". */
  label?: string;
}

/** The one loading state. It tells a screen reader that the area is busy; the page puts Skeletons inside,
 *  arranged as its loaded content will be, so nothing jumps when the data arrives (.impeccable.md, States). */
export function Loading({ label = "Loading", children, ...props }: LoadingProps) {
  return (
    <div role="status" aria-busy="true" aria-label={label} {...props}>
      {children}
    </div>
  );
}

export interface LoadingBlocksProps extends LoadingProps {
  /** How many blocks. Default 3. */
  count?: number;
  /** The lines of one block, as SkeletonLines takes them. Default a title and two sentences. */
  widths?: (number | string)[];
}

/** A list page that is loading: a bordered box of blocks, one per item to come. */
export function LoadingBlocks({ count = 3, widths = ["30%", "75%", "50%"], className, ...props }: LoadingBlocksProps) {
  return (
    <Loading className={cn("border border-border bg-card p-4", className)} {...props}>
      {Array.from({ length: count }, (_, i) => <SkeletonLines key={i} widths={widths} className="pb-4 pt-1 last:pb-1" />)}
    </Loading>
  );
}

/** Placeholder rows for a list that is loading, the same height as ListRow. */
export function SkeletonRows({ count = 3, className, ...props }: { count?: number } & React.HTMLAttributes<HTMLDivElement>) {
  return (
    <Loading className={className} {...props}>
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className={cn(ROW_CLASS, "flex-col flex-nowrap items-start justify-center gap-1.5")}>
          <Skeleton width="38%" height={10} />
          <Skeleton width="62%" height={8} />
        </div>
      ))}
    </Loading>
  );
}
