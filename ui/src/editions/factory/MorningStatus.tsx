import * as React from "react";
import { Link } from "react-router-dom";

import { Button, Checkbox, Select } from "../../components/edition";
import { Input } from "../../components/edition";
import { SlackPick } from "../../components/SlackPick";
import { Switch } from "../../components/ui/switch";
import type { Person } from "../../types";
import type { MorningResult, MorningStatus } from "./FactorySettings.api";

// Settings, Crew, Morning status: the standup report, sent by the cell itself on weekday mornings
// to Slack, by email, or both. Off until someone turns it on. Email is Tares Cloud only, so on a
// self-hosted cell its box says that and stays off; Slack not connected only rules out Slack.

export const MORNING_OFF: MorningStatus = {
  enabled: false, to_slack: false, to_email: false, channel: "", recipients: [], time: "09:00", timezone: "",
  email_available: false, last_result: "",
};

const here = () => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";

function zones(current: string): { value: string; label: string }[] {
  const all = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] }).supportedValuesOf?.("timeZone") ?? ["UTC"];
  const list = all.includes(current) ? all : [current, ...all];
  return list.map((z) => ({ value: z, label: z.replace(/_/g, " ") }));
}

/** What keeps it from being turned on, or nothing when at least one place has what it needs. */
export function missingFor(d: MorningStatus, slackConnected: boolean, emailAvailable: boolean): string {
  const email = d.to_email && emailAvailable;
  if (!d.to_slack && !email) return "Choose where to send it: Slack, Email, or both.";
  if ((d.to_slack && slackConnected && d.channel) || (email && d.recipients.length > 0)) return "";
  if (d.to_slack && !slackConnected) return emailAvailable ? "Connect Slack, or send it by email instead." : "Connect Slack first.";
  if (d.to_slack && !d.channel) return "Pick a Slack channel to send it to.";
  return "Pick at least one person to email.";
}

const LOWER = (s: string) => s.toLowerCase();
const TICK = "data-[state=unchecked]:bg-[var(--input-bg)]!";

