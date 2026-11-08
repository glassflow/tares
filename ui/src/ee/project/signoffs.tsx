// Tares enterprise licence, see ee/LICENSE
import { TimeAgo, usePolling } from "../../components/bits";
import Fold from "../../components/project/Fold";
import type { Handover } from "../../types";
import { projectQuestions } from "../api";

// What the team adds to a project's overview: the questions a spec put to the owners of the repos
// it changes (TR-636), and the hand-overs the lead pushed through before every owner signed off.

/** "Questions for the team": open ones first, each group oldest first (the API orders by when asked). */
export function TeamQuestions({ project }: { project: string }) {
  const { data } = usePolling(() => projectQuestions(project), 15000);
  const rows = [...(data?.questions ?? [])].sort((a, b) => (a.state === "open" ? 0 : 1) - (b.state === "open" ? 0 : 1));
  if (!rows.length) return null;
  const open = rows.filter((q) => q.state === "open").length;
  return (
    <Fold id="questions" title="Questions for the team" pinnedByDefault meta={`${open} open · ${rows.length - open} answered`}>
      <ul className="fo-docs">
        {rows.map((q) => (
          <li key={q.id} style={{ display: "block" }}>
            <b>{q.id}</b> For {q.to_person?.name ?? "the area owner"} · <span className="mono">{q.repo}</span>
            <div>{q.text}</div>
            {q.state === "answered" ? (
              <small style={{ display: "block" }}>
                Answered by {q.answered_by_person?.name ?? q.answered_by ?? "the area owner"}
                {q.answered_at && <> <TimeAgo ts={q.answered_at} /></>}: {q.answer}
              </small>
            ) : (
              <small style={{ display: "block" }}>Asked <TimeAgo ts={q.asked_at} /></small>
            )}
          </li>
        ))}
      </ul>
    </Fold>
  );
}

/** "Early hand-overs": each override of the area owners' sign-off, with the reason and who did it. */
export function SignOffs({ handover }: { handover?: Handover | null }) {
  const overrides = handover?.overrides?.length ? handover.overrides : handover?.override ? [handover.override] : [];
  if (!overrides.length) return null;
  return (
    <Fold id="signoffs" title="Early hand-overs" meta="" pinnedByDefault>
      <ul className="fo-docs">
        {overrides.map((o, i) => (
          <li key={`${o.at}-${i}`} style={{ display: "block" }}>
            Handed over before every area owner signed off: {o.reason}
            <small style={{ display: "block" }}>
              {o.by_label ?? o.person?.name ?? "the project lead"} · <TimeAgo ts={o.at} />
            </small>
          </li>
        ))}
      </ul>
    </Fold>
  );
}
