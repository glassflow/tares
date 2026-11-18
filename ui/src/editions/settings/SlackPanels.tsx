import { useCallback, useContext, useEffect, useState, type ReactNode } from "react";

import { api } from "../../api";
import { type Cloud, cloudLinkHere } from "../../cloud";
import { Banner, Button, Input, Loading, Panel, SkeletonLines, StatusDot } from "../../components/edition";
import { TerseSettings } from "./AccessPanels";

// Slack's settings, on the edition kit (TR-1392). Factory Settings and the platform Settings page
// (which the AI SRE edition opens at /settings/advanced?tab=slack) both show these.

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";
const CODE = "font-mono text-[12px] text-foreground";

const messageOf = (e: unknown) => String((e as Error)?.message ?? e);
/** The daemon's refusal, as a sentence under the field. */
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

type SecretStatus = { configured: boolean; source: string; stored: boolean; env_overrides: boolean };

interface SecretSpec {
  testid: string;
  title: string;
  /** What it is for and where to find it in Slack. */
  about: ReactNode;
  /** The same, on a page that has already said what Slack is for (the factory's Integrations). */
  terseAbout: ReactNode;
  envVar: string;
  /** Slack's own name for it, so the label matches the page the person copies it from. */
  label: string;
  hint: string;
  /** What not having one means. */
  unset: string;
  /** "token" or "secret", for the Clear button and the banner. */
  noun: string;
  status: () => Promise<SecretStatus>;
  save: (value: string) => Promise<unknown>;
  clear: () => Promise<unknown>;
}

/** A secret the daemon stores and never returns. The environment wins over a saved one, as with the
 *  model keys, so a value typed here months ago never overrides the operator's deployment. */
function StoredSecret({ spec }: { spec: SecretSpec }) {
  const [st, setSt] = useState<SecretStatus>();
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState<"save" | "clear">();
  const [loadErr, setLoadErr] = useState<string>();
  const [saveErr, setSaveErr] = useState<string>();
  const { status } = spec;
  const terse = useContext(TerseSettings);

  const load = useCallback(() => status().then((s) => { setSt(s); setLoadErr(undefined); }).catch((e) => setLoadErr(messageOf(e))), [status]);
  useEffect(() => { void load(); }, [load]);

  const save = async () => {
    setBusy("save"); setSaveErr(undefined);
    try {
      await spec.save(value.trim());
      setValue("");
      await load();
    } catch (e) { setSaveErr(sentence(messageOf(e))); }
    setBusy(undefined);
  };
  const clear = async () => {
    setBusy("clear");
    try { await spec.clear(); await load(); }
    catch (e) { setLoadErr(messageOf(e)); }
    setBusy(undefined);
  };

  const fromEnv = st?.source.startsWith("env:");
  return (
    <Panel title={spec.title} data-testid={spec.testid}>
      <div className="grid gap-4">
        <p className={TEXT}>{terse ? spec.terseAbout : spec.about}</p>
        {loadErr && <Banner tone="error">{loadErr}</Banner>}
        {!st ? (!loadErr && <Loading label={`Loading the ${spec.title.toLowerCase()}`}><SkeletonLines widths={["40%"]} /></Loading>) : (
          <>
            <p className="m-0 flex items-baseline gap-2 text-sm" data-testid={`${spec.testid}-state`}>
              <StatusDot tone={st.configured ? "ready" : "quiet"} />
              <span>
                {!st.configured ? (terse ? "Not set yet." : `Not set. ${spec.unset}`)
                  : terse ? "Saved."
                  : fromEnv ? <>Set, from <code className={CODE}>{st.source.slice(4)}</code> in the daemon's environment.</>
                  : "Set, saved here."}
              </span>
            </p>
            {st.env_overrides && st.stored && (
              <Banner tone="warn">
                {`${spec.envVar} in the daemon's environment wins, so the ${spec.noun} saved here is not in use. Remove the variable, or clear the saved ${spec.noun}.`}
              </Banner>
            )}
            <form className="grid max-w-[560px] gap-3" onSubmit={(e) => { e.preventDefault(); if (value.trim()) void save(); }}>
              <Input type="password" autoComplete="off" spellCheck={false} className="font-mono"
                     label={spec.label} hint={spec.hint} error={saveErr}
                     value={value} onChange={(e) => { setValue(e.target.value); setSaveErr(undefined); }} />
              <div className="flex flex-wrap gap-2">
                <Button type="submit" busy={busy === "save"} disabled={!value.trim() || busy === "clear"}>Save</Button>
                {st.stored && <Button variant="secondary" busy={busy === "clear"} disabled={busy === "save"} onClick={() => void clear()}>{`Clear the saved ${spec.noun}`}</Button>}
              </div>
            </form>
          </>
        )}
      </div>
    </Panel>
  );
}

