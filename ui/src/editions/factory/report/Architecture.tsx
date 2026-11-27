import { Link } from "react-router-dom";

import { EmptyState } from "../../../components/edition";
import { formatCount } from "../../../lib/number";
import { formatDayUTC } from "../../../lib/time";
import type { ArchitectureData, ArchitectureRepo, DecisionList, ReportData, ReportDecision } from "../FactoryReport.api";
import { CAPTION, CELL, Figure, HEAD, LINK, NOTE, NoValue, SUB, TABLE } from "./shared";

// Architecture (TR-1673): the architecture findings of review in these weeks, by repo and week, the
// repos where they keep rising, and the decisions made beside each repo. All of it comes with the
// page's one read (tares/report.py `architecture`).

const COUNTED = [
  "A review finding counts here when its text is about how the code is divided up, such as layering, a module boundary or an import cycle. It is matched by words, so some are missed and some are put here by mistake.",
  "Findings recorded before this kind existed are not counted, so the first weeks can look like a rise.",
  "A finding counts in the week a review first raised it (Monday to Sunday, UTC), at any severity, whether it is fixed or still open. The counts are as of the cell's last pass.",
  "A finding's repo is that of its ticket's pull request. Repo not recorded holds the findings of tickets with none, and is never marked as rising.",
  "Rising looks at the four newest finished weeks up to the end of the range, also when some are before it: each of the last three had more of these findings than the week before it. A week that is not over is left out.",
  "Decisions are all those made in these weeks, not only the ones about architecture. Merge approvals are left out, and so are decisions asked with no project.",
  "A decision is listed under each repo of its tickets' pull requests, and under Not tied to a repo when they have none. A long list shows only the newest ones, with the total.",
  "A decision's repo is as of the cell's last pass, made about every 5 minutes, so a decision on a newer ticket or pull request is under Not tied to a repo until the next one. After an upgrade, stored weeks read Repo not recorded until the first pass, about 30 seconds after the cell starts.",
  "Only projects you can open are counted and named.",
];

const TITLE = "Architecture findings per week by repo";

function Weeks({ repos, running }: { repos: ArchitectureRepo[]; running: string | null }) {
  return (
    <Figure title={TITLE} data-testid="architecture-weeks">
      <table aria-label={TITLE} className={TABLE}>
        <thead>
          <tr>
            <th scope="col" className={HEAD}>Repo</th>
            {repos[0].weeks.map((w) => (
              <th key={w.week} scope="col" className={HEAD} data-week={w.week}>
                <span className="whitespace-nowrap">{formatDayUTC(w.week)}</span>
                {w.week === running && <span className={SUB}>so far</span>}
              </th>
            ))}
            <th scope="col" className={HEAD}>Total</th>
            <th scope="col" className={HEAD}>Decisions</th>
          </tr>
        </thead>
        <tbody>
          {repos.map((r) => (
            <tr key={r.repo ?? ""} data-repo={r.repo ?? ""}>
              <th scope="row" className={CELL}>
                {r.repo === null ? "Repo not recorded" : <span data-user-text>{r.repo}</span>}
                {r.rising && <span className="block text-xs font-semibold" data-rising>Rising</span>}
              </th>
              {r.weeks.map((w) => <td key={w.week} className={CELL} data-count={w.week}>{formatCount(w.count)}</td>)}
              <td className={CELL} data-total>{formatCount(r.total)}</td>
              {/* the decisions of no repo are listed apart, so this row has no count of them */}
              <td className={CELL} data-decided>{r.repo === null ? <NoValue /> : formatCount(r.decisions_total)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Figure>
  );
}

function Decision({ d }: { d: ReportDecision }) {
  return (
    <li className="m-0 grid min-w-0 gap-0.5 text-[13px]" data-decision>
      <span data-user-text data-question>{d.question}</span>
      <span data-answer>
        {d.answer ? <>Answer: <span data-user-text>{d.answer}</span></> : "No answer recorded"}
      </span>
      <span className={SUB} data-made>
        {d.person ? <span data-user-text>{d.person.name}</span> : "Person not recorded"}
        {" · "}{formatDayUTC(d.at)}
        {" · "}<span data-user-text>{d.name}</span>
        {d.tickets.map((ticket, at) => (
          <span key={at}>
            {" · "}
            <Link className={LINK} to={`/projects/${encodeURIComponent(d.project)}/tickets/${encodeURIComponent(ticket)}`} data-user-text>
              {ticket}
            </Link>
          </span>
        ))}
      </span>
    </li>
  );
}

/** The decisions of one repo, or of none. */
function Decisions({ repo, list }: { repo: string | null; list: DecisionList }) {
  return (
    <div className="grid min-w-0 gap-1.5" data-decisions={repo ?? ""}>
      <h3 className="m-0 text-[13px] font-semibold">
        {repo === null ? "Not tied to a repo" : <span data-user-text>{repo}</span>}
      </h3>
      <ul className="m-0 grid list-none gap-2.5 p-0">
        {list.decisions.map((d, at) => <Decision key={at} d={d} />)}
      </ul>
      {list.decisions_total > list.decisions.length && (
        <p className={NOTE} data-shown>
          Showing the newest {formatCount(list.decisions.length)} of {formatCount(list.decisions_total)} decisions.
        </p>
      )}
    </div>
  );
}

export function Architecture({ data }: { data: ReportData }) {
  const { repos, unplaced } = data.sections.architecture as ArchitectureData;
  const decided = repos.filter((r) => r.decisions_total > 0);
  const anyDecision = decided.length > 0 || unplaced.decisions_total > 0;
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 pt-3 [overflow-wrap:anywhere]">
      {repos.length === 0 && !anyDecision ? (
        <EmptyState className="py-0" data-testid="architecture-empty">
          No architecture finding was first raised in these weeks, and there is no decision to list.
        </EmptyState>
      ) : (
        <>
          {repos.length > 0 ? (
            <Weeks repos={repos} running={data.range.running_week} />
          ) : (
            <EmptyState className="py-0" data-testid="architecture-no-findings">No architecture finding was first raised in these weeks.</EmptyState>
          )}
          {anyDecision ? (
            <div className="grid min-w-0 gap-3" data-testid="architecture-decisions">
              <div className={CAPTION}>Decisions made in these weeks</div>
              {decided.map((r) => <Decisions key={r.repo} repo={r.repo} list={r} />)}
              {unplaced.decisions_total > 0 && <Decisions repo={null} list={unplaced} />}
            </div>
          ) : (
            <p className={NOTE} data-testid="architecture-no-decisions">There is no decision to list for these weeks.</p>
          )}
        </>
      )}
      <div className="grid min-w-0 gap-1" data-testid="architecture-counted">
        <div className={CAPTION}>How this is counted</div>
        {COUNTED.map((line) => <p key={line} className={NOTE}>{line}</p>)}
      </div>
    </div>
  );
}
