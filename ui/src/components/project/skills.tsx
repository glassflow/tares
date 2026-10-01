import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import { api } from "../../api";
import ConfirmDialog from "../ConfirmDialog";
import SkillsPanel, { LoadedBy, SkillEditor } from "../SkillsPanel";
import { ErrorState, TimeAgo, formatBytes, usePolling } from "../bits";
import { Facts, NotHere, ViewHead, type Ctx } from "./common";

export function SkillsView({ ctx }: { ctx: Ctx }) {
  return (
    <SkillsPanel project={ctx.id}
                 onOpenAgent={(n) => ctx.go({ kind: "agent", name: n })}
                 onOpenSkill={(n) => ctx.go({ kind: "skill", name: n })}
                 onChanged={ctx.refresh}
                 adding={ctx.params.get("add") === "1"}
                 setAdding={(open) => ctx.go({ kind: "skills" }, open ? { add: "1" } : undefined, true)} />
  );
}

/** One skill: what it is for, who loaded it, and its instructions as the agent reads them. */
export function SkillView({ ctx, name }: { ctx: Ctx; name: string }) {
  const { data, error, reload } = usePolling(() => api.skill(ctx.id, name), 30000);
  const [editing, setEditing] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const summary = ctx.skills?.find((sk) => sk.name === name);
  if (error && !data) return ctx.skills && !summary
    ? <NotHere what="skill" name={name} back={{ kind: "skills" }} />
    : <ErrorState error={error} what="this skill" onRetry={reload} />;
  if (!data) return <div className="dim">loading…</div>;
  return (
    <>
      <ViewHead title={<span className="mono">{data.name}</span>} sub={data.description}>
        {!editing && <>
          <button className="primary" onClick={() => setEditing(true)}>Edit</button>
          <button className="danger" onClick={() => setConfirmDel(true)}>Remove</button>
        </>}
      </ViewHead>
      {editing ? (
        <SkillEditor project={ctx.id} initial={{ name: data.name, description: data.description, body: data.body }}
                     onSaved={() => { setEditing(false); reload(); ctx.refresh(); }}
                     onCancel={() => setEditing(false)} />
      ) : (
        <>
          <Facts rows={[
            ["loaded by", <><LoadedBy names={summary?.loaded_by ?? []} onOpenAgent={(n) => ctx.go({ kind: "agent", name: n })} />
              <span className="help"> · in the last 7 days</span></>],
            ["size", <>{formatBytes(summary?.size ?? new TextEncoder().encode(data.body).length)}
              {summary && <span className="help"> · updated <TimeAgo ts={summary.updated_at} /></span>}</>],
          ]} />
          <div className="panel md" style={{ marginTop: 14 }}>
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{data.body}</ReactMarkdown>
          </div>
        </>
      )}
      {confirmDel && (
        <ConfirmDialog title={`Remove skill ${name} from this project?`} danger confirmLabel="Remove"
          message="The project's agents stop seeing it from their next run. Other projects that use it keep it; it is deleted when no project uses it. Runs that loaded it keep the record."
          onCancel={() => setConfirmDel(false)}
          onConfirm={async () => {
            setConfirmDel(false);
            try { await api.deleteSkill(ctx.id, name); ctx.refresh(); ctx.go({ kind: "skills" }, undefined, true); }
            catch (e) { ctx.fail(e); }
          }} />
      )}
    </>
  );
}
