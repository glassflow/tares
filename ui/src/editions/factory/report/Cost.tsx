import * as React from "react";

import { fmtCost } from "../../../components/bits";
import { EmptyState, ListRow, Stat } from "../../../components/edition";
import { formatCount } from "../../../lib/number";
import { HOUR, durationWords, formatDayUTC, formatDistance, formatStampUTC } from "../../../lib/time";
import type { CostData, CostRole, CostedRow, Money, ReportData } from "../FactoryReport.api";
import { CAPTION, CELL, HEAD, NoValue, TABLE, tickets } from "./shared";

// Cost (TR-1671): what the model replies of these weeks would cost at API list prices, per project and
// week and by role, then what merged tickets and accepted milestones cost. A reply names no ticket, so
// the ticket figures are approximate (tares/report_cost.py has the rule). They are the cell's last pass.

const ROLE: Record<CostRole, string> = {
  builder: "Builder", reviewer: "Reviewer", orchestrator: "Orchestrator", releaser: "Releaser",
  person: "Own sessions", other: "Other",
};

/** The three highest spending projects have an ink of their own in the bars; the rest share one. */
const INKS = ["fill-foreground", "fill-muted-foreground", "fill-faint"];
const ink = (i: number) => INKS[i] ?? "fill-border";

const NOTE = "m-0 max-w-[640px] text-xs text-muted-foreground";

const dollars = (v: number) => (v === 0 ? "$0" : fmtCost(v));
/** "$12.40", or "at least $12.40" when replies with no price are left out of it. */
const amount = (m: Money) => (m.unpriced > 0 ? `at least ${dollars(m.cost_usd)}` : dollars(m.cost_usd));
const noPrice = (n: number) => `${formatCount(n)} ${n === 1 ? "reply" : "replies"} with no price`;

function Amount({ money }: { money: Money }) {
  return (
    <>
      <span className="whitespace-nowrap">{amount(money)}</span>
      {money.unpriced > 0 && <span className="block text-xs text-muted-foreground">{noPrice(money.unpriced)}</span>}
    </>
  );
}

function Swatch({ index }: { index: number }) {
  return (
    <svg aria-hidden="true" width={8} height={8} className="mr-1.5 inline-block shrink-0">
      <rect width={8} height={8} className={ink(index)} />
    </svg>
  );
}

/** A caption over a table that scrolls sideways when it does not fit. */
function Figure({ title, note, children, ...props }: {
  title: string; note: string; children: React.ReactNode; "data-testid": string;
}) {
  const id = React.useId();
  return (
    <figure aria-labelledby={id} className="m-0 grid min-w-0 content-start gap-2" {...props}>
      <figcaption id={id} className={CAPTION}>{title}</figcaption>
      <div role="region" aria-labelledby={id} tabIndex={0} className="min-w-0 overflow-x-auto focus-visible:outline-2 focus-visible:outline-ring">
        {children}
      </div>
      <p className={NOTE} data-note>{note}</p>
    </figure>
  );
}

