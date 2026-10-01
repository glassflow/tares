import { useState } from "react";

import { api } from "../../api";
import ConfirmDialog from "../ConfirmDialog";

/** Delete a draft project: nothing of it is set up yet, so only the draft and its plan go. */
export function DeleteDraft({ id, name, onDeleted, className }: {
  id: string; name: string; onDeleted: () => void; className?: string;
}) {
  const [asking, setAsking] = useState(false);
  const [err, setErr] = useState<string>();
  return (
    <>
      <button type="button" className={className ?? "danger"} onClick={() => { setErr(undefined); setAsking(true); }}>
        Delete draft
      </button>
      {err && <span className="help" style={{ color: "var(--err)" }}>{err}</span>}
      {asking && (
        <ConfirmDialog title={`Delete the draft ${name}?`}
          message="Its goal and plan go. Nothing else is touched: nothing of it was set up yet."
          confirmLabel="Delete" danger
          onConfirm={async () => {
            setAsking(false);
            try { await api.deleteProject(id); onDeleted(); }
            catch (e) { setErr(String((e as Error).message ?? e)); }
          }}
          onCancel={() => setAsking(false)} />
      )}
    </>
  );
}
