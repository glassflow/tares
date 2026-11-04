import * as React from "react";
import { Link } from "react-router-dom";

import { cn } from "@/lib/utils";

/** Shared with SkeletonRows so a loading row is exactly as tall as a real one. */
export const ROW_CLASS =
  "flex min-h-[58px] min-w-0 [overflow-wrap:anywhere] flex-wrap items-center gap-x-4 gap-y-1 border-t border-border px-[18px] py-4 first:border-t-0";

export interface ListRowProps extends React.HTMLAttributes<HTMLElement> {
  /** `a` renders a plain link (give it `href`); `to` renders a router link. Default: a div. */
  as?: "div" | "a";
  to?: string;
  href?: string;
}

// The console's bare `a` rule underlines links, so the row's link form turns that off.
const LINK_CLASS =
  "no-underline! hover:bg-[var(--row-hover)] focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-[color:var(--accent)]";

export function ListRow({ as = "div", to, href, className, ...props }: ListRowProps) {
  if (to !== undefined) return <Link to={to} className={cn(ROW_CLASS, LINK_CLASS, className)} {...props} />;
  if (as === "a") return <a href={href} className={cn(ROW_CLASS, LINK_CLASS, className)} {...props} />;
  return <div className={cn(ROW_CLASS, "hover:bg-[var(--row-hover)]", className)} {...props} />;
}
