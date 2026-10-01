import { Link } from "react-router-dom";

import type { SlackChannels } from "../api";
import { Picker } from "./bits";

/** The Slack channel an agent posts to: the channels the cell's bot is in, or why there are none
 *  (Slack not connected: where to connect it; the list unavailable: type the channel's ID). */
export function SlackPick({ slack, value, onChange, disabled, emptyLabel = "Pick a channel" }: {
  slack: SlackChannels | undefined; value: string; onChange: (id: string) => void; disabled?: boolean;
  emptyLabel?: string;   // what no channel means here ("Pick a channel", or "No channel: console only")
}) {
  if (!slack) return <p className="help">Reading your Slack channels…</p>;
  if (slack.reason === "no_token") {
    return (
      <p className="help">
        Slack is not connected to Tares yet. <Link to="/settings">Connect it under Settings, Slack</Link>, then pick the channel here.
      </p>
    );
  }
  if (!slack.channels.length) {
    return (
      <label className="field">
        <span className="help">
          {slack.reason === "missing_scope" ? "Tares cannot list your channels." : "No channel to pick from: invite the Tares bot to a channel, or type its ID."}
        </span>
        <input type="text" value={value} placeholder="C0123456789" disabled={disabled} aria-label="Slack channel ID"
               onChange={(e) => onChange(e.target.value.trim())} />
      </label>
    );
  }
  const options = ["", ...slack.channels.map((c) => c.id)];
  if (value && !options.includes(value)) options.push(value);
  const labels: Record<string, string> = { "": emptyLabel };
  for (const c of slack.channels) labels[c.id] = c.is_private ? `${c.name} (private)` : `#${c.name}`;
  return <Picker value={value} options={options} labels={labels} ariaLabel="Slack channel" onChange={onChange} disabled={disabled} />;
}