const BOT_TOKEN: SecretSpec = {
  testid: "slack-token",
  title: "Bot token",
  about: (
    <>
      Lets Tares post to Slack, in the channels you pick. Create a Slack app with the bot scopes{" "}
      <code className={CODE}>chat:write</code>, <code className={CODE}>chat:write.public</code> (post to a public channel
      without inviting the app), <code className={CODE}>channels:read</code> and <code className={CODE}>groups:read</code>{" "}
      (list the channels to pick from), and <code className={CODE}>commands</code> (for <code className={CODE}>/tares ask</code>).
      Paste its Bot User OAuth Token here, or set <code className={CODE}>TARES_SLACK_BOT_TOKEN</code> in the daemon's environment.
    </>
  ),
  terseAbout: (
    <>
      The Bot User OAuth Token of your Slack app. The app needs the bot scopes <code className={CODE}>chat:write</code>,{" "}
      <code className={CODE}>chat:write.public</code>, <code className={CODE}>channels:read</code>,{" "}
      <code className={CODE}>groups:read</code> and <code className={CODE}>commands</code>.
    </>
  ),
  envVar: "TARES_SLACK_BOT_TOKEN",
  label: "Bot User OAuth Token",
  hint: "Starts with xoxb-. Once saved it is never shown again, here or through the API.",
  unset: "Slack channels can't be picked until it is.",
  noun: "token",
  status: api.slackTokenStatus,
  save: api.setSlackToken,
  clear: api.clearSlackToken,
};

const SIGNING_SECRET: SecretSpec = {
  testid: "slack-secret",
  title: "Signing secret",
  about: (
    <>
      Lets your team ask Tares from Slack. Point the app's <code className={CODE}>/tares</code> slash command at{" "}
      <code className={CODE}>/api/slack/events</code> on this daemon, then type{" "}
      <code className={CODE}>/tares ask what happened to checkout-svc?</code> in any channel. Paste the app's Signing Secret
      (under Basic Information) here, or set <code className={CODE}>TARES_SLACK_SIGNING_SECRET</code>. Tares checks every
      request from Slack against it and refuses one older than 5 minutes.
    </>
  ),
  terseAbout: (
    <>
      Lets your team answer the crew from Slack. Paste the app's Signing Secret (Basic Information), and point its{" "}
      <code className={CODE}>/tares</code> command at <code className={CODE}>/api/slack/events</code>.
    </>
  ),
  envVar: "TARES_SLACK_SIGNING_SECRET",
  label: "Signing Secret",
  hint: "Once saved it is never shown again, here or through the API.",
  unset: "Tares refuses every request from Slack until it is.",
  noun: "secret",
  status: api.slackSigningSecretStatus,
  save: api.setSlackSigningSecret,
  clear: api.clearSlackSigningSecret,
};

/** A self-hosted daemon: the bot token for posting, the signing secret for /tares ask. */
export function SlackSelfHostedPanels() {
  return (
    <div className="grid min-w-0 gap-4">
      <StoredSecret spec={BOT_TOKEN} />
      <StoredSecret spec={SIGNING_SECRET} />
    </div>
  );
}

function OwnerOnly({ cloud }: { cloud: Cloud }) {
  const email = cloud.current?.owner_email;
  return (
    <p className={TEXT} data-testid="slack-owner-only">
      {email ? <>Only the workspace owner, <span className="text-foreground" data-user-text>{email}</span>, can connect Slack.</>
        : "Only the workspace owner can connect Slack."}
    </p>
  );
}

/** Tares Cloud (TR-364): the control plane runs Slack's install and pushes the token, the signing
 *  secret and the team's name here, so this is one Connect or Disconnect, each a link to the
 *  control plane that comes back to this page. `title` for a page with no heading of its own. */
export function SlackCloudPanel({ cloud, connectUrl, title }: { cloud: Cloud; connectUrl: string; title?: string }) {
  const [st, setSt] = useState<Awaited<ReturnType<typeof api.slackTokenStatus>>>();
  const [err, setErr] = useState<string>();
  useEffect(() => {
    api.slackTokenStatus().then(setSt).catch((e) => setErr(messageOf(e)));
  }, []);
  const canConnect = cloud.isOwner !== false;
  const terse = useContext(TerseSettings);
  return (
    <Panel title={title} data-testid="slack-cloud">
      <div className="grid gap-4">
        {err && <Banner tone="error">{err}</Banner>}
        {!st ? (!err && <Loading label="Loading Slack"><SkeletonLines widths={["35%", "70%"]} /></Loading>) : st.configured ? (
          <>
            <p className="m-0 font-semibold">
              {st.team?.name ? <>Connected to <span data-user-text>{st.team.name}</span></> : "Connected"}
            </p>
            <p className={TEXT}>
              {terse
                ? <>For a private channel, invite Tares there first with <code className={CODE}>/invite @Tares</code>.</>
                : <>Tares posts what it finds to the channels you pick, and your team can ask it from any channel with{" "}
                  <code className={CODE}>/tares ask</code>. It posts to any public channel; for a private one, add it there first
                  with <code className={CODE}>/invite @Tares</code>.</>}
            </p>
            {canConnect
              ? <div><Button asChild variant="secondary"><a href={cloudLinkHere(connectUrl, { action: "disconnect" })}>Disconnect</a></Button></div>
              : <OwnerOnly cloud={cloud} />}
          </>
        ) : (
          <>
            {!terse && (
              <p className={TEXT}>
                Connect Slack to have Tares post what it finds to a channel, and to let your team ask it from Slack with{" "}
                <code className={CODE}>/tares ask</code>.
              </p>
            )}
            {canConnect
              ? <div><Button asChild><a href={cloudLinkHere(connectUrl)}>Connect Slack</a></Button></div>
              : <OwnerOnly cloud={cloud} />}
          </>
        )}
      </div>
    </Panel>
  );
}
