import { Link } from "react-router-dom";

import { Button, EmptyState } from "../../../components/edition";
import { formatCount } from "../../../lib/number";
import { formatDayUTC, formatDistance, formatStampUTC } from "../../../lib/time";
import type { MergeRow, MergesData, ReportData } from "../FactoryReport.api";
import { CAPTION, CELL, Figure, HEAD, LINK, NOTE, SUB, TABLE } from "./shared";

// Merges (TR-1672): the merges through the merge queue in these weeks, each with its autonomy level,
// the gate on the reviewed commit, the review rounds and who approved. The words of a row and the CSV
// come with the page's one read (tares/report.py `merges`).

const webUrl = (u: string | null): u is string => !!u && /^https?:\/\//i.test(u);

function download(csv: string, name: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = name;
  a.click();
  URL.revokeObjectURL(a.href);
}

function Commit({ label, sha }: { label: string; sha: string | null }) {
  return (
    <span className="block whitespace-nowrap">
      <span className="text-muted-foreground">{label}</span>{" "}
      {sha ? <span className="font-mono text-xs" title={sha}>{sha.slice(0, 7)}</span> : "not recorded"}
    </span>
  );
}

function Row({ r, now }: { r: MergeRow; now: number }) {
  const pull = `${r.repo} #${r.pr}`;
  return (
    <tr data-merge={`${r.repo}#${r.pr}`}>
      <th scope="row" className={CELL}>
        <span className="whitespace-nowrap" title={formatDistance(Date.parse(r.merged_at), now)}>{formatStampUTC(r.merged_at)}</span>
      </th>
      <td className={`${CELL} min-w-[200px]`} data-merged-ticket>
        {r.ticket ? (
          <Link className={LINK} to={`/projects/${encodeURIComponent(r.project)}/tickets/${encodeURIComponent(r.ticket)}`}>
            {r.ticket} <span data-user-text>{r.title}</span>
          </Link>
        ) : "Ticket not recorded"}
        <span className={SUB} data-user-text>{r.name}</span>
        <span className={SUB} data-user-text>
          {webUrl(r.url) ? <a href={r.url} target="_blank" rel="noopener noreferrer" className="text-muted-foreground underline decoration-border underline-offset-[3px]">{pull}</a> : pull}
        </span>
      </td>
      <td className={`${CELL} min-w-[170px]`}>
        <span className="block" data-level>{r.level_words}</span>
        <span className={SUB} data-gate>{r.gate_words}</span>
      </td>
      <td className={CELL} data-commits>
        <Commit label="reviewed" sha={r.reviewed_sha} />
        <Commit label="merged" sha={r.merged_sha} />
      </td>
      <td className={CELL} data-rounds>{r.rounds === null ? "not recorded" : formatCount(r.rounds)}</td>
      <td className={`${CELL} min-w-[110px]`} data-approver {...(r.approver ? { "data-user-text": true } : {})}>{r.approver_words}</td>
    </tr>
  );
}

const COUNTED = [
  "One line for each pull request that went through the merge queue, in the week it was merged (UTC). Pushes that skipped the queue and merges from before it are not here, so these counts can differ from Delivered.",
  "A merge with no record is placed at the last time the queue wrote on it, and says what is not recorded.",
  "The reviewed commit is the one that passed the gate. When Tares updated the branch from its base first, that is the updated commit.",
  "The gate says whether Tares merged after the reviewer's pass or the pull request was merged outside Tares, and what CI said.",
  "The approver is the person who said yes to the merged commit. At L4 no approval is needed.",
  "The level and the review rounds of a merge are those of the time it was merged.",
];

export function Merges({ data }: { data: ReportData }) {
  const d = data.sections.merges as MergesData;
  const { from, to, running_week: running } = data.range;
  const now = Date.now();
  return (
    <div className="grid min-w-0 grid-cols-1 gap-6 pt-3 [overflow-wrap:anywhere]">
      {d.total === 0 ? (
        <EmptyState className="py-0" data-testid="merges-empty">Nothing was merged through the merge queue in these weeks.</EmptyState>
      ) : (
        <>
          <Figure title="Merges per week by level" data-testid="merges-weeks">
            <table aria-label="Merges per week by level" className={TABLE}>
              <thead>
                <tr>{["Week", "L3: a person approves", "L4: merges alone", "Level not recorded"].map((h) => <th key={h} scope="col" className={HEAD}>{h}</th>)}</tr>
              </thead>
              <tbody>
                {d.weeks.map((w) => (
                  <tr key={w.week} data-week={w.week}>
                    <th scope="row" className={CELL}>
                      <span className="whitespace-nowrap">{formatDayUTC(w.week)}</span>
                      {w.week === running && <span className={SUB}>so far</span>}
                    </th>
                    <td className={CELL}>{formatCount(w["L3-review"])}</td>
                    <td className={CELL}>{formatCount(w["L4-ship"])}</td>
                    <td className={CELL}>{formatCount(w.unrecorded)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Figure>
          <div className="grid min-w-0 gap-2">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className={NOTE} data-testid="merges-shown">
                {d.total > d.rows.length
                  ? `Showing the newest ${formatCount(d.rows.length)} of ${formatCount(d.total)} merges. The CSV holds all of them.`
                  : `${formatCount(d.total)} ${d.total === 1 ? "merge" : "merges"} in these weeks, newest first.`}
              </p>
              <Button variant="secondary" data-testid="merges-csv" onClick={() => download(d.csv, `merges-${from}-to-${to}.csv`)}>
                Download CSV
              </Button>
            </div>
            <Figure title="Merge ledger" data-testid="merges-ledger">
              <table aria-label="Merge ledger" className={TABLE}>
                <thead>
                  <tr>{["Merged", "Change", "Level and gate", "Commits", "Review rounds", "Approver"].map((h) => <th key={h} scope="col" className={HEAD}>{h}</th>)}</tr>
                </thead>
                <tbody>
                  {d.rows.map((r) => <Row key={`${r.repo}#${r.pr}#${r.merged_at}`} r={r} now={now} />)}
                </tbody>
              </table>
            </Figure>
          </div>
        </>
      )}
      {d.levels.length > 0 && (
        <div className="grid min-w-0 gap-1" data-testid="merges-levels">
          <div className={CAPTION}>Each project's level now</div>
          <ul className="m-0 grid list-none gap-0.5 p-0 text-[13px]">
            {d.levels.map((l) => (
              <li key={l.project} className="m-0 min-w-0" data-project={l.project}>
                <span data-user-text>{l.name}</span> · {l.level_words}
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="grid min-w-0 gap-1" data-testid="merges-counted">
        <div className={CAPTION}>How this is counted</div>
        {COUNTED.map((line) => <p key={line} className={NOTE}>{line}</p>)}
      </div>
    </div>
  );
}
