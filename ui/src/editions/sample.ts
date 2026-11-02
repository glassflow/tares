import { useLocation } from "react-router-dom";

/** The browser flag that lets a `?sample=<name>` address show sample data. Only the e2e scripts set it. */
export const SAMPLE_FLAG = "taresSamples";

/** The sample a screen shows, from `?sample=<name>` in the address, or null. Sample data is fake, so
 *  it shows only when this browser has the flag set to "1"; on a live cell the address alone does
 *  nothing and the screen loads its real data. */
export function useSample(): string | null {
  const { search } = useLocation();
  let flagged = false;
  try {
    flagged = window.localStorage.getItem(SAMPLE_FLAG) === "1";
  } catch {
    flagged = false;
  }
  if (!flagged) return null;
  return new URLSearchParams(search).get("sample") || null;
}
