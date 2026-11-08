import type { CSSProperties } from "react";

import { Banner } from "@/components/edition";
import { cn } from "@/lib/utils";

/** Where the enterprise licence is explained. The daemon sends the same address in its 402 answer. */
export const ENTERPRISE_URL = "https://docs.glassflow.ai/tares/enterprise";

/** The one plain sentence for a team action the cell has no licence for: "Adding people needs an
 *  enterprise licence." */
export function licenceSentence(what = "That"): string {
  return `${what} needs an enterprise licence.`;
}

const LINK = "text-foreground! underline! decoration-border! underline-offset-[3px]";

/** Shown where a team action would be, when the cell answers it with `licence_required`. `inline`
 *  is one muted line under the control; `page` is a boxed note for a page that is only for teams. */
export default function LicenceNeeded({ what, variant = "inline", url = ENTERPRISE_URL, className, style }: {
  what?: string;
  variant?: "inline" | "page";
  url?: string;
  className?: string;
  style?: CSSProperties;
}) {
  const link = (
    <a href={url} target="_blank" rel="noreferrer" className={LINK}>About the enterprise licence</a>
  );
  if (variant === "page") {
    return (
      <Banner tone="note" data-testid="licence-needed" className={cn("items-start", className)} style={style}>
        <strong className="block font-medium">Teams need an enterprise licence</strong>
        <span className="block text-muted-foreground">This page is for cells with more than one person.</span>
        <span className="mt-1 block">{link}</span>
      </Banner>
    );
  }
  return (
    <p role="status" data-testid="licence-needed"
       className={cn("m-0! mt-1.5! text-[13px] text-muted-foreground", className)} style={style}>
      {licenceSentence(what)} {link}
    </p>
  );
}
