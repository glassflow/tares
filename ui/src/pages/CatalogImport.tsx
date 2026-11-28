import { useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../api";
import { useWho } from "../editions/who";
import { Banner, Button, Collapse, Dialog, Switch, Textarea } from "../components/edition";

type Counts = Awaited<ReturnType<typeof api.importYaml>>;
const count = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

/** Import a YAML file of connections (the form Export makes) into this Tares. A merge is checked as a
 *  whole before the daemon writes; a replace removes what is here first, and today the daemon does
 *  that before the checks, so the page claims nothing about a failed one. The route is admin only. */
export default function CatalogImport() {
  const { admin } = useWho();
  const rule = "rule";
  const [text, setText] = useState("");
  const [replace, setReplace] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ counts: Counts; replaced: boolean }>();
  const [err, setErr] = useState<string>();

  const run = async () => {
    setConfirming(false); setBusy(true); setErr(undefined); setDone(undefined);
    try {
      const counts = await api.importYaml(text, replace ? "replace" : "merge");
      setDone({ counts, replaced: replace });
      setText("");
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 403
        ? "This key can't import. Importing needs a key that can write."
        : String((e as Error).message ?? e));
    }
    setBusy(false);
  };

  const empty = !text.trim();
  return (
    <div className="grid gap-6" data-testid="import-page">
      <header className="min-w-0">
        <p className="m-0 mb-1 text-[13px] text-muted-foreground"><Link to="/sources">Connections</Link></p>
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Import connections</h1>
        <p className="m-0 max-w-[62ch] text-muted-foreground">
          Paste a YAML file of connections, {rule}s, agents and MCP servers, such as one another Tares exported.
        </p>
      </header>

      <Collapse>{admin === false && (
        <Banner tone="note" data-testid="import-admin-only">
          This key can read but not import. Use a key that can write to import a file.
        </Banner>
      )}</Collapse>

      {done && <Done {...done} rule={rule} />}
      <Collapse>{err && (
        <Banner tone="error" data-testid="import-error">
          Could not import the file: <span data-user-text>{err}</span>
        </Banner>
      )}</Collapse>

      <section className="grid gap-4">
        <Textarea id="import-yaml" data-testid="import-yaml" data-words-exempt label="The file" rows={14}
                  className="min-h-[260px] font-mono text-[13px]" value={text}
                  placeholder={"sources:\n  - name: ...\ntriggers:\n  - name: ...\n    sources: [...]"}
                  hint="Secrets left out of an export are empty here: enter them again after importing."
                  onChange={(e) => { setText(e.target.value); setDone(undefined); }} />

        <Switch id="import-replace" data-testid="import-replace" checked={replace} label="Replace everything"
                hint={replace
                  ? `Removes every connection, ${rule}, agent and MCP server here, and each project's setup, then imports the file.`
                  : "Off: adds what is new and updates what has the same name. The rest stays."}
                onCheckedChange={(v) => setReplace(v === true)} />
      </section>

      <div className="flex flex-wrap items-center gap-3">
        <Button data-testid="import-run" disabled={busy || empty || admin === false}
                onClick={() => (replace ? setConfirming(true) : void run())}>
          {busy ? "Importing" : empty ? "Paste a file to import" : replace ? "Replace with the file" : "Import the file"}
        </Button>
        <Button variant="quiet" asChild><Link to="/sources/export">Export a file instead</Link></Button>
      </div>

      <Dialog open={confirming} onOpenChange={setConfirming} data-testid="import-confirm"
              title="Replace everything?"
              description={`Every connection, ${rule}, agent and MCP server here goes, with each project's setup, and the file takes their place. This can't be undone.`}
              footer={
                <>
                  <Button variant="secondary" onClick={() => setConfirming(false)} data-testid="import-confirm-cancel">Keep them</Button>
                  <Button onClick={() => void run()} data-testid="import-confirm-replace">Replace everything</Button>
                </>
              } />
    </div>
  );
}

function Done({ counts, replaced, rule }: { counts: Counts; replaced: boolean; rule: string }) {
  const parts = [
    count(counts.sources, "connection"),
    count(counts.triggers, rule),
    count(counts.agents, "agent"),
    count(counts.mcp_servers, "MCP server"),
  ];
  const none = counts.sources + counts.triggers + counts.agents + counts.mcp_servers === 0;
  return (
    <Banner tone="note" data-testid="import-done"
            action={<Button variant="secondary" asChild><Link to="/sources">See the connections</Link></Button>}>
      {none
        ? (replaced ? "Replaced everything with an empty file." : "The file had nothing to import.")
        : `${replaced ? "Replaced everything with" : "Imported"} ${parts.slice(0, -1).join(", ")} and ${parts[parts.length - 1]}.`}
    </Banner>
  );
}
