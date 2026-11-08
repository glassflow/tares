// Tares enterprise licence, see ee/LICENSE
import * as React from "react";

import { cn } from "@/lib/utils";
import type { HomeItem } from "@/editions/factory/home.types";

// Who a waiting item or a project member is from: the crew's mark, a person's picture or their
// initial in a colour. The team home draws it: waiting rows, the members on a project row.

// People other than the crew wear one of these, chosen by name so a person keeps their colour.
const PEOPLE: { bg: string; fg: string }[] = [
  { bg: "#1e0f00", fg: "#faf9ec" },
  { bg: "#2f5d8a", fg: "#ffffff" },
  { bg: "#6b4f8a", fg: "#ffffff" },
];
const CREW_COLOUR = { bg: "var(--accent)", fg: "var(--on-accent)" };

export function colourOf(key: string): { bg: string; fg: string } {
  let h = 0;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return PEOPLE[h % PEOPLE.length];
}

/** Who an item is from, in words, for the meta line: "your crew", or the person's name. */
export function senderWords(from: HomeItem["from"]): string {
  return !from || from.crew === true ? "your crew" : from.name;
}

/** The mark next to a row: the crew's mark, the person's picture (TR-1016) when they have one,
 *  else the person's initial in their colour. `small` is the member square on a project row. */
export function Sender({ from, small }: { from: HomeItem["from"]; small?: boolean }) {
  const crew = !from || from.crew === true;
  const name = crew ? "Your crew" : from.name;
  const c = crew ? CREW_COLOUR : colourOf(from.id ?? from.name);
  const [failed, setFailed] = React.useState(false);
  const pic = !crew && from?.picture && /^https:\/\//i.test(from.picture) && !failed ? from.picture : null;
  const size = small ? "size-5" : "mt-0.5 size-7";
  if (crew) {
    return (
      <span role="img" aria-label={small ? name : `From ${name}`} title={name} data-testid={small ? "project-member" : "waiting-from"}
            data-from="crew" className={cn("grid flex-none place-items-center border border-border bg-card", size)}>
        <img src="/tares-mark.svg" alt="" aria-hidden className={small ? "w-3.5" : "w-4.5"} />
      </span>
    );
  }
  if (pic) {
    return (
      <span role="img" aria-label={small ? name : `From ${name}`} title={name} data-testid={small ? "project-member" : "waiting-from"}
            data-from="picture" className={cn("flex-none overflow-hidden rounded-full bg-muted", size)}>
        <img src={pic} alt="" referrerPolicy="no-referrer" loading="lazy" onError={() => setFailed(true)} className="size-full object-cover" />
      </span>
    );
  }
  return (
    <span
      role="img" aria-label={small ? name : `From ${name}`} title={name} data-testid={small ? "project-member" : "waiting-from"}
      className={cn("grid flex-none select-none place-items-center font-semibold", small ? "size-5 text-[11px]" : "mt-0.5 size-7 text-[13px]")}
      style={{ background: c.bg, color: c.fg }}
      data-user-text
    >
      {(crew ? "Y" : Array.from(name)[0] ?? "?").toUpperCase()}
    </span>
  );
}
