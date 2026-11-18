import { useEffect, useState } from "react";
import { Link } from "react-router-dom";

import { api } from "../api";
import { Banner, Button, Checkbox, Collapse, EmptyState, ListRow, ListSection, LoadingBlocks, StatusLabel } from "../components/edition";
import type { ConnectorSpec, EnvScan, ProposedSource } from "../types";

/** Discover (TR-1394): Tares looks at the Docker Compose containers on its machine and proposes a
 *  connection for each, plus one for a Prometheus it finds. Nothing is made until the person picks.
 *  The scan's `summary` and skip `reason` are the daemon's words, written for MCP as well; a Docker
 *  logs proposal is described here from its config instead, and the rest show the daemon's text
 *  marked as system text. */
export default function SourceDiscover() {
  const [scan, setScan] = useState<EnvScan>();
  const [specs, setSpecs] = useState<Record<string, ConnectorSpec>>({});
  const [error, setError] = useState<string>();
  const [loading, setLoading] = useState(true);
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [results, setResults] = useState<Record<string, string>>();
  const [busy, setBusy] = useState(false);

  const run = async () => {
    setLoading(true); setError(undefined); setResults(undefined);
    try {
      const s = await api.discoverEnvironment("docker");
      setScan(s);
      setPicked(Object.fromEntries(s.proposed_sources.map((p) => [p.name, p.preselect])));
    } catch (e) { setScan(undefined); setError(String((e as Error).message ?? e)); }
    setLoading(false);
  };
  useEffect(() => {
    run();
    // Only for the kind's label; a proposal still shows when this fails.
    api.connectors().then(setSpecs, () => {});
  }, []);

  const create = async () => {
    if (!scan) return;
    setBusy(true);
    const out: Record<string, string> = { ...results };
    for (const p of scan.proposed_sources) {
      if (!picked[p.name] || out[p.name] === "ok") continue;
      try {
        await api.createSource({ name: p.name, connector: p.connector, config: p.config });
        out[p.name] = "ok";
      } catch (e) { out[p.name] = String((e as Error).message ?? e); }
    }
    setResults(out);
    setBusy(false);
  };

  const proposed = scan?.proposed_sources ?? [];
  const chosen = proposed.filter((p) => picked[p.name] && results?.[p.name] !== "ok").length;
  const added = results ? Object.values(results).filter((v) => v === "ok").length : 0;
  const failed = results ? Object.values(results).length - added : 0;

  return (
    <div className="grid gap-6" data-testid="discover-page">
      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <h1 className="m-0 mb-1.5 text-[22px] md:text-[28px]">Discover connections</h1>
          <p className="m-0 max-w-[62ch] text-muted-foreground">
            Tares looks at the Docker containers on the machine it runs on and proposes a connection for
            each. Nothing is added until you pick.
          </p>
        </div>
        <Button variant="secondary" onClick={run} disabled={loading || busy} data-testid="discover-rescan">
          Look again
        </Button>
      </header>

      {loading && <LoadingBlocks label="Looking at Docker" />}

      <Collapse>{!loading && error && (
        <Banner tone="error" data-testid="discover-error"
                action={<Button variant="secondary" asChild><Link to="/sources/new">Add one by hand</Link></Button>}>
          Tares could not read Docker on the machine it runs on: <span data-user-text>{error}</span>
        </Banner>
      )}</Collapse>

      {!loading && scan && (
        <>
          <p className="m-0 text-[13px] text-muted-foreground" data-testid="discover-summary">
            {count(scan.summary.containers, "container")} found, {count(scan.summary.proposed, "connection")} proposed.
          </p>

          <Collapse>{results && failed === 0 && (
            <Banner data-testid="discover-added"
                    action={<Button asChild><Link to="/sources" data-testid="discover-open">Open connections</Link></Button>}>
              Added {count(added, "connection")}. Tares starts on them at once.
            </Banner>
          )}</Collapse>
          <Collapse>{results && failed > 0 && (
            <Banner tone="error" data-testid="discover-failed">
              Added {added} of {added + failed}. The ones that failed say why below; fix them and add again.
            </Banner>
          )}</Collapse>

          {proposed.length === 0 ? (
            <EmptyState title="Nothing to connect" data-testid="discover-empty"
                        action={<Button variant="secondary" asChild><Link to="/sources/new">Add one by hand</Link></Button>}>
              Tares proposes a connection for each container started by Docker Compose, and found none.
            </EmptyState>
          ) : (
            <ListSection paper title="Proposed connections" data-testid="discover-proposed">
              {proposed.map((p) => (
                <Proposal key={p.name} p={p} label={specs[p.connector]?.label} result={results?.[p.name]}
                          checked={!!picked[p.name]} disabled={busy || results?.[p.name] === "ok"}
                          onChange={(v) => setPicked({ ...picked, [p.name]: v })} />
              ))}
            </ListSection>
          )}

          {proposed.length > 0 && !(results && failed === 0) && (
            <div className="flex flex-wrap items-center gap-3">
              <Button onClick={create} disabled={busy || chosen === 0} data-testid="discover-add">
                {busy ? "Adding" : chosen === 0 ? "Pick a connection to add" : `Add ${count(chosen, "connection")}`}
              </Button>
              {results && <Button variant="quiet" asChild><Link to="/sources">Open connections</Link></Button>}
            </div>
          )}

          {scan.skipped.length > 0 && (
            <ListSection paper title="Found, with nothing to connect" data-testid="discover-skipped">
              {scan.skipped.map((s) => (
                <ListRow key={s.service} data-testid="discover-skip">
                  <span className="font-medium" data-user-text>{s.service}</span>
                  <span className="font-mono text-[13px] text-muted-foreground" data-user-text>{s.image}</span>
                  <span className="basis-full text-[13px] text-muted-foreground" data-help="daemon" data-words-exempt>{s.reason}</span>
                </ListRow>
              ))}
            </ListSection>
          )}
        </>
      )}
    </div>
  );
}

