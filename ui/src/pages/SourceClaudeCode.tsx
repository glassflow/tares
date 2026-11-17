import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { usePolling } from "../components/bits";
import { Banner, Button, CopyLine, Panel, StatusLabel } from "../components/edition";

const INSTALL = "/plugin marketplace add glassflow/tares\n/plugin install tares@tares";

/** Connect Claude Code (TR-1394). Claude Code sends its sessions to Tares through the Tares plugin, so
 *  this page gives the install commands and this Tares's own address, the same steps local or remote.
 *  It watches the connections list and says when the first session has arrived. */
export default function SourceClaudeCode() {
  const origin = window.location.origin;
  const { data: sources } = usePolling(() => api.sources(), 10000);
  // undefined while asking, or when the answer never came: the page then can't say whether a key is needed.
  const [keyNeeded, setKeyNeeded] = useState<boolean>();

  useEffect(() => {
    api.health().then((h) => setKeyNeeded(h.auth_required), () => setKeyNeeded(undefined));
  }, []);

  const connection = sources?.find((s) => s.connector === "claude_code");
  const to = connection ? `/sources/${encodeURIComponent(connection.name)}` : undefined;

  return (
    <div className="grid max-w-[880px] gap-6" data-testid="claude-code-page">
      <header>
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Connect Claude Code</h1>
        <p className="m-0 max-w-[62ch] text-muted-foreground">
          Install the Tares plugin in Claude Code. It sends each session to Tares as it happens, with its
          repository, branch and model, and lets Claude Code look things up in Tares. Secrets are removed
          before anything is kept. The steps are the same whether Tares is on your machine or remote.
        </p>
      </header>

      {connection && to && (
        <Banner data-testid="claude-code-connected"
                action={<Button variant="secondary" asChild><Link to={to}>Open the connection</Link></Button>}>
          Claude Code is connected. Its sessions arrive in{" "}
          <span className="font-mono" data-user-text>{connection.name}</span>.
        </Banner>
      )}

      <ol className="m-0 grid list-none gap-4 p-0">
        <li>
          <Panel title="1. Install the plugin" data-testid="claude-code-install">
            <p className="m-0 mb-3 text-[13px] text-muted-foreground">In Claude Code, type these two commands:</p>
            <CopyLine text={INSTALL} what="commands" multiline data-testid="claude-code-install-commands" />
          </Panel>
        </li>
        <li>
          <Panel title="2. Point it at this Tares" data-testid="claude-code-point">
            <p className="m-0 mb-3 text-[13px] text-muted-foreground">When the installer asks for the address of Tares, give it this one:</p>
            <CopyLine text={origin} what="address" data-testid="claude-code-address" />
            <p className="m-0 mt-4 text-[13px] text-muted-foreground" data-testid="claude-code-key">
              <KeyWords needed={keyNeeded} />
            </p>
          </Panel>
        </li>
        <li>
          <Panel title="3. Start a session" data-testid="claude-code-start"
                 action={connection
                   ? <StatusLabel tone="done" data-testid="claude-code-state">Receiving sessions</StatusLabel>
                   : <StatusLabel tone="waiting" data-testid="claude-code-state">Waiting for the first session</StatusLabel>}>
            <p className="m-0 text-[13px] text-muted-foreground">
              Sessions arrive from the moment the plugin is installed; earlier ones are not brought in. The first
              one adds a Claude Code connection to <Link to="/sources" className="text-foreground underline underline-offset-2">Connections</Link>,
              and this page says so.
            </p>
          </Panel>
        </li>
      </ol>
    </div>
  );
}

/** What to give the installer when it asks for a key. When Tares couldn't say whether it needs one, the
 *  page says what to do if asked rather than guessing either way. */
function KeyWords({ needed }: { needed: boolean | undefined }) {
  const make = (
    <>
      make one in <Link to="/settings?tab=keys" className="text-foreground underline underline-offset-2">Settings</Link>{" "}
      that can <b className="font-medium text-foreground">Read</b> and <b className="font-medium text-foreground">Send data</b>, and paste it in.
      With it the plugin sends sessions and looks things up, without admin rights.
    </>
  );
  if (needed === true) return <>It also asks for a key. This Tares needs one: {make}</>;
  if (needed === false) return <>It also asks for a key. This Tares is open, so leave it empty.</>;
  return <>If it also asks for a key, {make}</>;
}
