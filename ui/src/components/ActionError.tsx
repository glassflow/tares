import type { CSSProperties } from "react";

import { isLicenceRequired } from "../api";
import LicenceNeeded from "./LicenceNeeded";

/** What a failed team action shows: the licence line when the cell needs an enterprise licence for
 *  it, else the error as the console's usual red box. `what` names the action ("Adding people"). */
export default function ActionError({ error, what, className = "alert error", style }: {
  error: unknown; what: string; className?: string; style?: CSSProperties;
}) {
  if (isLicenceRequired(error)) return <LicenceNeeded what={what} url={error.url} style={style} />;
  const text = typeof error === "string" ? error : String((error as Error)?.message ?? error);
  return <div className={className} style={style}>{text}</div>;
}
