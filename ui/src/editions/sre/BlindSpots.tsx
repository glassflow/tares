import * as React from "react";
import { Link } from "react-router-dom";

import { Button, ListSection } from "../../components/edition";
import type { HomeBlindSpot } from "./Home.api";

const FIRST = 5;

/** "Blind spots": where a problem would go unseen or unexplained, each with the way to fix it. The
 *  first few show; the rest wait behind a button. */
export function BlindSpots({ items }: { items: HomeBlindSpot[] }) {
  const [all, setAll] = React.useState(false);
  if (items.length === 0) return null;
  const shown = all ? items : items.slice(0, FIRST);
  return (
    <ListSection title="Blind spots" data-testid="blind-spots">
      <ul className="m-0! list-none! p-0!">
        {shown.map((b, i) => (
          <li key={`${b.kind}-${b.service}-${i}`} className="grid gap-1 border-t border-border px-[18px] py-3 first:border-t-0" data-testid="blind-spot">
            <b className="text-[14px]" data-user-text>{b.title}</b>
            <span className="text-[13px] text-muted-foreground" data-user-text>{b.detail}</span>
            <Link to={b.action.to} className="w-fit text-[13px] text-foreground underline underline-offset-2">{b.action.label}</Link>
          </li>
        ))}
      </ul>
      {items.length > FIRST && (
        <div className="border-t border-border px-[18px] py-3">
          <Button variant="secondary" className="h-8 text-[13px]!" onClick={() => setAll(!all)} data-testid="blind-spots-toggle">
            {all ? "Show fewer" : `Show ${items.length - FIRST} more`}
          </Button>
        </div>
      )}
    </ListSection>
  );
}