function Weeks({ d, running }: { d: CostData; running: string | null }) {
  const most = Math.max(...d.weeks.map((w) => w.cost_usd), 0) || 1;
  const names = new Map(d.projects.map((p, i) => [p.project, { name: p.name, index: i }]));
  return (
    <Figure title="API-equivalent cost per project per week" data-testid="cost-weeks"
            note="Only replies this cell received. A session that sends no usage, a deleted project and a project you cannot open are not here.">
      <table aria-label="API-equivalent cost per project per week" className={TABLE}>
        <thead>
          <tr>{["Week", "Projects", "Total"].map((h) => <th key={h} scope="col" className={HEAD}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {d.weeks.map((week) => {
            // the projects keep one order, the range's, so an ink is in the same place in every bar
            const parts = d.projects.filter((p) => week.projects[p.project]).map((p) => ({ id: p.project, ...week.projects[p.project] }));
            let from = 0;
            return (
              <tr key={week.week} data-week={week.week}>
                <th scope="row" className={`${CELL} w-28`}>
                  <span className="whitespace-nowrap">{formatDayUTC(week.week)}</span>
                  {week.week === running && <span className="block text-xs text-muted-foreground">so far</span>}
                </th>
                <td className={CELL}>
                  <svg aria-hidden="true" width="100%" height={10} className="block">
                    {parts.map((part) => {
                      const x = from;
                      from += part.cost_usd;
                      return <rect key={part.id} data-bar={part.id} x={`${(100 * x) / most}%`} width={`${(100 * part.cost_usd) / most}%`}
                                   height={10} className={ink(names.get(part.id)!.index)} />;
                    })}
                  </svg>
                  {parts.length > 0 && (
                    <ul className="m-0 mt-1 flex list-none flex-wrap gap-x-3 gap-y-0.5 p-0 text-xs text-muted-foreground">
                      {parts.map((part) => (
                        <li key={part.id} className="m-0 min-w-0 [overflow-wrap:anywhere]" data-project={part.id}>
                          <Swatch index={names.get(part.id)!.index} />
                          <span data-user-text>{names.get(part.id)!.name}</span> {amount(part)}
                        </li>
                      ))}
                    </ul>
                  )}
                </td>
                <td className={`${CELL} w-28 text-right`} data-total>{amount(week)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Figure>
  );
}

function Split({ title, head, note, total, rows, testid }: {
  title: string; head: string; note: string; total: Money; testid: string;
  rows: { key: string; name: React.ReactNode; money: Money }[];
}) {
  return (
    <Figure title={title} note={note} data-testid={testid}>
      <table aria-label={title} className={TABLE}>
        <thead>
          <tr>{[head, "In these weeks"].map((h) => <th key={h} scope="col" className={HEAD}>{h}</th>)}</tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} data-row={r.key}>
              <th scope="row" className={`${CELL} [overflow-wrap:anywhere]`}>{r.name}</th>
              <td className={`${CELL} text-right`}><Amount money={r.money} /></td>
            </tr>
          ))}
          <tr data-row="total">
            <th scope="row" className={`${CELL} font-semibold`}>Total</th>
            <td className={`${CELL} text-right font-semibold`}><Amount money={total} /></td>
          </tr>
        </tbody>
      </table>
    </Figure>
  );
}

const rounds = (n: number) => `${formatCount(n)} review ${n === 1 ? "round" : "rounds"}`;
const took = (hours: number | null, to: string) =>
  hours === null ? "no start recorded" : `${durationWords(Math.round(hours * HOUR))} from first start to ${to}`;

function Top<T extends CostedRow>({ id, title, note, empty, rows, to, name, facts }: {
  id: string; title: string; note: string; empty: string; rows: T[];
  to: (row: T) => string; name: (row: T) => React.ReactNode; facts: (row: T) => string;
}) {
  return (
    <div className="grid min-w-0 content-start gap-1" data-testid={`cost-top-${id}`}>
      <div className={CAPTION}>{title}</div>
      {rows.length === 0 ? <EmptyState className="py-1">{empty}</EmptyState> : (
        <div className="min-w-0 [--row-x:0px]">
          {rows.map((row) => (
            <ListRow key={to(row)} to={to(row)} data-testid={`cost-top-${id}-row`}
                     className="flex-col flex-nowrap items-start gap-1 py-3 text-foreground">
              <span className="text-sm">{name(row)}</span>
              <span className="text-xs text-muted-foreground">
                <span data-user-text>{row.project_name}</span> · {amount(row)}
                {row.unpriced > 0 && ` · ${noPrice(row.unpriced)}`} · {facts(row)}
              </span>
            </ListRow>
          ))}
        </div>
      )}
      <p className={NOTE} data-note>{note}</p>
    </div>
  );
}

function ByTicket({ d, running }: { d: NonNullable<CostData["by_ticket"]>; running: string | null }) {
  const now = Date.now();
  const page = (id: string) => `/projects/${encodeURIComponent(id)}`;
  const { milestones: each, untied } = d;
  return (
    <div className="grid min-w-0 gap-5" data-testid="cost-tickets">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <h3 className="m-0 text-[15px] font-semibold">Tickets and milestones, approximate</h3>
        <p className="m-0 text-xs text-muted-foreground" data-testid="cost-as-of" title={formatDistance(Date.parse(d.at), now)}>
          As of {formatStampUTC(d.at)}
        </p>
      </div>
      <div className="grid grid-cols-1 gap-x-8 gap-y-5 sm:grid-cols-2">
        <div className="grid min-w-0 content-start gap-1.5 [overflow-wrap:anywhere]" data-testid="cost-per-milestone">
          <Stat label="API-equivalent cost per accepted milestone"
                value={each.mean_cost_usd === null ? "no value" : amount({ cost_usd: each.mean_cost_usd, unpriced: each.unpriced_milestones })} />
          <p className={NOTE}>
            {formatCount(each.accepted)} {each.accepted === 1 ? "milestone" : "milestones"} accepted in these weeks
            {each.unpriced_milestones > 0 && `, ${formatCount(each.unpriced_milestones)} with replies with no price`}
          </p>
          <p className={NOTE} data-note>Only milestones a person accepted. A deleted ticket or milestone is not here.</p>
        </div>
        <div className="grid min-w-0 content-start gap-1.5 [overflow-wrap:anywhere]" data-testid="cost-untied">
          <Stat label="Not tied to a ticket, API-equivalent cost" value={amount(untied)} />
          {untied.unpriced > 0 && <p className={NOTE}>{noPrice(untied.unpriced)}</p>}
          {untied.no_project && (
            <p className="m-0 text-[13px]" data-testid="cost-no-project">
              No project: {amount(untied.no_project)}
              {untied.no_project.unpriced > 0 && `, ${noPrice(untied.no_project.unpriced)}`}
            </p>
          )}
          <p className={NOTE} data-note>
            Replies no ticket takes, with all of the orchestrator, the releaser, own sessions and other roles. Counted reply by reply.
          </p>
        </div>
      </div>
      <Figure title="API-equivalent cost per merged ticket per week" data-testid="cost-per-ticket"
              note="The mean over the tickets first merged in the week, each with the cost of its whole life. Tickets not merged are not here.">
        <table aria-label="API-equivalent cost per merged ticket per week" className={TABLE}>
          <thead>
            <tr>{["Week", "Merged", "Mean per ticket"].map((h) => <th key={h} scope="col" className={HEAD}>{h}</th>)}</tr>
          </thead>
          <tbody>
            {d.weeks.map((w) => (
              <tr key={w.week} data-week={w.week}>
                <th scope="row" className={CELL}>
                  <span className="whitespace-nowrap">{formatDayUTC(w.week)}</span>
                  {w.week === running && <span className="block text-xs text-muted-foreground">so far</span>}
                </th>
                <td className={CELL}>{formatCount(w.merged)}</td>
                <td className={`${CELL} text-right`}>
                  {w.mean_cost_usd === null ? <NoValue /> : (
                    <>
                      <span className="whitespace-nowrap">{amount({ cost_usd: w.mean_cost_usd, unpriced: w.unpriced_tickets })}</span>
                      {w.unpriced_tickets > 0 && (
                        <span className="block text-xs text-muted-foreground">
                          {formatCount(w.unpriced_tickets)} {tickets(w.unpriced_tickets)} with replies with no price
                        </span>
                      )}
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Figure>
      <div className="grid min-w-0 grid-cols-1 gap-5 lg:grid-cols-2 lg:gap-x-8">
        <Top id="tickets" title="Most expensive tickets, API-equivalent cost" rows={d.top_tickets}
             empty="No ticket merged in these weeks has a cost."
             note="Merged tickets only, placed by their first merge."
             to={(t) => `${page(t.project)}/tickets/${encodeURIComponent(t.label)}`}
             name={(t) => <>{t.label} <span data-user-text>{t.title}</span></>}
             facts={(t) => `${rounds(t.rounds)} · ${took(t.hours, "merge")}`} />
        <Top id="milestones" title="Most expensive milestones, API-equivalent cost" rows={d.top_milestones}
             empty="No milestone accepted in these weeks has a cost."
             note="Placed by the latest acceptance. The sum of its tickets, canceled ones included."
             to={(m) => `${page(m.project)}/milestones#milestone-${encodeURIComponent(m.id)}`}
             name={(m) => <span data-user-text>{m.name}</span>}
             facts={(m) => `${formatCount(m.tickets)} ${tickets(m.tickets)} · ${rounds(m.rounds)} · ${took(m.hours, "acceptance")}`} />
      </div>
    </div>
  );
}

const COUNTED = [
  "A reply names no ticket, so the cost of a ticket is approximate.",
  "A builder's reply goes to the tickets it held in progress or with changes asked at that time, split evenly. With none, it goes to its tickets in review. With none of those, it is not tied to a ticket. A helper's reply goes as its builder's would.",
  "A reviewer's reply is split over the tickets in review at that time. With none in review, it is not tied to a ticket. A ticket's share counts the tickets in review in projects open to everyone and in its own project, so a ticket has one cost for every reader.",
  "A reply that counts for a project with limited access goes only to that project's tickets.",
  "A scope narrower than the whole factory misses crew replies that carry no project. A ticket's cost follows the crew member that held it, so ticket costs need not fit inside a project's total.",
  "Cost per merged ticket in a week is the mean cost of the tickets first merged that week.",
];

export function Cost({ data }: { data: ReportData }) {
  const d = data.sections.cost as CostData;
  const { running_week: running } = data.range;
  const spent = d.total.cost_usd > 0 || d.total.unpriced > 0;
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 pt-3 [overflow-wrap:anywhere]">
      <div className="grid min-w-0 gap-1">
        <p className="m-0 max-w-[640px] text-[13px]" data-testid="cost-label">
          API-equivalent cost: list prices from tares/pricing.py. A subscription crew pays a flat fee.
        </p>
        {data.scope.value !== "factory" && (
          <p className={NOTE} data-testid="cost-narrow">This scope misses crew replies that carry no project.</p>
        )}
      </div>
      {spent ? (
        <>
          <div className="grid min-w-0 gap-1.5" data-testid="cost-total">
            <Stat label="API-equivalent cost" value={amount(d.total)} />
            {d.total.unpriced > 0 && <p className={NOTE}>{noPrice(d.total.unpriced)}</p>}
          </div>
          <Weeks d={d} running={running} />
          <div className="grid min-w-0 grid-cols-1 gap-6 lg:grid-cols-2 lg:gap-x-8">
            <Split testid="cost-projects" title="API-equivalent cost by project" head="Project" total={d.total}
                   note="Highest first. A project you cannot open is not here."
                   rows={d.projects.map((p, i) => ({
                     key: p.project, money: p, name: <><Swatch index={i} /><span data-user-text>{p.name}</span></>,
                   }))} />
            <Split testid="cost-roles" title="API-equivalent cost by role" head="Role" total={d.total}
                   note="Own sessions have no crew role. Other holds helpers, comms and any other role. Roles are as the cell knows them now."
                   rows={d.roles.map((r) => ({ key: r.role, name: ROLE[r.role], money: r }))} />
          </div>
        </>
      ) : <EmptyState className="py-0" data-testid="cost-empty">No spend was received for these weeks.</EmptyState>}
      {d.by_ticket ? <ByTicket d={d.by_ticket} running={running} />
        : <EmptyState className="py-0" data-testid="cost-not-yet">Ticket and milestone costs are not worked out yet.</EmptyState>}
      <div className="grid min-w-0 gap-1" data-testid="cost-counted">
        <div className={CAPTION}>How this is counted</div>
        {COUNTED.map((line) => <p key={line} className={NOTE}>{line}</p>)}
      </div>
    </div>
  );
}
