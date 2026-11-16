import { useEffect, useState } from "react";

import { api } from "../../api";
import { Banner, Panel, SkeletonRows, Switch } from "../../components/edition";

// Whether agents that connect over MCP see the building-block tools (TR-757), on the edition kit
// (TR-1392). On by default on a platform cell; off on a factory or AI SRE cell until an admin turns it
// on. Only an admin sees it; the daemon refuses anyone else. Factory Settings > Agent tools and the
// platform Settings page (Access tab) both show this.

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";

const messageOf = (e: unknown) => String((e as Error)?.message ?? e);
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** `title` for a page with no heading of its own (the platform Settings page). */
export function McpToolsPanel({ title }: { title?: string }) {
  const [admin, setAdmin] = useState<boolean>();
  const [on, setOn] = useState<boolean>();
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState<string>();
  useEffect(() => {
    api.whoami()
      .then((w) => {
        const a = w.scopes.includes("admin");
        setAdmin(a);
        return a ? api.mcpSettings().then((s) => setOn(s.object_tools)) : undefined;
      })
      .catch((e) => { setAdmin((a) => a ?? true); setErr(sentence(messageOf(e))); });
  }, []);
  if (!admin) return null;

  const toggle = async (next: boolean) => {
    setBusy(true); setErr(undefined); setSaved(false);
    try { setOn((await api.setMcpObjectTools(next)).object_tools); setSaved(true); }
    catch (e) { setErr(sentence(messageOf(e))); }
    setBusy(false);
  };
  return (
    <Panel title={title} data-testid="mcp-tools">
      <div className="grid gap-4">
        <p className={TEXT}>
          Agents that connect over MCP always get the tools named for what you want done. This adds the building blocks
          underneath: connections, raw data and rules.
        </p>
        {on === undefined ? (!err && <SkeletonRows count={1} />) : (
          <Switch label="Let agents use the building blocks" checked={on} disabled={busy} data-testid="mcp-object-tools"
                  hint="Crew sessions always have them, and project keys lose them while this is off. It only shortens the list: what a key may do is set by its scopes."
                  onCheckedChange={(next) => void toggle(next)} />
        )}
        {saved && !err && (
          <p className="m-0 text-[13px]" role="status" data-testid="mcp-saved">
            Saved. Agents that are already connected see the change when they reconnect.
          </p>
        )}
        {err && <Banner tone="error" data-testid="mcp-error">{err}</Banner>}
      </div>
    </Panel>
  );
}
