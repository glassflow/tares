import type { CSSProperties } from "react";

import { Banner } from "@/components/edition";
import { useCloud } from "@/cloud";
import { cn } from "@/lib/utils";
import { ENTERPRISE_URL, licenceSentence, licenceWordsOn } from "@/licenceWords";

// the words and the address live in licenceWords.ts (api.ts needs them too); re-exported so
// existing imports keep working
export { ENTERPRISE_URL, licenceSentence };

const LINK = "text-foreground! underline! decoration-border! underline-offset-[3px]";

/** Shown where a team action would be, when the cell answers it with `licence_required`. `inline`
 *  is one muted line under the control; `page` is a boxed note for a page that is only for teams.
 *  On a Tares Cloud cell, and until /health says the cell is self-hosted, it says "not available on
 *  this workspace right now" and links nowhere: the cloud never names a licence. */
export default function LicenceNeeded({ what, variant = "inline", url = ENTERPRISE_URL, className, style }: {
  what?: string;
  variant?: "inline" | "page";
  url?: string;
  className?: string;
  style?: CSSProperties;
}) {
  useCloud();   // renders again when /health lands, which is what switches the words
  if (!licenceWordsOn()) {
    if (variant === "page") {
      return (
        <Banner tone="note" data-testid="licence-needed" className={cn("items-start", className)} style={style}>
          <strong className="block font-medium">Teams are not available on this workspace right now.</strong>
        </Banner>
      );
    }
    return (
      <p role="status" data-testid="licence-needed"
         className={cn("m-0! mt-1.5! text-[13px] text-muted-foreground", className)} style={style}>
        {licenceSentence(what)}
      </p>
    );
  }
  const link = (
    <a href={url || ENTERPRISE_URL} target="_blank" rel="noreferrer" className={LINK}>About the enterprise licence</a>
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
