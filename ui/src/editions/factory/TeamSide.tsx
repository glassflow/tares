import * as React from "react";
import { Link } from "react-router-dom";

import { Button, Panel } from "../../components/edition";
import type { HomeArea, HomeCrewLimit, HomeTeamCrew } from "./home.types";
import { colourOf } from "./WaitingForYou";

// What the side column of a cell with more than one person adds: how far the person's crew may go,
// the other crews, and the repos the person owns. A cell of one keeps today's column.

const FIRST_CREWS = 3;
const OWN_CREW = "Your crew";

function Head({ title, link }: { title: string; link?: { to: string; text: string } }) {
  return (
    <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
      <h2 className="m-0! text-base!">{title}</h2>
      {link && <Link to={link.to} className="text-[13px] text-muted-foreground!">{link.text}</Link>}
    </div>
  );
}

/** The foot of the person's own crew card: the link to its settings, the person's limit and the team's. */
export function CrewLimit({ limit }: { limit: HomeCrewLimit }) {
  const mine = limit.mine ? limit.mine[0].toUpperCase() + limit.mine.slice(1) : "";
  return (
    <div data-testid="crew-limit" className="mt-1 border-t border-border pt-3 text-[13px]">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <Link to="/settings#crew" className="font-medium text-[color:var(--accent-deep)]!">Autonomy</Link>
        <span className="text-right text-muted-foreground" data-testid="crew-limit-mine">{mine}</span>
      </div>
      <div className="mt-0.5 text-muted-foreground" data-testid="crew-limit-text">{limit.text}</div>
    </div>
  );
}

function CrewSquare({ name }: { name: string }) {
  const c = colourOf(name);
  return (
    <span aria-hidden data-user-text className="mt-0.5 grid size-7 flex-none select-none place-items-center text-[13px] font-semibold"
          style={{ background: c.bg, color: c.fg }}>
      {(Array.from(name)[0] ?? "?").toUpperCase()}
    </span>
  );
}

/** The other crews of the team, three rows then Show all; a crew not heard from in a day is one line below. */
export function TeamCrews({ crews, quiet }: { crews: HomeTeamCrew[]; quiet: string[] }) {
  const [all, setAll] = React.useState(false);
  const others = crews.filter((c) => c.name !== OWN_CREW);
  const silent = quiet.filter((n) => n !== OWN_CREW);
  if (others.length === 0 && silent.length === 0) return null;
  const shown = all ? others : others.slice(0, FIRST_CREWS);
  return (
    <section aria-label="The team’s crews" className="min-w-0" data-testid="team-crews">
      <Head title="The team’s crews" />
      <div className="border border-border bg-card">
        {shown.map((c) => (
          <div key={c.id} data-testid="team-crew-row" className="flex gap-3 border-t border-border px-[18px] py-3 first:border-t-0">
            <CrewSquare name={c.person?.name ?? c.name} />
            <div className="min-w-0 flex-1">
              <div className="flex justify-between gap-2">
                <b className="[overflow-wrap:anywhere]" data-user-text>{c.name}</b>
                <span className="flex-none text-xs text-muted-foreground" data-user-text>{c.heard}</span>
              </div>
              <div className="text-[13px] text-muted-foreground [overflow-wrap:anywhere]" data-user-text>
                {c.machine ? `${c.machine}. ` : ""}{c.doing}
              </div>
            </div>
          </div>
        ))}
        {others.length > shown.length && (
          <div className="border-t border-border px-[18px] py-3">
            <Button variant="quiet" className="px-0! text-[color:var(--accent-deep)]!" onClick={() => setAll(true)}>
              Show all {others.length} crews
            </Button>
          </div>
        )}
        {silent.length > 0 && (
          <div data-testid="quiet-crews" className="border-t border-border px-[18px] py-2.5 text-[13px] text-muted-foreground first:border-t-0">
            Not seen in a day: <span data-user-text>{silent.join(", ")}</span>
          </div>
        )}
      </div>
    </section>
  );
}

/** The repos the person owns, with what waits on each. */
export function ReposYouOwn({ areas }: { areas: HomeArea[] }) {
  if (areas.length === 0) return null;
  return (
    <section aria-label="Repos you own" className="min-w-0" data-testid="repos-you-own">
      <Head title="Repos you own" link={{ to: "/settings/advanced?tab=areas", text: "All owners" }} />
      <Panel>
        {areas.map((a) => (
          <div key={a.repo} data-testid="repo-you-own" className="mt-2 flex justify-between gap-2 text-[13px] first:mt-0">
            <code className="font-mono [overflow-wrap:anywhere]" data-user-text>{a.repo}</code>
            <span className="text-right text-muted-foreground">{a.note}</span>
          </div>
        ))}
      </Panel>
    </section>
  );
}
