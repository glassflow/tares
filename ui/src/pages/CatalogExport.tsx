import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";

import { api, ApiError } from "../api";
import type { ConnectorSpec, Source } from "../types";
import { copyText } from "../editions/copyText";
import { useEdition } from "../editions/edition";
import { Banner, Button, Checkbox, EmptyState, ListRow, ListSection, LoadingBlocks, Panel, StatusLabel, Switch } from "../components/edition";

const REDACTED = "••••••••";   // matches connectors.REDACTED_SECRET; a set secret comes back as this
const hasSecret = (s: Source) => Object.values(s.config ?? {}).includes(REDACTED);
const count = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/** Export the connections, with what hangs on them, as one YAML file another Tares imports. The
 *  daemon's defaults (every connection, no secrets) are the agent's and MCP's call; this page adds
 *  picking connections and an opt-in for secrets. The route is admin only on the daemon. */
export default function CatalogExport() {
  const { edition, admin } = useEdition();
  const rules = edition === "sre" ? "check rules" : "rules";
  const [sources, setSources] = useState<Source[]>();
  const [specs, setSpecs] = useState<Record<string, ConnectorSpec>>({});
  const [loadErr, setLoadErr] = useState<string>();
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [includeSecrets, setIncludeSecrets] = useState(false);
  const [yaml, setYaml] = useState<string>();
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();

  useEffect(() => {
    api.sources().then((ss) => { setSources(ss); setPicked(new Set(ss.map((s) => s.name))); },
                       (e) => setLoadErr(String((e as Error).message ?? e)));
    // Only for the kind's label; the list still shows when this fails.
    api.connectors().then(setSpecs, () => {});
  }, []);

  const withSecret = useMemo(
    () => (sources ?? []).filter((s) => picked.has(s.name) && hasSecret(s)).length, [sources, picked]);
  const allPicked = !!sources && picked.size === sources.length;

  // A file made before the picks changed no longer matches them: drop it rather than offer it.
  const pick = (next: Set<string>) => { setPicked(next); setYaml(undefined); };
  const toggle = (name: string, on: boolean) => {
    const n = new Set(picked);
    if (on) n.add(name); else n.delete(name);
    pick(n);
  };

  const run = async () => {
    setBusy(true); setErr(undefined); setYaml(undefined);
    try {
      setYaml(await api.exportYaml({
        sources: allPicked ? undefined : [...picked],   // all picked: the same call as the default
        includeSecrets,
      }));
    } catch (e) {
      setErr(e instanceof ApiError && e.status === 403
        ? "This key can't export. Exporting needs a key that can write."
        : String((e as Error).message ?? e));
    }
    setBusy(false);
  };

  return (
    <div className="grid gap-6" data-testid="export-page">
      <header className="min-w-0">
        <p className="m-0 mb-1 text-[13px] text-muted-foreground"><Link to="/sources">Connections</Link></p>
        <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Export connections</h1>
        <p className="m-0 max-w-[62ch] text-muted-foreground">
          One YAML file of your connections, with the {rules}, agents and projects that go with them. Import it into
          another Tares, or keep it as a copy.
        </p>
      </header>

      {admin === false && (
        <Banner tone="note" data-testid="export-admin-only">
          This key can read but not export. Use a key that can write to make the file.
        </Banner>
      )}

      {loadErr && (
        <Banner tone="error" data-testid="export-load-error">
          Could not load the connections: <span data-user-text>{loadErr}</span>
        </Banner>
      )}

      {!sources && !loadErr && <LoadingBlocks label="Loading connections" />}

      {sources && sources.length === 0 && (
        <EmptyState title="Nothing to export" data-testid="export-empty"
                    action={<Button variant="secondary" asChild><Link to="/sources/new">Add a connection</Link></Button>}>
          This Tares has no connections yet. Add one, or import a file from another Tares.
        </EmptyState>
      )}

      {sources && sources.length > 0 && (
        <>
          <ListSection paper title="Connections" data-testid="export-connections"
                       aside={
                         <Button variant="quiet" data-testid="export-all"
                                 onClick={() => pick(allPicked ? new Set() : new Set(sources.map((s) => s.name)))}>
                           {allPicked ? "Pick none" : "Pick all"}
                         </Button>
                       }>
            <p className="m-0 py-2 text-[13px] text-muted-foreground">
              The {rules} that read only the picked connections come with them.
            </p>
            {sources.map((s) => (
              <ListRow key={s.name} data-testid="export-connection" data-name={s.name}>
                <div data-user-text className="min-w-0 flex-1 basis-48">
                  <Checkbox id={`export-${s.name}`} label={s.name} checked={picked.has(s.name)}
                            onCheckedChange={(v) => toggle(s.name, v === true)} />
                </div>
                <span className="text-[13px] text-muted-foreground">
                  {specs[s.connector]?.label ?? <span className="font-mono" data-user-text>{s.connector}</span>}
                </span>
                {hasSecret(s) && <StatusLabel tone="quiet" data-testid="export-has-secret">Has a secret</StatusLabel>}
              </ListRow>
            ))}
          </ListSection>

          <section className="grid gap-3" data-testid="export-secrets">
            <Switch id="export-include-secrets" data-testid="export-include-secrets" checked={includeSecrets}
                    label="Include secrets (tokens, connection strings)"
                    hint={includeSecrets ? undefined : `Left out by default: enter them again on the other Tares after importing.${
                      withSecret ? ` ${count(withSecret, "picked connection")} ${withSecret === 1 ? "has" : "have"} one.` : ""}`}
                    onCheckedChange={(v) => { setIncludeSecrets(v === true); setYaml(undefined); }} />
            {includeSecrets && (
              <Banner tone="warn" data-testid="export-secrets-warn">
                The file will hold the secrets in plain text. Don't commit it or share it.
              </Banner>
            )}
          </section>

          <div className="flex flex-wrap items-center gap-3">
            <Button onClick={() => void run()} disabled={busy || picked.size === 0 || admin === false} data-testid="export-run">
              {busy ? "Exporting" : picked.size === 0 ? "Pick a connection to export" : `Export ${count(picked.size, "connection")}`}
            </Button>
            <Button variant="quiet" asChild><Link to="/sources/import">Import a file instead</Link></Button>
          </div>

          {err && <Banner tone="error" data-testid="export-error"><span data-user-text>{err}</span></Banner>}

          {yaml !== undefined && <Result yaml={yaml} />}
        </>
      )}
    </div>
  );
}

