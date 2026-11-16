import { useContext, useEffect, useRef, useState } from "react";

import { api, type AgentLimits } from "../../api";
import { Banner, Button, Input, Panel, SkeletonRows } from "../../components/edition";
import { cn } from "@/lib/utils";
import { TerseSettings } from "./AccessPanels";

// How many times each agent may start in a day (TR-325), on the edition kit (TR-1392). A guard against a runaway trigger,
// not a bill: spend is bounded by each agent's budget. Saved here it replaces the deployment's value;
// cleared, that comes back. `agent` is who the person came for (a project's "Raise the limit", TR-670):
// the field is scrolled into view and marked, since the one number applies to every agent without its
// own limit. Factory Settings > Budget and the platform Settings page (Limits tab) both show this.

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";
const MAX = 10000;

const messageOf = (e: unknown) => String((e as Error)?.message ?? e);
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function origin(st: AgentLimits): string {
  if (st.daily_cap_source === "console") return "saved here";
  if (st.daily_cap_source === "env") return `from ${st.env} in the daemon's environment`;
  return "the default";
}

export function AgentLimitsPanel({ agent }: { agent?: string }) {
  const terse = useContext(TerseSettings);
  const [st, setSt] = useState<AgentLimits>();
  const field = useRef<HTMLDivElement>(null);
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadErr, setLoadErr] = useState<string>();
  const [fieldErr, setFieldErr] = useState<string>();
  const [msg, setMsg] = useState<string>();

  const load = () => api.agentLimits()
    .then((s) => { setSt(s); setValue(String(s.daily_cap)); setLoadErr(undefined); })
    .catch((e) => setLoadErr(sentence(messageOf(e))));
  useEffect(() => { void load(); }, []);
  useEffect(() => {
    if (agent && st) field.current?.scrollIntoView({ block: "center" });
  }, [agent, !!st]);

  const save = async (daily_cap: string | null, done: string) => {
    if (daily_cap !== null) {
      const n = Number(daily_cap);
      if (!Number.isInteger(n) || n < 1 || n > MAX) { setFieldErr("Give a whole number from 1 to 10,000."); return; }
    }
    setBusy(true); setFieldErr(undefined); setMsg(undefined);
    try { await api.setAgentLimits({ daily_cap }); setMsg(done); await load(); }
    catch (e) { setFieldErr(sentence(messageOf(e))); }
    setBusy(false);
  };

  return (
    <Panel title="Agent limits" data-testid="agent-limits">
      <div className="grid gap-4">
        {!terse && (
          <p className={TEXT}>
            How many times each Tares agent may start work in 24 hours. Once an agent reaches it, anything more that would
            start it is recorded as capped and costs nothing until the window moves on. An agent set to start every 10
            minutes starts 144 times a day. Spend is bounded separately by each agent's budget.
          </p>
        )}
        {!st ? (loadErr ? <Banner tone="error">{loadErr}</Banner> : <SkeletonRows count={1} />) : (
          <form className="grid gap-4" noValidate onSubmit={(e) => { e.preventDefault(); void save(value.trim(), "Saved. It applies the next time an agent starts."); }}>
            <div ref={field} data-testid="daily-cap-field" data-came-for={agent ? "" : undefined}
                 className={cn("grid max-w-sm gap-2", agent && "border border-primary bg-muted/40 p-3")}>
              <Input label="Starts per agent per day" type="number" inputMode="numeric" min={1} max={MAX} value={value}
                     disabled={busy} error={fieldErr} hint={`Now ${st.daily_cap}, ${origin(st)}.`} data-testid="daily-cap"
                     onChange={(e) => { setValue(e.target.value); setFieldErr(undefined); setMsg(undefined); }} />
              {agent && (
                <p className="m-0 text-[13px] leading-relaxed">
                  <span data-user-text>{agent}</span> stopped at this limit. Raise it here and <span data-user-text>{agent}</span>{" "}
                  starts again the next time something calls on it; the number applies to every agent without its own limit.
                </p>
              )}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button type="submit" busy={busy} disabled={value.trim() === String(st.daily_cap)} data-testid="daily-cap-save">Save</Button>
              {st.daily_cap_source === "console" && (
                <Button type="button" variant="secondary" disabled={busy} data-testid="daily-cap-clear"
                        onClick={() => void save(null, "Cleared. The deployment's value or the default applies.")}>
                  Use the deployment's value or the default
                </Button>
              )}
            </div>
            {msg && <p className="m-0 text-[13px]" role="status" data-testid="daily-cap-saved">{msg}</p>}
          </form>
        )}
      </div>
    </Panel>
  );
}