export function MorningStatusPane({ value, slackConnected, people, dim, onSave, onSendNow }: {
  value: MorningStatus;
  slackConnected: boolean;
  /** Everyone on the workspace; null until the list has loaded. */
  people: Person[] | null;
  dim: boolean;
  onSave: (m: MorningStatus) => Promise<string | null>;
  /** Sends one report on the saved settings; null when nothing was sent (a sample). */
  onSendNow: () => Promise<MorningResult | null>;
}) {
  const [draft, setDraft] = React.useState<MorningStatus>({ ...value, timezone: value.timezone || here() });
  const [busy, setBusy] = React.useState<"save" | "send" | null>(null);
  const [note, setNote] = React.useState("");
  const [error, setError] = React.useState("");
  const [sent, setSent] = React.useState<MorningResult | null>(null);
  const emailAvailable = value.email_available;
  const off = dim || (!slackConnected && !emailAvailable);
  const missing = missingFor(draft, slackConnected, emailAvailable);
  const blocked = draft.enabled && !!missing;
  const canSend = !off && !missingFor({ ...draft, enabled: true }, slackConnected, emailAvailable);
  const emails = (people ?? []).filter((p) => (p.email ?? "").trim());
  const ticked = new Set(draft.recipients.map(LOWER));
  const lastResult = sent?.result ?? value.last_result;

  // a box that shows off because the cell has no email is not asked for, whatever was saved
  const toSave = (): MorningStatus => ({ ...draft, to_email: draft.to_email && emailAvailable });
  const set = (m: Partial<MorningStatus>) => { setNote(""); setError(""); setDraft((d) => ({ ...d, ...m })); };
  const tick = (email: string, on: boolean) => {
    const rest = draft.recipients.filter((r) => LOWER(r) !== LOWER(email));
    set({ recipients: on ? [...rest, email] : rest });
  };

  const save = async () => {
    setBusy("save"); setNote(""); setError(""); setSent(null);
    const failed = await onSave(toSave());
    setBusy(null);
    if (failed) setError(failed);
    else setNote(draft.enabled ? `Saved. It goes out every weekday at ${draft.time}.` : "Saved. Morning status is off.");
  };

  const sendNow = async () => {
    setBusy("send"); setNote(""); setError(""); setSent(null);
    const failed = await onSave(toSave());
    if (failed) { setBusy(null); setError(failed); return; }
    try {
      const result = await onSendNow();
      setSent(result);
    } catch (e) {
      setError(String((e as Error).message ?? e));
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="grid min-w-0 gap-4" data-testid="morning-status">
      <div className="min-w-0">
        <h2 className="m-0! text-xl!">Morning status</h2>
        <p className="m-0! text-[13px] text-muted-foreground">A short report each weekday morning: what got done, what is moving, and what is waiting for you.</p>
      </div>

      <div className={"border border-border bg-card" + (off ? " opacity-60" : "")}>
        <div className="flex items-center justify-between gap-4 border-b border-border px-4 py-3.5">
          <div className="min-w-0">
            <div className="font-semibold">Send the morning status</div>
            <div className="mt-0.5 text-[13px] text-muted-foreground" data-testid="morning-summary">
              {draft.enabled ? `Every weekday at ${draft.time}, ${draft.timezone.replace(/_/g, " ")}. Monday's covers the weekend.` : "Off. Nothing is sent."}
            </div>
          </div>
          <Switch aria-label="Send the morning status" checked={draft.enabled} disabled={off}
                  onCheckedChange={(v) => set({ enabled: v })} data-testid="morning-toggle" />
        </div>
        <div className="grid gap-4 px-4 py-4">
          <div className="grid gap-2.5" data-testid="morning-send-to">
            <div className="text-sm font-medium">Send to</div>

            <div className="grid gap-2">
              <Checkbox label="Slack" className={TICK} checked={draft.to_slack}
                        disabled={off || (!slackConnected && !draft.to_slack)}
                        onCheckedChange={(v) => set({ to_slack: v === true })} data-testid="morning-to-slack" />
              {!slackConnected && (
                <div className="ml-7 text-[13px] text-muted-foreground" data-testid="morning-no-slack">
                  <b className="text-foreground">Connect Slack first.</b> <Link to="/settings?tab=slack" className="text-[color:var(--accent-deep)]! underline!">Connect Slack</Link>
                </div>
              )}
              {draft.to_slack && slackConnected && (
                <div className="ml-7 grid gap-1.5">
                  <SlackPick value={draft.channel} onChange={(c) => set({ channel: c })} disabled={off} emptyLabel="Pick a channel" />
                  {!draft.channel && <div className="text-[13px] text-[color:var(--err)]" data-testid="morning-need-channel">Pick a channel to send it to.</div>}
                </div>
              )}
            </div>

            <div className="grid gap-2">
              <Checkbox label="Email" className={TICK} checked={draft.to_email && emailAvailable}
                        disabled={off || !emailAvailable}
                        hint={emailAvailable ? undefined : "Not available on this cell"}
                        onCheckedChange={(v) => set({ to_email: v === true })} data-testid="morning-to-email" />
              {draft.to_email && emailAvailable && (
                <div className="ml-7 grid gap-1.5" data-testid="morning-recipients" data-user-text>
                  {people === null && <div className="text-[13px] text-muted-foreground">Reading the people on this workspace…</div>}
                  {people !== null && emails.length === 0 && <div className="text-[13px] text-muted-foreground" data-testid="morning-no-people">Nobody here has an email address yet.</div>}
                  <div className="grid gap-1.5 sm:grid-cols-2">
                    {emails.map((p) => (
                      <Checkbox key={p.id} label={p.name || (p.email as string)} className={TICK}
                                hint={p.name ? (p.email as string) : undefined} disabled={off}
                                checked={ticked.has(LOWER(p.email as string))}
                                onCheckedChange={(v) => tick(p.email as string, v === true)}
                                data-testid="morning-recipient" />
                    ))}
                  </div>
                </div>
              )}
              {draft.to_email && emailAvailable && !draft.recipients.length && people !== null && emails.length > 0 && (
                <div className="ml-7 text-[13px] text-[color:var(--err)]" data-testid="morning-need-people">Pick at least one person to email.</div>
              )}
            </div>
          </div>

          <div className="grid gap-4 sm:grid-cols-[160px_minmax(0,1fr)]">
            <Input label="Time" type="time" value={draft.time} disabled={off} onChange={(e) => set({ time: e.target.value })} data-testid="morning-time" />
            <Select label="Time zone" options={zones(draft.timezone)} value={draft.timezone} disabled={off}
                    onValueChange={(z) => set({ timezone: z })} data-testid="morning-zone" />
          </div>

          {blocked && <div className="text-[13px] text-[color:var(--err)]" role="alert" data-testid="morning-missing">{missing}</div>}

          <div className="flex flex-wrap items-center gap-3">
            <Button busy={busy === "save"} disabled={off || blocked || !draft.time || busy !== null} onClick={() => void save()} data-testid="morning-save">Save</Button>
            <Button variant="secondary" busy={busy === "send"} disabled={!canSend || !draft.time || busy !== null} onClick={() => void sendNow()} data-testid="morning-send-now">Send one now</Button>
            {note && <span className="text-[13px] text-muted-foreground" role="status" data-testid="morning-note">{note}</span>}
            {error && <span className="text-[13px] text-[color:var(--err)]" role="alert" data-testid="morning-error">{error}</span>}
          </div>
          {lastResult && <div className="text-[13px] text-muted-foreground" data-testid="morning-last-result">{lastResult}</div>}
        </div>
      </div>
    </div>
  );
}