function Result({ yaml }: { yaml: string }) {
  const [copy, setCopy] = useState<"idle" | "copied" | "failed">("idle");
  const timer = useRef<ReturnType<typeof setTimeout>>();
  useEffect(() => () => clearTimeout(timer.current), []);
  const doCopy = async () => {
    clearTimeout(timer.current);
    try {
      await copyText(yaml);
      setCopy("copied");
      timer.current = setTimeout(() => setCopy("idle"), 1500);
    } catch { setCopy("failed"); }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([yaml], { type: "application/yaml" }));
    const a = document.createElement("a");
    a.href = url; a.download = "tares-connections.yaml"; a.click();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  };
  return (
    <Panel title="The file" data-testid="export-result"
           action={
             <div className="flex flex-wrap gap-2">
               <Button variant="secondary" onClick={() => void doCopy()} data-testid="export-copy">
                 {copy === "copied" ? "Copied" : "Copy"}
               </Button>
               <Button variant="secondary" onClick={download} data-testid="export-download">Download</Button>
             </div>
           }>
      {copy === "failed" && (
        <p className="m-0 mb-2 text-[13px] text-err" role="alert" data-testid="export-copy-failed">
          Could not copy. Download the file, or select the text and copy it by hand.
        </p>
      )}
      <pre data-words-exempt data-testid="export-yaml"
           className="m-0 max-h-[60vh] overflow-auto border border-border bg-background px-3 py-2 font-mono text-[13px]">
        {yaml}
      </pre>
    </Panel>
  );
}
