import { Link } from "react-router-dom";

import { Banner } from "@/components/edition";
import { useEdition } from "../editions/edition";
import { useCloud } from "../cloud";
import { useLicence } from "../licence";
import { licenceDate } from "./LicencePanel";

/** One line above the page when the licence ended or the cell has more people than it covers. Nothing
 *  stops working: adding people is what pauses. Links to the Enterprise licence tab. */
export default function LicenceBanner() {
  const { state } = useLicence();
  const { edition } = useEdition();
  const cloud = useCloud();
  if (!cloud.ready || cloud.hosted) return null;   // Tares Cloud needs no licence
  if (!state || (!state.expired && !state.over_seats)) return null;
  const to = edition === undefined || edition === "platform" ? "/settings?tab=licence" : "/settings/advanced?tab=licence";
  const words = state.expired
    ? `The enterprise licence ended on ${licenceDate(state.expires)}. Everything keeps working; adding people is paused until it is renewed.`
    : `This cell has ${state.used} people and the licence covers ${state.seats}. Adding people is paused.`;
  return (
    <Banner tone="warn" data-testid="licence-banner" className="mb-4 border-x-0 border-t-0"
            action={<Link to={to} className="text-foreground! underline! decoration-border! underline-offset-[3px]" data-testid="licence-banner-link">Open the licence</Link>}>
      {words}
    </Banner>
  );
}