function Proposal({ p, label, result, checked, disabled, onChange }: {
  p: ProposedSource; label?: string; result?: string; checked: boolean; disabled: boolean;
  onChange: (v: boolean) => void;
}) {
  const id = `proposal-${p.name}`;
  return (
    <ListRow className="items-start" data-testid="discover-proposal" data-name={p.name}>
      <div className="min-w-0 flex-1 basis-64">
        <div data-user-text>
          <Checkbox id={id} label={p.name} checked={checked} disabled={disabled}
                    aria-describedby={`${id}-about`} onCheckedChange={(v) => onChange(v === true)} />
        </div>
        <p id={`${id}-about`} className="m-0 mt-1 pl-7 text-[13px] text-muted-foreground">
          {label ?? <span className="font-mono" data-user-text>{p.connector}</span>}. <About p={p} />
        </p>
        {result && result !== "ok" && (
          <p className="m-0 mt-1 pl-7 text-[13px] text-err" role="alert" data-testid="discover-proposal-error">
            Not added: <span data-user-text>{result}</span>
          </p>
        )}
      </div>
      {result === "ok" && <StatusLabel tone="done" data-testid="discover-proposal-added">Added</StatusLabel>}
    </ListRow>
  );
}

/** A Docker logs proposal says what it follows from its own config; any other kind shows the scan's text. */
function About({ p }: { p: ProposedSource }) {
  if (p.connector === "docker_logs") {
    const labels = (p.config.labels ?? []) as { name: string; const?: string }[];
    const service = labels.find((l) => l.name === "service")?.const;
    const project = labels.find((l) => l.name === "project")?.const;
    return (
      <>
        Every log line of{" "}
        {service ? <>the <span className="font-mono" data-user-text>{service}</span> service</> : "the container"}
        {project && <> in <span className="font-mono" data-user-text>{project}</span></>}, from container{" "}
        <span className="font-mono" data-user-text>{String(p.config.container ?? "")}</span>.
      </>
    );
  }
  return <span data-help="daemon" data-words-exempt>{p.summary}</span>;
}

const count = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;
