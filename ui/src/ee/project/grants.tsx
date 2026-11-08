// Tares enterprise licence, see ee/LICENSE
import { Link } from "react-router-dom";

import { api } from "../../api";
import { Picker, usePolling } from "../../components/bits";
import { usePeople } from "../../components/usePeople";
import type { Grant, GrantsView } from "../../types";
import { consentGrant } from "../api";

// The team side of the Grants view (TR-630): who has agreed to a grant, the Consent button for the
// person a grant waits for, the grants of other projects that wait for the caller, and the repos a
// project grant names (a repo someone else owns makes the grant wait for that owner). The grant
// list, adding one and revoking one stay in the core console.

/** The "agreed by" column. `part` is the header cell or one grant's cell. */
export function GrantAgreedBy({ part, grant }: { part: "head" | "cell"; grant?: Grant }) {
  if (part === "head") return <th>agreed by</th>;
  return <td className="help">{(grant?.consents ?? []).filter((c) => !c.declined).map((c) => c.name).join(", ")}</td>;
}

/** After "Waiting" in the state cell: " for Kim, Lee", who the grant waits for. Nothing when nobody is named. */
export function GrantWaitingFor({ grant }: { grant: Grant }) {
  const names = grant.waiting_for.map((w) => w.name ?? w.id).join(", ");
  return names ? <> for {names}</> : null;
}

/** The Consent button, for the person a waiting grant waits for (or an admin, when it waits for one). */
export function GrantConsent({ grant, list, busy, onBusy, reload, onError }: {
  grant: Grant; list?: GrantsView; busy: boolean;
  onBusy: (id?: string) => void; reload: () => void; onError: (e: unknown) => void;
}) {
  const { meId, isAdmin } = usePeople();
  const waitingForMe = list?.waiting_for_me ?? [];
  const may = grant.state === "waiting" && (waitingForMe.some((g) => g.id === grant.id)
    || (!!meId && grant.waiting_for.some((w) => w.id === meId))
    || (isAdmin && grant.waiting_for.some((w) => w.id === "admin")));
  if (!may) return null;
  const consent = async () => {
    onBusy(grant.id); onError(undefined);
    try { await consentGrant(grant.id); reload(); } catch (e) { onError(e); }
    onBusy(undefined);
  };
  return <button className="primary" disabled={busy} onClick={consent}>Consent</button>;
}

/** Grants of other projects that wait for the caller's yes: they may not see those projects. `row`
 *  and `head` are the core's own, so the table looks the same as the one above it. */
export function GrantsWaitingElsewhere({ here, list, row, head }: {
  here: Grant[]; list?: GrantsView; row: (g: Grant) => React.ReactNode; head: React.ReactNode;
}) {
  const elsewhere = (list?.waiting_for_me ?? []).filter((g) => !here.some((h) => h.id === g.id));
  if (!elsewhere.length) return null;
  return (
    <>
      <h3 style={{ margin: "22px 0 8px" }}>Waiting for your yes in other projects</h3>
      <table>{head}<tbody>{elsewhere.map(row)}</tbody></table>
    </>
  );
}

/** The repos a new project grant names, with who owns each. Reports the picked repos up. */
export function GrantRepos({ repos, onChange }: { repos: string[]; onChange: (repos: string[]) => void }) {
  const { data: areas } = usePolling(() => api.areas(), 60000);
  const owner = (repo: string) => areas?.find((a) => a.repo === repo)?.person.name;
  const free = (areas ?? []).map((a) => a.repo).filter((r) => !repos.includes(r));
  return (
    <div className="field">
      <span className="lbl">repos it names</span>
      {repos.length > 0 && (
        <div className="btnrow" style={{ marginBottom: 8 }}>
          {repos.map((r) => (
            <span key={r} className="chip mono">
              {r}{owner(r) && <span className="dim"> · {owner(r)}</span>}
              <button type="button" className="chip-x" aria-label={`remove ${r}`}
                      onClick={() => onChange(repos.filter((x) => x !== r))}>×</button>
            </span>
          ))}
        </div>
      )}
      {free.length > 0 && (
        <Picker value="" onChange={(v) => v && onChange([...repos, v])} ariaLabel="Add a repo"
                options={["", ...free]}
                labels={{ "": "Add a repo", ...Object.fromEntries(free.map((r) => [r, `${r} (${owner(r)})`])) }} />
      )}
      <span className="help">
        {areas && areas.length === 0
          ? <>No repo has an owner yet, so nobody else has to agree. Owners are set under <Link to="/settings?tab=areas">Settings, Areas</Link>.</>
          : "A repo that someone else owns makes the grant wait for that owner to agree."}
      </span>
    </div>
  );
}

