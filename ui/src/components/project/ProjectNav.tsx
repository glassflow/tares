import { SETTINGS, VLink, sameView, sourceState, viewParam, type Ctx, type View } from "./common";
import { handedBy } from "../../pages/AgentDetail";

// The full setup's navigation: back to the project's Overview, the views that are always there
// (Activity, Events), one group per kind of object, filled with the project's real objects, and
// Settings. On a narrow screen it folds into one select.

interface Entry { v: View; label: string; dot?: "ok" | "error" | "paused"; hint?: string; title?: string }
interface Group { head: View; label: string; add?: { extra: Record<string, string>; label: string }; items: Entry[] }

function groups(ctx: Ctx): Group[] {
  const all = ctx.agentsData?.agents ?? [];
  const out: Group[] = [
    {
      head: { kind: "sources" }, label: "Sources",
      add: { extra: { add: "1" }, label: "Add a source" },
      items: ctx.mySources.map((x) => {
        const st = sourceState(x);
        return { v: { kind: "source", name: x.name }, label: x.name, dot: st,
                 title: st === "error" ? x.health?.last_error ?? "error" : st };
      }),
    },
    {
      head: { kind: "triggers" }, label: "Triggers",
      add: { extra: { add: "1" }, label: "Add a trigger" },
      items: ctx.triggers.map((t) => ({ v: { kind: "trigger", name: t.name }, label: t.name,
                                         hint: t.paused ? "paused" : undefined })),
    },
    {
      head: { kind: "agents" }, label: "Agents",
      add: ctx.triggers.length ? { extra: { add: "1" }, label: "Add an agent" } : undefined,
      items: [
        ...ctx.agents.map((a): Entry => {
          const handoff = !a.enabled && handedBy(all, a.name).length > 0;
          return { v: { kind: "agent", name: a.name }, label: a.name,
                   hint: handoff ? "handoff" : a.enabled ? undefined : "off",
                   title: handoff ? "runs only when another agent hands off to it" : a.enabled ? undefined : "disabled" };
        }),
        ...(ctx.externals ?? []).map((x): Entry => ({
          v: { kind: "external", name: x.subscription_id }, label: x.name, hint: "external",
          title: "an agent outside Tares" })),
      ],
    },
    {
      head: { kind: "skills" }, label: "Skills",
      add: { extra: { add: "1" }, label: "Add a skill" },
      items: (ctx.skills ?? []).map((sk) => ({ v: { kind: "skill", name: sk.name }, label: sk.name })),
    },
  ];
  return out;
}

export default function ProjectNav({ ctx }: { ctx: Ctx }) {
  const gs = groups(ctx);
  const top: Entry[] = [
    { v: { kind: "activity" }, label: "Activity" },
    { v: { kind: "events" }, label: "Events" },
    ...(ctx.s.sessions ? [{ v: { kind: "sessions" }, label: "Sessions" } as Entry] : []),
  ];
  const settings: Entry[] = SETTINGS.map(([k, label]) => ({ v: { kind: "settings", name: k }, label }));
  const cur = ctx.view;
  const on = (v: View) => sameView(v, cur);

  const link = (e: Entry, cls: string) => (
    <VLink key={viewParam(e.v)} v={e.v} className={`pnav-link ${cls}${on(e.v) ? " active" : ""}`}
           title={e.title} ariaCurrent={on(e.v)}>
      {e.dot && <span className={`pnav-dot ${e.dot}`} aria-hidden="true" />}
      <span className="pnav-label">{e.label}</span>
      {e.hint && <span className="pnav-hint">{e.hint}</span>}
    </VLink>
  );

  // the narrow-screen select: every entry, grouped as in the list
  const optionLabel = (e: Entry) => e.label + (e.hint ? ` (${e.hint})` : e.dot && e.dot !== "ok" ? ` (${e.dot})` : "");
  return (
    <>
      <VLink v={{ kind: "overview" }} className="pnav-back pnav-back-narrow">
        <span aria-hidden="true">←</span> Back to the project</VLink>
      <nav className="pnav" aria-label="Project setup">
        <VLink v={{ kind: "overview" }} className="pnav-link pnav-back">
          <span aria-hidden="true">←</span><span className="pnav-label">Back to the project</span></VLink>
        {top.map((e) => link(e, "pnav-top"))}
        {gs.map((g) => (
          <div className="pnav-group" key={g.label}>
            <div className="pnav-head">
              <VLink v={g.head} className={`pnav-link pnav-title${on(g.head) ? " active" : ""}`} ariaCurrent={on(g.head)}>
                <span className="pnav-label">{g.label}</span>
              </VLink>
              {g.add && (
                <VLink v={g.head} extra={g.add.extra} className="pnav-add" title={g.add.label} ariaLabel={g.add.label}>+</VLink>
              )}
            </div>
            {g.items.map((e) => link(e, "pnav-item"))}
          </div>
        ))}
        <div className="pnav-group">
          <div className="pnav-head"><span className="pnav-title pnav-static">Settings</span></div>
          {settings.map((e) => link(e, "pnav-item pnav-plain"))}
        </div>
      </nav>
      <label className="pnav-select">
        <span className="lbl">show</span>
        <select value={viewParam(cur)} onChange={(e) => {
          const raw = e.target.value;
          const i = raw.indexOf(":");
          ctx.go(i < 0 ? { kind: raw as View["kind"] } : { kind: raw.slice(0, i) as View["kind"], name: raw.slice(i + 1) });
        }}>
          {top.map((e) => <option key={viewParam(e.v)} value={viewParam(e.v)}>{e.label}</option>)}
          {gs.map((g) => (
            <optgroup key={g.label} label={g.label}>
              <option value={viewParam(g.head)}>All {g.label.toLowerCase()}</option>
              {g.items.map((e) => <option key={viewParam(e.v)} value={viewParam(e.v)}>{optionLabel(e)}</option>)}
            </optgroup>
          ))}
          <optgroup label="Settings">
            {settings.map((e) => <option key={viewParam(e.v)} value={viewParam(e.v)}>{e.label}</option>)}
          </optgroup>
        </select>
      </label>
    </>
  );
}
