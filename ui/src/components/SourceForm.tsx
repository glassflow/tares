import { useEffect, useMemo, useState } from "react";

import { api } from "../api";
import { Combo, Picker, ruleSummary } from "./bits";
import InfoDialog, { HelpButton } from "./InfoDialog";
import type { ColumnsProposal, ConnectorField, ConnectorSpec, DiscoverProposal, EnvScan, GithubCredential, TestResult } from "../types";

// Structured push connectors know their entity shape, so a fresh source starts with sensible
// label axes (the user can still edit/remove them).
const DEFAULT_LABELS: Record<string, Array<Record<string, unknown>>> = {
  vercel: [{ name: "project", field: "project", primary: true },
           { name: "environment", field: "environment" }, { name: "source", field: "source" }],
  otlp: [{ name: "service", field: "resourceAttributes.service.name", primary: true }],
};

// Connector-appropriate example names; the generic fallback suits metric-ish sources.
const NAME_PLACEHOLDER: Record<string, string> = {
  github: "e.g. my-repo-commits",
  postgres: "e.g. orders-table",
};

// What Discover does, per connector (fallback: the prometheus introspection copy).
const DISCOVER_HINT: Record<string, string> = {
  docker_logs: "list the containers taresd can see, then pick one to fill this form",
  github: "pick the repo above, then Discover its default branch + author labels",
  postgres: "enter the DSN above, then Discover; it lists the tables it can see; pick one and it proposes the cursor, entity key and labels from the columns",
  prometheus: "enter the URL (+ any auth) above, then Discover; it lists the metrics and labels so you can pick what to ingest (by name or by label). No PromQL to write.",
  prometheus_alerts: "enter the URL (+ any auth) above, then Discover; it lists the alerting rules Prometheus already has, and you ingest them as they fire (optionally filtered by severity).",
};

// Postgres form: plain-language field labels + grouping (main poll settings vs collapsed advanced),
// so the internal schema names don't leak into the UI. Fields not in PG_GROUP go in the main group.
const PG_LABEL: Record<string, string> = {
  dsn: "Connection URL",
  table: "Table",
  cursor_column: "How we find new rows",
  time_column: "Event time",
  columns: "Columns to pull",
  limit: "Rows per poll",
};
const PG_GROUP: Record<string, "advanced"> = { limit: "advanced" };
const PROM_LABEL: Record<string, string> = {
  default_key: "Fallback entity key",
};

interface Props {
  connector: string;
  spec: ConnectorSpec;
  initial?: { name: string; type: string; poll: string; config: Record<string, unknown> };
  lockName?: boolean;
  highlight?: string[];   // field names to mark as "yours to fill" (a builder proposal's `needs`)
  submitLabel: string;
  onSubmit: (body: {
    name: string; type: string; connector: string; poll: string; config: Record<string, unknown>;
  }) => Promise<void>;
}

/** Form generated from the connector's spec (GET /api/connectors). string/number fields render
 *  as inputs, json fields as a code textarea. Test runs one poll server-side before saving. */
export default function SourceForm({ connector, spec, initial, lockName, highlight, submitLabel, onSubmit }: Props) {
  const [name, setName] = useState(initial?.name ?? "");
  const type = initial?.type ?? "event_stream";  // ignored by the daemon; derived from connector
  const [poll, setPoll] = useState(initial?.poll ?? spec.poll ?? "5s");
  // reference connector: per-attachment documents (each with its own labels; correlation is label-native)
  const [refAttachments, setRefAttachments] = useState<Attachment[]>(
    ((initial?.config?.attachments as Array<{ name?: string; format?: string; content?: string;
      labels?: Record<string, string> }>) ?? []).map((a) => ({
      name: a.name ?? "", format: a.format ?? "txt", content: a.content ?? "",
      labels: Object.entries(a.labels ?? {}) })));
  // A secret that's already stored comes back from the API as a redaction placeholder, never its
  // real value. Show which secrets are set, but never prefill one — blank means "keep it" (below).
  const secretSet = (f: ConnectorField) => !!f.secret && !!initial?.config?.[f.name];
  const [cleared, setCleared] = useState<Record<string, boolean>>({});
  const [values, setValues] = useState<Record<string, string>>(() => {
    const v: Record<string, string> = {};
    for (const f of spec.fields) {
      if (f.type === "list") continue;   // list fields use `rows`, below
      if (f.secret) { v[f.name] = ""; continue; }   // never prefill a secret
      const cur = initial?.config?.[f.name];
      // a fresh source starts from the connector's default where it has one (a number or a
      // string), so the value the daemon would use is the value on screen
      if (cur === undefined || cur === null)
        v[f.name] = !initial && f.default != null && (f.type === "number" || f.type === "string") ? String(f.default) : "";
      else if (f.type === "json") v[f.name] = JSON.stringify(cur, null, 2);
      else v[f.name] = String(cur);
    }
    return v;
  });
  const [rows, setRows] = useState<Record<string, Array<Record<string, string>>>>(() => {
    const r: Record<string, Array<Record<string, string>>> = {};
    for (const f of spec.fields) {
      if (f.type !== "list") continue;
      const cur = initial?.config?.[f.name];
      r[f.name] = Array.isArray(cur)
        ? (cur as Record<string, unknown>[]).map((row) =>
            Object.fromEntries((f.item ?? []).map((sf) =>
              [sf.name, row?.[sf.name] != null ? String(row[sf.name]) : ""])))
        : [];
    }
    return r;
  });
  const [labelRows, setLabelRows] = useState<LabelRow[]>(() => {
    const l = initial?.config?.labels;
    // structured push connectors get sensible default label axes when creating a fresh source
    return (Array.isArray(l) && l.length) ? labelsToRows(l) : labelsToRows(DEFAULT_LABELS[connector]);
  });
  const [error, setError] = useState<string>();
  const [test, setTest] = useState<TestResult>();
  const [busy, setBusy] = useState(false);
  // For an existing source, what the data actually carries beats the connector's static
  // `provides` list — merge the observed field profile into the labels editor's suggestions,
  // keeping coverage so the dropdown can show how many sampled events carry each field.
  const [observed, setObserved] = useState<{ sampled: number; fields: { name: string; coverage: number }[] }>();
  useEffect(() => {
    if (!initial?.name) return;
    api.sourceFields(initial.name)
      .then((p) => setObserved({ sampled: p.sampled,
                                 fields: p.fields.map((f) => ({ name: f.name, coverage: f.coverage })) }))
      .catch(() => {});
  }, [initial?.name]);
  const coverage = new Map((observed?.fields ?? []).map((f) => [f.name, f.coverage]));
  const labelFieldOpts = Array.from(new Set([
    ...(spec.provides ?? []).map((p) => p.name),
    ...(observed?.fields ?? []).map((f) => f.name),
  ])).sort((a, b) => (coverage.get(b) ?? -1) - (coverage.get(a) ?? -1) || a.localeCompare(b));
  const labelFieldHints = observed
    ? Object.fromEntries(labelFieldOpts
        .filter((n) => coverage.has(n))
        .map((n) => [n, `${coverage.get(n)} / ${observed.sampled} events`]))
    : undefined;
  const [proposal, setProposal] = useState<DiscoverProposal>();
  const [colProposal, setColProposal] = useState<ColumnsProposal>();
  const [pgColumns, setPgColumns] = useState<string[]>();  // postgres: discovered column names, for the picker
  const [editConn, setEditConn] = useState(false);         // postgres: connection collapsed after discover unless editing
  const [tables, setTables] = useState<string[]>();
  const [catalog, setCatalog] = useState<{ metrics: string[]; labels: string[] }>();  // prometheus: pickers
  const [basket, setBasket] = useState<Set<string>>(new Set());                       // prometheus: chosen metrics
  const [metricsConfirmed, setMetricsConfirmed] = useState(false);                    // prometheus: basket collapsed
  // prometheus_alerts: the configured rules + severity filter, curation collapsed after confirm
  const [alertDiscover, setAlertDiscover] = useState<{ rules: AlertRule[]; severities: string[];
    proposed_config: Record<string, unknown>; summary: string }>();
  const [alertSev, setAlertSev] = useState<Set<string>>(new Set());
  const [alertsConfirmed, setAlertsConfirmed] = useState(false);
  const [containers, setContainers] = useState<EnvScan["containers"]>();
  const [discovering, setDiscovering] = useState(false);
  const [discoverErr, setDiscoverErr] = useState<string>();
  // github: access comes from a token stored in the cell (a credential) or, as the exception, a
  // token on this source only. The stored ones are offered first; the repo is picked from what
  // the chosen token can see. `values.credential` / `values.token` stay the form's truth.
  const [ghCreds, setGhCreds] = useState<GithubCredential[]>();
  const [ghNew, setGhNew] = useState(false);                     // the "add a new token" strip is open
  const [ghSave, setGhSave] = useState(false);                   // save the new token in the cell (off: this source only)
  const [ghName, setGhName] = useState("");
  const [ghApi, setGhApi] = useState("");
  const [ghTest, setGhTest] = useState<{ busy?: boolean; ok?: boolean; login?: string; error?: string }>();
  const [ghRepos, setGhRepos] = useState<{ full_name: string; default_branch: string; private: boolean }[]>();
  const [ghReposErr, setGhReposErr] = useState<string>();
  const [ghHelp, setGhHelp] = useState(false);
  const ghOwnToken = connector === "github" && !!initial?.config?.token && !cleared.token;   // editing a source that carries its own token
  useEffect(() => {
    if (connector !== "github") return;
    api.githubCredentials().then((r) => {
      setGhCreds(r.credentials);
      // a fresh source with stored tokens starts on the first one; nothing is typed by hand
      if (!initial && r.credentials.length > 0)
        setValues((v) => (v.credential ? v : { ...v, credential: r.credentials[0].name }));
      const names = r.credentials.map((c) => c.name);
      let n = "github", i = 2;
      while (names.includes(n)) n = `github-${i++}`;
      setGhName(n);
    }).catch(() => setGhCreds([]));
  }, [connector]);   // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (connector !== "github" || !values.credential) { setGhRepos(undefined); setGhReposErr(undefined); return; }
    let live = true;
    setGhRepos(undefined); setGhReposErr(undefined);
    api.githubCredentialRepos(values.credential)
      .then((r) => { if (live) setGhRepos(r.repos); })
      .catch((e) => { if (live) setGhReposErr(String((e as Error).message ?? e)); });
    return () => { live = false; };
  }, [connector, values.credential]);
  const ghPick = (name: string) => {
    setValues((v) => ({ ...v, credential: name, token: "" }));
    if (ghOwnToken) setCleared((c) => ({ ...c, token: true }));   // the stored token replaces this source's own
    setGhTest(undefined); setGhNew(false);
  };
  const ghTestCred = async () => {
    if (!values.credential) return;
    setGhTest({ busy: true });
    try {
      const r = await api.testGithubCredential(values.credential);
      setGhTest({ ok: r.ok, login: r.login, error: r.error });
    } catch (e) { setGhTest({ ok: false, error: String((e as Error).message ?? e) }); }
  };
  const ghSaveToken = () => run(async () => {
    const token = values.token.trim();
    if (!token) throw new Error("paste the token first");
    if (!ghName.trim()) throw new Error("give the token a name");
    await api.createGithubCredential({ name: ghName.trim(), token, api_url: ghApi.trim() || undefined });
    const r = await api.githubCredentials();
    setGhCreds(r.credentials);
    ghPick(ghName.trim());
  });

  const jsonErrors = useMemo(() => {
    const errs: Record<string, string> = {};
    for (const f of spec.fields) {
      if (f.type === "json" && values[f.name]?.trim()) {
        try { JSON.parse(values[f.name]); } catch (e) { errs[f.name] = "invalid JSON: " + ((e as Error).message ?? e); }
      }
    }
    return errs;
  }, [spec.fields, values]);

  const build = () => {
    if (connector === "reference") {
      const attachments = refAttachments
        .filter((a) => a.name.trim() && a.content.trim())
        .map((a) => ({
          name: a.name.trim(), format: a.format || "txt", content: a.content,
          labels: Object.fromEntries(a.labels.filter(([k, v]) => k.trim() && v.trim())),
        }));
      if (!attachments.length) throw new Error("add at least one document");
      if (!name.trim()) throw new Error("name is required");
      // declare the union of label names as real Tares labels, so the source's Labels panel shows
      // them and views can correlate on them (field maps to the payload label surfaced by label_context)
      const labelNames = [...new Set(refAttachments.flatMap(
        (a) => a.labels.map(([k]) => k.trim()).filter(Boolean)))];
      const cfg: Record<string, unknown> = { attachments };
      if (labelNames.length) cfg.labels = labelNames.map((n) => ({ name: n, field: n }));
      return { name: name.trim(), type, connector, poll: poll || spec.poll || "5s", config: cfg };
    }
    const config: Record<string, unknown> = {};
    for (const f of spec.fields) {
      if (f.type === "list") {
        const out: Record<string, unknown>[] = [];
        for (const row of rows[f.name] ?? []) {
          if (!(f.item ?? []).some((sf) => (row[sf.name] ?? "").trim())) continue;  // skip empty row
          const obj: Record<string, unknown> = {};
          for (const sf of f.item ?? []) {
            const raw = (row[sf.name] ?? "").trim();
            if (!raw) {
              if (sf.required) throw new Error(`${f.name}: "${sf.name}" is required in every row`);
              continue;
            }
            obj[sf.name] = sf.type === "number" ? Number(raw) : raw;
          }
          out.push(obj);
        }
        if (f.required && !out.length) throw new Error(`${f.name}: add at least one row`);
        if (out.length) config[f.name] = out;
        continue;
      }
      const raw = values[f.name]?.trim();
      // A secret that's already set: blank = keep (omit), typed = replace, Remove = clear ("").
      if (secretSet(f)) {
        if (cleared[f.name]) config[f.name] = "";       // explicit remove
        else if (raw) config[f.name] = raw;             // replace
        // else omit → the daemon keeps the stored secret
        continue;
      }
      if (!raw) {
        if (f.required) throw new Error(`${f.name} is required`);
        continue;
      }
      if (f.type === "json") config[f.name] = JSON.parse(raw);
      else if (f.type === "number") config[f.name] = Number(raw);
      else config[f.name] = raw;
    }
    const labels = labelRows.filter((r) => r.name.trim()).map(rowToSpec);
    if (labels.length) config.labels = labels;
    if (!name.trim()) throw new Error("name is required");
    return { name: name.trim(), type, connector, poll: poll.trim() || spec.poll || "5s", config };
  };

  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(undefined);
    try { await fn(); } catch (e) { setError(String((e as Error).message ?? e)); }
    setBusy(false);
  };

  // discover errors surface inside the Discover panel (the top-of-form alert can be scrolled
  // out of view on long forms — the user is looking at the button they just clicked)
  const discover = (override?: Record<string, unknown>) => run(async () => {
    setDiscovering(true);
    setDiscoverErr(undefined);
    try {
      await doDiscover(override);
    } catch (e) {
      setDiscoverErr(String((e as Error).message ?? e));
    }
    setDiscovering(false);
  });

  const doDiscover = async (override?: Record<string, unknown>) => {
    if (connector === "docker_logs") {
      setContainers((await api.discoverEnvironment("docker")).containers);
      return;
    }
    const cfg: Record<string, unknown> = {};
    for (const f of spec.fields) {
      const raw = values[f.name]?.trim();
      if (raw && f.type !== "json") cfg[f.name] = f.type === "number" ? Number(raw) : raw;
    }
    Object.assign(cfg, override);
    const p = await api.discoverSource(connector, cfg);
    // connectors whose discover proposes a config to confirm in a panel set `proposal`
    // (prometheus: metrics), `colProposal` (postgres: table columns) or `tables` (postgres
    // without a table yet: pick one); simpler ones (github) just apply the proposed config
    // and report a one-line summary
    const cat = (p as { catalog?: { metrics: string[]; labels: string[] } }).catalog;
    if (cat) {
      // prometheus: the metric-name + label-name catalogs — feed the two-tab metric picker
      setCatalog(cat);
      setBasket(new Set());
      setMetricsConfirmed(false);
    } else if (Array.isArray((p as { rules?: unknown[] }).rules)) {
      // prometheus_alerts: the configured alerting rules — show them + a severity filter
      setAlertDiscover(p as unknown as typeof alertDiscover);
      setAlertSev(new Set());
      setAlertsConfirmed(false);
    } else if (Array.isArray((p as { columns?: unknown[] }).columns)) {
      const cp = p as unknown as ColumnsProposal;
      setColProposal(cp);
      setPgColumns(cp.columns.map((c) => c.name));   // feed the column picker below
      setEditConn(false);                            // collapse the connection now it's confirmed
    } else if (Array.isArray((p as { tables?: unknown[] }).tables)) {
      setTables((p as unknown as { tables: string[] }).tables);
    } else {
      applyConfig((p as { proposed_config?: Record<string, unknown> }).proposed_config ?? {});
      const summary = (p as { summary?: unknown }).summary;
      if (typeof summary === "string") setTest({ ok: true, note: summary });
    }
  };

  const pickTable = (t: string) => {
    setValues((v) => ({ ...v, table: t }));
    if (!name.trim()) setName(`${t.split(".").pop()}-table`);
    setTables(undefined);
    void discover({ table: t });   // straight to the columns proposal for the picked table
  };

  // prometheus: add/remove metrics in the basket (both tabs feed this one set)
  const basketAdd = (names: string[]) =>
    setBasket((s) => { const n = new Set(s); names.forEach((m) => n.add(m)); return n; });
  const basketRemove = (name: string) =>
    setBasket((s) => { const n = new Set(s); n.delete(name); return n; });
  // fetch the metrics carrying a label (the by-label tab), via a discover call
  const fetchLabelMetrics = async (label: string): Promise<string[]> => {
    const cfg: Record<string, unknown> = { for_label: label };
    for (const f of spec.fields) {
      const raw = values[f.name]?.trim();
      if (raw && f.type !== "json") cfg[f.name] = raw;
    }
    const p = await api.discoverSource("prometheus", cfg) as { metrics_for_label?: string[] };
    return p.metrics_for_label ?? [];
  };
  // confirm the basket → finalize (sample + propose config) → fill the form, collapse the picker
  const confirmMetrics = () => run(async () => {
    if (!basket.size) return;
    if (!name.trim()) setName("prometheus-metrics");
    await doDiscover({ selected: [...basket] });   // finalize → else-branch applyConfig
    setMetricsConfirmed(true);
  });

  // prometheus_alerts: toggle a severity in the filter, and confirm the curation
  const toggleSev = (s: string) =>
    setAlertSev((prev) => { const n = new Set(prev); n.has(s) ? n.delete(s) : n.add(s); return n; });
  const confirmAlerts = () => {
    if (!alertDiscover) return;
    if (!name.trim()) setName("prometheus-alerts");
    setValues((v) => ({ ...v, severities: [...alertSev].join(",") }));
    applyConfig(alertDiscover.proposed_config);   // key + labels
    setAlertsConfirmed(true);
  };

  const pickContainer = (c: { name: string; service: string; project: string }) => {
    setValues((v) => ({ ...v, container: c.name }));
    if (!name.trim()) setName(`${c.service}_logs`);
    setLabelRows(labelsToRows(
      [{ name: "service", const: c.service, primary: true }, { name: "project", const: c.project }]));
    setContainers(undefined);
  };

  const applyConfig = (c: Record<string, unknown>) => {
    setValues((v) => {
      const nv = { ...v };
      for (const f of spec.fields) {
        if (f.type === "list") continue;
        if (c[f.name] != null) nv[f.name] = String(c[f.name]);
      }
      return nv;
    });
    setRows((r) => {
      const nr = { ...r };
      for (const f of spec.fields) {
        if (f.type !== "list" || !Array.isArray(c[f.name])) continue;
        nr[f.name] = (c[f.name] as Record<string, unknown>[]).map((row) =>
          Object.fromEntries((f.item ?? []).map((sf) =>
            [sf.name, row?.[sf.name] != null ? String(row[sf.name]) : ""])));
      }
      return nr;
    });
    setLabelRows(labelsToRows(c.labels));
  };

  const applyProposal = () => {
    if (!proposal) return;
    applyConfig(proposal.proposed_config as unknown as Record<string, unknown>);
    setProposal(undefined);
  };

  // Editing labels re-processes every stored event for the source; warn before that happens.
  // Only meaningful when editing (initial present) — a fresh source has no stored events.
  const canonRows = (rs: LabelRow[]) =>
    JSON.stringify(rs.filter((r) => r.name.trim()).map(rowToSpec));
  const labelsChanged = !!initial && canonRows(labelRows) !== canonRows(labelsToRows(initial?.config?.labels));

  // The right-column control for one field: postgres column checklist / single-column dropdown, a
  // json textarea, or a plain input (with secret handling).
  const fieldControl = (f: ConnectorField) => {
    if (connector === "postgres" && f.name === "columns" && pgColumns?.length) {
      // always keep the cursor/time columns + any column a label reads (the key is a primary label)
      const mandatory = new Set([
        values.cursor_column, values.time_column,
        ...labelRows.filter((r) => r.kind === "field").map((r) => r.value),
      ].map((v) => v?.trim()).filter(Boolean) as string[]);
      return <ColumnPicker columns={pgColumns} value={values[f.name] ?? ""} mandatory={mandatory}
                           onChange={(v) => setValues({ ...values, [f.name]: v })} />;
    }
    if (connector === "postgres" && pgColumns?.length
        && ["cursor_column", "time_column"].includes(f.name)) {
      const opts = pgColumns.includes(values[f.name] ?? "") || !values[f.name]
        ? pgColumns : [values[f.name], ...pgColumns];   // keep a stale value selectable
      return (
        <select value={values[f.name] ?? ""} onChange={(e) => setValues({ ...values, [f.name]: e.target.value })}>
          <option value="">{f.required ? "select…" : "none"}</option>
          {opts.map((c) => <option key={c} value={c}>{c}</option>)}
        </select>
      );
    }
    if (f.type === "bool")
      return <input type="checkbox" checked={values[f.name] === "true"}
                    onChange={(e) => setValues({ ...values, [f.name]: e.target.checked ? "true" : "" })}
                    style={{ width: "auto" }} />;
    if (f.type === "json")
      return <textarea className="code" value={values[f.name]}
                       onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />;
    return <input type={f.secret ? "password" : f.type === "number" ? "number" : "text"}
                  value={values[f.name]} autoComplete={f.secret ? "off" : undefined}
                  disabled={secretSet(f) && cleared[f.name]}
                  placeholder={secretSet(f) ? (cleared[f.name] ? "" : "leave blank to keep") : undefined}
                  onChange={(e) => setValues({ ...values, [f.name]: e.target.value })} />;
  };

  const fieldHelp = (f: ConnectorField) =>
    secretSet(f) ? (
      <span className="help">
        {cleared[f.name]
          ? <>will be removed on save; <button type="button" className="linklike"
                onClick={() => setCleared({ ...cleared, [f.name]: false })}>undo</button></>
          : <>a value is set; type to replace, or leave blank to keep ·{" "}
              <button type="button" className="linklike"
                onClick={() => { setCleared({ ...cleared, [f.name]: true }); setValues({ ...values, [f.name]: "" }); }}>remove</button></>}
      </span>
    ) : <span className="help">{jsonErrors[f.name] ?? f.help}</span>;

  const fieldLabel = (f: ConnectorField) =>
    (connector === "postgres" && PG_LABEL[f.name]) ||
    (connector === "prometheus" && PROM_LABEL[f.name]) || f.name;
  const fieldDetected = (f: ConnectorField) =>       // value came from Discover, not the user
    connector === "postgres" && !!pgColumns?.length && !!values[f.name]?.trim()
    && ["cursor_column", "time_column"].includes(f.name);

  // GitHub: access (a stored token, or a new one) and the repo picked from what it can see, in
  // place of the generic repo / credential / token rows.
  const renderGithubFields = () => {
    const creds = ghCreds ?? [];
    const credOpts = creds.map((c) => c.name);
    const credLabels = Object.fromEntries(creds.map((c) => [c.name, c.account ? `${c.name} (${c.account})` : c.name]));
    const stored = !!values.credential && !ghNew;
    const addingNew = ghNew || (ghCreds !== undefined && creds.length === 0 && !ghOwnToken);
    const repoField = spec.fields.find((f) => f.name === "repo")!;
    return (
      <>
        <div className="fld-row">
          <div className="fld-meta">
            <span className="lbl">access</span>
            <span className="help">
              {stored ? "a token stored in this cell; the source reads it at every poll, so rotating it under Settings rotates this source too"
                : addingNew ? "a GitHub token; private repos need one, and without one GitHub allows 60 requests an hour"
                : ghOwnToken ? "this source carries its own token"
                : "no token: public repos only, 60 requests an hour"}
            </span>
          </div>
          <div className="fld-ctrl">
            {ghCreds === undefined && <span className="dim">loading…</span>}
            {ghCreds !== undefined && !addingNew && (
              <div className="btnrow" style={{ flexWrap: "nowrap" }}>
                {creds.length > 0 && (
                  <Picker value={values.credential} onChange={ghPick} ariaLabel="stored token" style={{ flex: 1 }}
                          options={values.credential && !credOpts.includes(values.credential) ? [values.credential, ...credOpts] : credOpts}
                          labels={credLabels} />
                )}
                {stored && (
                  <button type="button" onClick={ghTestCred} disabled={ghTest?.busy}>{ghTest?.busy ? "testing…" : "Test"}</button>
                )}
                <button type="button" onClick={() => { setGhNew(true); setValues((v) => ({ ...v, token: "" })); }}>Add a new token</button>
              </div>
            )}
            {ghTest && !ghTest.busy && !addingNew && (
              <div style={{ marginTop: 6 }}>
                {ghTest.ok ? <span className="badge ok">signed in as {ghTest.login}</span>
                  : <span className="badge error">{ghTest.error ?? "failed"}</span>}
              </div>
            )}
            {ghOwnToken && !addingNew && !stored && (
              <div className="help" style={{ marginTop: 6 }}>
                kept as it is{creds.length > 0 ? "; pick a stored token above to use that instead" : ""}
              </div>
            )}
            {addingNew && (
              <div>
                <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                  <input type="password" className="mono" autoComplete="new-password" data-1p-ignore data-lpignore="true"
                         value={values.token} placeholder="github_pat_…" style={{ flex: 1 }}
                         onChange={(e) => setValues({ ...values, token: e.target.value, credential: "" })} />
                  <HelpButton onClick={() => setGhHelp(true)} label="Which permissions does the token need?" />
                </div>
                <label style={{ display: "block", marginTop: 8 }}>
                  <input type="checkbox" checked={ghSave} onChange={(e) => setGhSave(e.target.checked)} />{" "}
                  save it in this cell, so other sources and agents can use it too
                </label>
                {ghSave ? (
                  <div className="btnrow" style={{ marginTop: 8, flexWrap: "nowrap" }}>
                    <input type="text" autoComplete="off" data-1p-ignore data-lpignore="true" value={ghName} onChange={(e) => setGhName(e.target.value)}
                           placeholder="name" style={{ width: 160 }} aria-label="token name" />
                    <input type="text" autoComplete="off" data-1p-ignore data-lpignore="true" className="mono" value={ghApi} onChange={(e) => setGhApi(e.target.value)}
                           placeholder="GitHub Enterprise API URL, optional" style={{ flex: 1 }} aria-label="GitHub Enterprise API URL" />
                    <button type="button" className="primary" disabled={busy || !values.token.trim() || !ghName.trim()} onClick={ghSaveToken}>Save token</button>
                    {creds.length > 0 && <button type="button" onClick={() => { setGhNew(false); setValues((v) => ({ ...v, token: "" })); }}>Cancel</button>}
                  </div>
                ) : (
                  <div className="btnrow" style={{ marginTop: 8 }}>
                    <span className="help">used by this source only; it is stored with the source, never shown again</span>
                    {creds.length > 0 && <button type="button" onClick={() => { setGhNew(false); setValues((v) => ({ ...v, token: "" })); }}>Cancel</button>}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
        <div className="fld-row">
          <div className="fld-meta">
            <span className="lbl">repo<span className="req"> *</span></span>
            <span className="help">
              {stored
                ? ghRepos ? `pick one of the ${ghRepos.length} repos the token can see, or type owner/name`
                  : ghReposErr ? `could not list the token's repos (${ghReposErr}); type owner/name`
                  : "listing the repos the token can see…"
                : repoField.help}
            </span>
          </div>
          <div className="fld-ctrl">
            {stored && ghRepos ? (
              <Combo className="mono" value={values.repo} placeholder="owner/name"
                     options={ghRepos.map((r) => r.full_name)}
                     hints={Object.fromEntries(ghRepos.map((r) => [r.full_name, `${r.private ? "private" : "public"} · ${r.default_branch}`]))}
                     onChange={(v) => setValues({ ...values, repo: v.trim() })} />
            ) : (
              <input type="text" className="mono" value={values.repo} placeholder="owner/name" autoComplete="off"
                     onChange={(e) => setValues({ ...values, repo: e.target.value })} />
            )}
          </div>
        </div>
        {ghHelp && (
          <InfoDialog title="Token permissions" onClose={() => setGhHelp(false)}>
            <p className="help" style={{ margin: 0 }}>
              Create a fine-grained personal access token on GitHub (Settings, Developer settings, Personal access tokens, Fine-grained tokens).
              Under Repository access pick the repos this cell should read. Repository permissions: Contents, Read-only, to read commits and diffs; Metadata, Read-only, added by GitHub automatically, to list the repos.
              A token that agents also write with needs Contents and Pull requests as Read and write.
            </p>
          </InfoDialog>
        )}
      </>
    );
  };

  // Two-column row: label + description on the left, control on the right. (list fields keep their
  // own full-width editor.)
  const renderField = (f: ConnectorField) => {
    if (f.type === "list")
      return <ListFieldEditor key={f.name} field={f} rows={rows[f.name] ?? []}
                              onChange={(r) => setRows({ ...rows, [f.name]: r })} />;
    return (
      <div className={"fld-row" + (jsonErrors[f.name] ? " invalid" : "") + (highlight?.includes(f.name) ? " needs" : "")} key={f.name}>
        <div className="fld-meta">
          <span className="lbl">{fieldLabel(f)}{f.required && <span className="req"> *</span>}
            {fieldDetected(f) && <span className="badge ok" style={{ marginLeft: 8 }}>detected</span>}
            {highlight?.includes(f.name) && <span className="badge" style={{ marginLeft: 8 }}>yours to fill</span>}</span>
          {fieldHelp(f)}
        </div>
        <div className="fld-ctrl">{fieldControl(f)}</div>
      </div>
    );
  };

  // Prometheus: the discovered queries as a compact list, with the fallback-key field + a raw
  // query editor tucked into a collapsed Advanced group (so the form isn't 80 fat panels tall).
  const renderPromFields = (fields: ConnectorField[]) => {
    const queriesField = fields.find((f) => f.name === "queries");
    const rest = fields.filter((f) => f.name !== "queries");
    return (
      <>
        {queriesField && (
          <PromQueriesView rows={rows.queries ?? []}
                           onChange={(r) => setRows({ ...rows, queries: r })} />
        )}
        <details className="fld-group adv-group">
          <summary>Advanced<span className="caret">›</span></summary>
          <div>
            {rest.map(renderField)}
            {queriesField && (
              <ListFieldEditor field={queriesField} rows={rows.queries ?? []}
                               onChange={(r) => setRows({ ...rows, queries: r })} />
            )}
          </div>
        </details>
      </>
    );
  };

  // Postgres: group the poll settings into a main block + a collapsed "Advanced".
  const renderPgFields = (fields: ConnectorField[]) => {
    const main = fields.filter((f) => PG_GROUP[f.name] !== "advanced");
    const advanced = fields.filter((f) => PG_GROUP[f.name] === "advanced");
    const found = !!pgColumns?.length;
    return (
      <>
        <div className="fld-group">
          <div className="fld-group-head">
            <h3>{found ? <>What we read from <code>{values.table || "the table"}</code></> : "How to poll the table"}</h3>
            <p className="help">{found
              ? "Discover picked these from the columns; adjust any that aren't right."
              : "Run Discover above to fill these from the table, or set them by hand."}</p>
          </div>
          {main.map(renderField)}
        </div>
        {advanced.length > 0 && (
          <details className="fld-group adv-group">
            <summary>Advanced<span className="caret">›</span></summary>
            <div>{advanced.map(renderField)}</div>
          </details>
        )}
      </>
    );
  };

  return (
    <form onSubmit={(e) => { e.preventDefault(); run(async () => onSubmit(build())); }}>
      {error && <div className="alert error">{error}</div>}
      {test && (
        <div className={`alert ${test.ok ? "ok" : "error"}`}>
          {test.ok
            ? <>connection ok; {test.note ?? `${test.events} event(s) on a test poll`}
                {test.sample?.length ? <pre className="payload">{test.sample.join("\n")}</pre> : null}</>
            : <>test failed: {test.error}</>}
        </div>
      )}

      <label className="field" style={{ maxWidth: 360 }}>
        <span className="lbl">name <span className="req">*</span></span>
        <input type="text" value={name} disabled={lockName}
               placeholder={NAME_PLACEHOLDER[connector] ?? "e.g. metrics"}
               onChange={(e) => setName(e.target.value)} />
      </label>

      {spec.mode === "poll" && (() => {
        // split the stored "5m" string into a number + unit so the user can't type a malformed
        // duration; recombine on change. Save-time validation (_check_duration) still applies.
        const pm = /^\s*(\d+(?:\.\d+)?)\s*([smh])\s*$/.exec(poll);
        const num = pm ? pm[1] : "5";
        const unit = pm ? pm[2] : "s";
        return (
          <div className="field" style={{ maxWidth: 260 }}>
            <span className="lbl">poll interval</span>
            <div style={{ display: "flex", gap: 8 }}>
              <input type="number" min="1" step="1" value={num} style={{ flex: 1 }}
                     onChange={(e) => setPoll(`${e.target.value}${unit}`)} />
              <Picker value={unit} onChange={(v) => setPoll(`${num}${v}`)} style={{ width: 120 }}
                      ariaLabel="poll interval unit"
                      options={["s", "m", "h"]}
                      labels={{ s: "seconds", m: "minutes", h: "hours" }} />
            </div>
          </div>
        );
      })()}

      {spec.discover && !editConn
        && ((connector === "postgres" && pgColumns?.length) || (connector === "prometheus" && catalog)
            || (connector === "prometheus_alerts" && alertDiscover)) ? (
        <div className="conn-summary">
          <span className="tick">✓</span>
          <span>Connected{connector === "postgres" ? (() => {
            try { const u = new URL(values.dsn); const db = u.pathname.replace(/^\//, "");
              return <> to <code>{db || u.hostname}</code>{db && u.hostname ? <> · {u.hostname}</> : null}</>; }
            catch { return null; }
          })() : (() => {
            try { return <> to <code>{new URL(values.url).host}</code></>; } catch { return null; }
          })()}</span>
          <button type="button" className="linklike" style={{ marginLeft: "auto" }}
                  onClick={() => setEditConn(true)}>edit connection</button>
        </div>
      ) : connector === "reference" ? (
        <ReferenceForm attachments={refAttachments} setAttachments={setRefAttachments} />
      ) : connector === "github" ? (
        renderGithubFields()
      ) : (
        (spec.discover ? spec.fields.filter((f) => f.discover_input) : spec.fields).map(renderField)
      )}

      {spec.discover && !(connector === "postgres" && pgColumns?.length && !editConn)
        && !(connector === "prometheus" && metricsConfirmed)
        && !(connector === "prometheus_alerts" && alertsConfirmed) && (
        <div className="panel" style={{ background: "var(--th-bg)", marginBottom: 14 }}>
          {!(connector === "prometheus" && catalog) && !(connector === "prometheus_alerts" && alertDiscover) && (
            <div className="btnrow" style={{ alignItems: "center" }}>
              <button type="button" disabled={busy} onClick={() => discover()}>
                {discovering ? "⏳ discovering…"
                  : `✦ ${connector === "docker_logs" ? "Discover containers" : "Discover"}`}
              </button>
              <span className="help" style={{ margin: 0 }}>
                {DISCOVER_HINT[connector]
                  ?? "fill the URL above, then let Tares introspect the source and propose what to ingest; no PromQL to write by hand"}
              </span>
            </div>
          )}
          {discoverErr && <div className="alert error" style={{ marginTop: 10, marginBottom: 0 }}>{discoverErr}</div>}
          {proposal && <ProposalView proposal={proposal} onApply={applyProposal} />}
          {connector === "prometheus" && catalog && !metricsConfirmed && (
            <MetricBasket catalog={catalog} basket={basket} onAdd={basketAdd} onRemove={basketRemove}
                          fetchLabelMetrics={fetchLabelMetrics} onConfirm={confirmMetrics} busy={busy} />
          )}
          {connector === "prometheus_alerts" && alertDiscover && !alertsConfirmed && (
            <AlertRulesCuration rules={alertDiscover.rules} severities={alertDiscover.severities}
              sev={alertSev} onSevToggle={toggleSev} summary={alertDiscover.summary} busy={busy}
              includePending={values.include_pending === "true"}
              onIncludePending={(v) => setValues({ ...values, include_pending: v ? "true" : "" })}
              onConfirm={confirmAlerts} />
          )}
          {tables && (
            <table style={{ marginTop: 10 }}>
              <thead><tr><th>table</th><th style={{ width: 100 }}></th></tr></thead>
              <tbody>
                {tables.map((t) => (
                  <tr key={t}>
                    <td className="mono">{t}</td>
                    <td style={{ textAlign: "right" }}>
                      <button type="button" disabled={busy} onClick={() => pickTable(t)}>use this</button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {colProposal && (
            <ColumnsProposalView proposal={colProposal}
              onApply={() => {
                applyConfig(colProposal.proposed_config);
                setTest({ ok: true, note: colProposal.summary });
                setColProposal(undefined);
              }} />
          )}
          {containers && (
            <table style={{ marginTop: 10 }}>
              <thead><tr><th>service</th><th>image</th><th></th></tr></thead>
              <tbody>
                {containers.map((c) => (
                  <tr key={c.name}>
                    <td className="mono">{c.service}</td>
                    <td className="help">{c.image}</td>
                    <td style={{ textAlign: "right" }}>
                      <button type="button" onClick={() => pickContainer(c)}>use this</button>
                    </td>
                  </tr>
                ))}
                {containers.length === 0 && (
                  <tr><td colSpan={3} className="help">no containers found</td></tr>
                )}
              </tbody>
            </table>
          )}
        </div>
      )}

      {connector === "prometheus" && metricsConfirmed && (
        <div className="conn-summary" style={{ marginBottom: 12 }}>
          <span className="tick">✓</span>
          <span>{basket.size} metric{basket.size === 1 ? "" : "s"} chosen</span>
          <button type="button" className="linklike" style={{ marginLeft: "auto" }}
                  onClick={() => setMetricsConfirmed(false)}>change metrics</button>
        </div>
      )}

      {connector === "prometheus_alerts" && alertsConfirmed && (
        <div className="conn-summary" style={{ marginBottom: 12 }}>
          <span className="tick">✓</span>
          <span>ingesting {alertSev.size ? [...alertSev].join(", ") : "all"} alerts
            {values.include_pending === "true" ? " (incl. pending)" : ""}</span>
          <button type="button" className="linklike" style={{ marginLeft: "auto" }}
                  onClick={() => setAlertsConfirmed(false)}>change filter</button>
        </div>
      )}

      {spec.discover && (connector === "postgres"
        ? renderPgFields(spec.fields.filter((f) => !f.discover_input))
        : connector === "prometheus"
        ? (metricsConfirmed ? renderPromFields(spec.fields.filter((f) => !f.discover_input)) : null)
        : connector === "prometheus_alerts"
        ? null   // include_pending + severities are set in the curation section
        : spec.fields.filter((f) => !f.discover_input).map(renderField))}

      {connector !== "reference"
        && !(connector === "prometheus" && !metricsConfirmed)
        && !(connector === "prometheus_alerts" && !alertsConfirmed) && (
        <LabelsEditor rows={labelRows} onChange={setLabelRows} sourceName={initial?.name}
                      fields={labelFieldOpts} fieldHints={labelFieldHints} />
      )}

      {labelsChanged && (
        <div className="alert info">
          Label changes apply to new events going forward. Existing events keep the labels they were
          ingested with; retroactive relabel of stored events is coming soon.
        </div>
      )}

      {!(connector === "prometheus" && !metricsConfirmed)
        && !(connector === "prometheus_alerts" && !alertsConfirmed) && (
        <div className="btnrow">
          <button type="submit" className="primary" disabled={busy || Object.keys(jsonErrors).length > 0}>
            {submitLabel}
          </button>
          {spec.mode === "poll" && (
            <button type="button" disabled={busy}
                    onClick={() => run(async () => setTest(await api.testSource(build())))}>
              Test connection
            </button>
          )}
        </div>
      )}
    </form>
  );
}

type AlertRule = { name: string; severity: string; group: string; state: string };
// labels as an ordered [key, value] list (not a Record) so editing a key doesn't change a React
// key and remount the input — that was eating focus after one character.
type Attachment = { name: string; format: string; content: string; labels: [string, string][] };
type MapRow = { from: string; to: string };
type LabelRow = { name: string; kind: "const" | "field"; value: string; primary: boolean;
                  type: "string" | "number";
                  pattern: string; replace: string; map: MapRow[]; normOpen: boolean };

function labelsToRows(arr: unknown): LabelRow[] {
  if (!Array.isArray(arr)) return [];
  const rows = arr.map((x) => {
    const o = x as Record<string, unknown>;
    const kind: "const" | "field" = "field" in o ? "field" : "const";
    const map = o.map && typeof o.map === "object"
      ? Object.entries(o.map as Record<string, string>).map(([from, to]) => ({ from, to: String(to) }))
      : [];
    return { name: String(o.name ?? ""), kind, value: String(o[kind] ?? ""), primary: !!o.primary,
             type: o.type === "number" ? "number" as const : "string" as const,
             pattern: String(o.pattern ?? ""), replace: String(o.replace ?? ""), map,
             // Collapsed on load. This used to open every label that had rules, because an
             // expanded panel was the ONLY way to see a label carried any — the rules column now
             // says so in a word, and each panel is a few hundred pixels that buried the form.
             normOpen: false };
  });
  if (rows.length && !rows.some((r) => r.primary)) rows[0].primary = true;  // first is the key by default
  return rows;
}

/** Row -> label spec, including the optional value normalization (pattern/replace + alias map). */
function rowToSpec(r: LabelRow): Record<string, unknown> {
  const spec: Record<string, unknown> = { name: r.name.trim(), [r.kind]: r.value,
                                          ...(r.primary ? { primary: true } : {}),
                                          // the key is always a string; number labels are aggregatable
                                          ...(r.type === "number" && !r.primary ? { type: "number" } : {}) };
  if (r.kind === "field") {
    if (r.pattern.trim()) { spec.pattern = r.pattern; spec.replace = r.replace; }
    const map = Object.fromEntries(r.map.filter((m) => m.from.trim()).map((m) => [m.from, m.to]));
    if (Object.keys(map).length) spec.map = map;
  }
  return spec;
}

/** What this label does to its values — shared with the read-only tables on the source page, so
 *  both name the same rule the same way. "" means it passes values through untouched, which the
 *  caller also uses to decide the active styling. */
function normSummary(row: LabelRow): string {
  return ruleSummary(row.pattern, row.map.length);
}

function normTitle(row: LabelRow): string {
  const bits: string[] = [];
  if (row.pattern) bits.push(`pattern ${row.pattern} → ${row.replace ? `"${row.replace}"` : "(empty)"}`);
  if (row.map.length) bits.push(`${row.map.length} value rename${row.map.length === 1 ? "" : "s"}`);
  return bits.length ? `${bits.join(", ")}. Click to edit.` : "No rules. Click to normalize values.";
}

function LabelsEditor({ rows, onChange, fields = [], fieldHints, sourceName }:
  { rows: LabelRow[]; onChange: (r: LabelRow[]) => void; fields?: string[];
    fieldHints?: Record<string, string>; sourceName?: string }) {
  const set = (i: number, patch: Partial<LabelRow>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const makePrimary = (i: number) => onChange(rows.map((r, j) =>
    j === i ? { ...r, primary: true, type: "string" as const } : { ...r, primary: false }));
  const remove = (i: number) => {
    const next = rows.filter((_, j) => j !== i);
    if (next.length && !next.some((r) => r.primary)) next[0].primary = true;  // keep one key
    onChange(next);
  };
  const keyIndex = Math.max(0, rows.findIndex((r) => r.primary));
  return (
    <div className="field">
      {/* No blurb here. It used to explain const-vs-field and what the key is — both of which the
          `from` column and the `entity key` picker now say on their own. */}
      <span className="lbl">labels &amp; key</span>
      {/* The key is ONE decision about the source, so it gets ONE control. It used to be a radio
          per row, which reads as "pick the row I'm editing" rather than "pick the key"; and its
          consequence (the key's type is forced to string) then looked like a broken type select. */}
      {rows.length > 0 && (
        <div className="key-picker">
          <span className="lbl" style={{ margin: 0 }}>entity key</span>
          <Picker value={String(keyIndex)} ariaLabel="entity key"
                  options={rows.map((_, i) => String(i))}
                  labels={Object.fromEntries(rows.map((r, i) =>
                    [String(i), r.name || `(unnamed label ${i + 1})`]))}
                  onChange={(v) => makePrimary(Number(v))} />
        </div>
      )}
      {rows.length > 0 && (
        <div className="label-head">
          <span className="help label-col-key" />
          <span className="help label-col-grow">name</span>
          <span className="help label-col-from">from</span>
          <span className="help label-col-grow">value</span>
          <span className="help" style={{ width: 108, flexShrink: 0 }}>type</span>
          <span className="help label-col-norm">rules</span>
          <span className="label-col-x" />
        </div>
      )}
      {rows.map((row, i) => (
        <div key={i}>
          <div className="label-row">
            <span className="label-col-key">
              {row.primary && <span className="badge key-chip" title="this label is the entity key">key</span>}
            </span>
            <input className="label-col-grow" placeholder="e.g. service" value={row.name}
                   onChange={(e) => set(i, { name: e.target.value })} />
            <Picker className="label-col-from" value={row.kind} ariaLabel="value comes from"
                    options={["const", "field"]}
                    labels={{ const: "const (fixed)", field: "field (per event)" }}
                    onChange={(v) => set(i, { kind: v as LabelRow["kind"] })} />
            {row.kind === "field" && fields.length ? (
              <Combo className="label-col-grow" value={row.value} options={fields}
                     hints={fieldHints} placeholder="field, e.g. service"
                     onChange={(v) => set(i, { value: v })} />
            ) : (
              <input className="label-col-grow"
                     placeholder={row.kind === "const" ? "value, e.g. api-server" : "field, e.g. service"}
                     value={row.value} onChange={(e) => set(i, { value: e.target.value })} />
            )}
            <Picker style={{ width: 108, flexShrink: 0 }} ariaLabel="label type"
                    value={row.primary ? "string" : row.type} disabled={row.primary}
                    options={["string", "number"]}
                    title={row.primary ? "the key is always a string"
                                       : "number labels can be aggregated (avg/max/sum) in triggers"}
                    onChange={(v) => set(i, { type: v as LabelRow["type"] })} />
            {/* Says WHAT the rule is, not just that the button exists. Reading the table used to
                give no way to tell a pass-through field from one rewriting its values: the only
                difference was `.dim` vs `.active`, and no CSS rule matched a bare `button.active`,
                so both rendered identically. */}
            {row.kind === "field" ? (
              <button type="button" title={normTitle(row)}
                      className={"label-col-norm norm" + (normSummary(row) ? " active" : "")}
                      onClick={() => set(i, { normOpen: !row.normOpen })}>
                {normSummary(row) ? `≈ ${normSummary(row)}` : "≈ none"}
              </button>
            ) : (
              <span className="label-col-norm" />
            )}
            <button type="button" className="danger label-col-x" onClick={() => remove(i)}>×</button>
          </div>
          {row.kind === "field" && row.normOpen && (
            <NormalizeEditor row={row} onChange={(patch) => set(i, patch)} sourceName={sourceName} />
          )}
        </div>
      ))}
      <button type="button"
              onClick={() => onChange([...rows, { name: "", kind: "const", value: "", primary: rows.length === 0,
                                                  type: "string", pattern: "", replace: "", map: [], normOpen: false }])}>
        + Add label
      </button>
      {fields.length > 0 && (
        <>
          <div className="help" style={{ marginTop: 6 }}>
            this connector provides: {fields.map((f, i) => <span key={f}><code>{f}</code>{i < fields.length - 1 ? ", " : ""}</span>)}
            {" "}· pick a <code>field</code> from these (the source's page shows each field's coverage once data flows).
          </div>
        </>
      )}
    </div>
  );
}

/** Merge value variants for one field label. User-first flow: show the values this field
 *  actually has, let the user rename the odd ones onto a canonical one, and reflect the result
 *  live. The regex lives behind an "advanced" toggle for whole families of variants. */
function NormalizeEditor({ row, onChange, sourceName }:
  { row: LabelRow; onChange: (patch: Partial<LabelRow>) => void; sourceName?: string }) {
  const [preview, setPreview] = useState<{ sampled: number; distinct_before: number;
    distinct_after: number; results: { from: string; to: string; events: number }[] }>();
  const [pErr, setPErr] = useState<string>();
  const [advanced, setAdvanced] = useState(!!row.pattern);

  // live preview: on open (bare field -> the observed values), and after every edit (debounced)
  useEffect(() => {
    if (!sourceName || !row.value.trim()) return;
    const t = setTimeout(async () => {
      try {
        setPErr(undefined);
        setPreview(await api.labelPreview(sourceName, rowToSpec(row)));
      } catch (e) { setPreview(undefined); setPErr(String((e as Error).message ?? e)); }
    }, 500);
    return () => clearTimeout(t);
  }, [sourceName, row.value, row.pattern, row.replace, JSON.stringify(row.map)]);

  const observed = preview?.results ?? [];
  const merges = observed.filter((r2) => r2.from !== r2.to).length;
  const fromOpts = observed.map((r2) => r2.from);
  const fromHints = Object.fromEntries(observed.map((r2) => [r2.from, `${r2.events} events`]));

  const hasRules = !!row.pattern || row.map.length > 0;
  // Which label this panel is editing. It is a full-width panel under a table row, so without the
  // name in the heading there is nothing tying it to the row that opened it.
  const label = row.name.trim();

  return (
    <div className="panel" style={{ margin: "2px 0 10px 28px", padding: 12 }}>
      <div className="pagehead" style={{ marginBottom: 6 }}>
        <strong>
          Merge value variants{label && <>: <code>{label}</code></>}
        </strong>
        <span className="btnrow">
          {hasRules && (
            <button type="button" className="danger"
                    title="discard the pattern rule and all renames for this label"
                    onClick={() => { onChange({ pattern: "", replace: "", map: [] }); }}>
              Clear
            </button>
          )}
          <button type="button" onClick={() => onChange({ normOpen: false })}>Done</button>
        </span>
      </div>
      <span className="help" style={{ display: "block", marginTop: 0, marginBottom: 8, whiteSpace: "normal" }}>
        {/* Kept, unlike the paragraphs removed elsewhere on this page: both sentences carry a fact
            the screen cannot show. The first is why merging matters at all; the second is what
            makes someone willing to click. "Rename the variants onto one name here" went; the
            controls below say that. */}
        Variants of the same thing
        (<span className="mono">checkout</span>, <span className="mono">checkout-svc</span>)
        count as different entities and won&rsquo;t correlate. Renaming makes a copy and the
        original stays in the stored event.
      </span>

      {pErr && <div className="alert error">{pErr}</div>}

      {sourceName && preview && (
        <div style={{ marginBottom: 10 }}>
          <span className="help">
            {label ? <code>{label}</code> : "This field"} has{" "}
            <strong>{preview.distinct_before}</strong> distinct value{preview.distinct_before === 1 ? "" : "s"} in
            recent events{merges > 0 && <>. With your rules it becomes <strong>{preview.distinct_after}</strong></>}:
          </span>
          <table style={{ marginTop: 6 }}>
            <thead><tr><th>value seen</th><th className="num">events</th><th>will become</th></tr></thead>
            <tbody>
              {observed.slice(0, 12).map((r2, k) => (
                <tr key={k}>
                  <td className="mono">{r2.from}</td>
                  <td className="num">{r2.events}</td>
                  {/* An empty target used to render as a green arrow followed by nothing, which is
                      indistinguishable from a missing value; and `ok` styling on it reads as
                      approval of the wrong thing. Blanking a value is a legitimate rule (a pattern
                      that keeps only what matches), so it needs to be stated, not flagged. */}
                  <td className="mono">
                    {r2.from === r2.to
                      ? <span className="dim">unchanged</span>
                      : r2.to === ""
                      ? <><span className="badge blanked" style={{ marginRight: 6 }}>→</span>
                          <span className="dim"><em>(empty)</em></span></>
                      : <><span className="badge ok" style={{ marginRight: 6 }}>→</span>{r2.to}</>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {sourceName && !preview && !pErr && <div className="dim" style={{ marginBottom: 10 }}>loading observed values…</div>}
      {!sourceName && (
        <span className="help" style={{ display: "block", marginBottom: 10 }}>
          (once this source has data, its actual values will show here so you can pick what to rename)
        </span>
      )}

      {row.map.map((m, j) => (
        <div key={j} className="btnrow" style={{ marginBottom: 6, alignItems: "center" }}>
          <span className="help">rename</span>
          <Combo style={{ flex: 1 }} value={m.from} options={fromOpts} hints={fromHints}
                 placeholder="value seen in the data"
                 onChange={(v) => onChange({ map: row.map.map((x, k) => k === j ? { ...x, from: v } : x) })} />
          <span className="help">to</span>
          <input className="mono" style={{ flex: 1 }} placeholder="the name it should have"
                 value={m.to}
                 onChange={(e) => onChange({ map: row.map.map((x, k) => k === j ? { ...x, to: e.target.value } : x) })} />
          <button type="button" className="danger" onClick={() => onChange({ map: row.map.filter((_, k) => k !== j) })}>×</button>
        </div>
      ))}
      <div className="btnrow">
        <button type="button" onClick={() => onChange({ map: [...row.map, { from: "", to: "" }] })}>
          + Rename a value
        </button>
        <button type="button" className="dim" onClick={() => setAdvanced((a) => !a)}>
          {advanced ? "hide pattern rule" : "advanced: pattern rule…"}
        </button>
      </div>

      {advanced && (
        <PatternRule key={row.pattern === "" ? "empty" : "set"} row={row} onChange={onChange} />
      )}
    </div>
  );
}

const escRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

type RuleKind = "suffix" | "prefix" | "after" | "custom";

/** Builds the pattern for the user from plain choices — "remove the ending -svc" — so nobody has
 *  to write a regex unless they pick custom. The stored config is still just pattern/replace. */
function PatternRule({ row, onChange }:
  { row: LabelRow; onChange: (patch: Partial<LabelRow>) => void }) {
  const [kind, setKind] = useState<RuleKind>(row.pattern ? "custom" : "suffix");
  const [text, setText] = useState("");

  const apply = (k: RuleKind, t: string) => {
    if (k === "suffix") onChange({ pattern: t ? `${escRe(t)}$` : "", replace: "" });
    else if (k === "prefix") onChange({ pattern: t ? `^${escRe(t)}` : "", replace: "" });
    else if (k === "after") onChange({ pattern: t ? `${escRe(t)}.*$` : "", replace: "" });
  };

  return (
    <div style={{ marginTop: 8 }}>
      <span className="help" style={{ display: "block", marginBottom: 6, whiteSpace: "normal" }}>
        A pattern rule fixes a whole family of values at once (it runs before the renames above).
        The table shows its effect live.
      </span>
      <div className="btnrow" style={{ alignItems: "center" }}>
        <Picker value={kind} style={{ maxWidth: 260 }} ariaLabel="pattern rule kind"
                options={["suffix", "prefix", "after", "custom"]}
                labels={{ suffix: "remove this ending", prefix: "remove this beginning",
                          after: "cut everything from … onwards", custom: "custom (regular expression)" }}
                onChange={(v) => { const k = v as RuleKind; setKind(k);
                                   if (k === "custom") return; apply(k, text); }} />
        {kind !== "custom" ? (
          <input className="mono" style={{ flex: 1 }}
                 placeholder={kind === "suffix" ? "e.g. -svc" : kind === "prefix" ? "e.g. prod-" : "e.g. ."}
                 value={text}
                 onChange={(e) => { setText(e.target.value); apply(kind, e.target.value); }} />
        ) : (
          <>
            <input className="mono" style={{ flex: 2 }} placeholder="regex, e.g. -(service|svc)$"
                   value={row.pattern} onChange={(e) => onChange({ pattern: e.target.value })} />
            <input className="mono" style={{ flex: 1 }} placeholder="replacement (empty = remove)"
                   value={row.replace} onChange={(e) => onChange({ replace: e.target.value })} />
          </>
        )}
      </div>
      {kind === "suffix" && (
        <span className="help">e.g. entering <span className="mono">-svc</span> turns{" "}
          <span className="mono">checkout-svc</span> into <span className="mono">checkout</span></span>
      )}
      {kind === "prefix" && (
        <span className="help">e.g. entering <span className="mono">prod-</span> turns{" "}
          <span className="mono">prod-checkout</span> into <span className="mono">checkout</span></span>
      )}
      {kind === "after" && (
        <span className="help">e.g. entering <span className="mono">.</span> turns{" "}
          <span className="mono">checkout.internal.eu</span> into <span className="mono">checkout</span></span>
      )}
    </div>
  );
}

function ListFieldEditor({ field, rows, onChange }:
  { field: ConnectorField; rows: Array<Record<string, string>>; onChange: (r: Array<Record<string, string>>) => void }) {
  const sub = field.item ?? [];
  const blank = () => Object.fromEntries(sub.map((sf) => [sf.name, ""]));
  const setCell = (i: number, name: string, val: string) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, [name]: val } : r)));

  return (
    <div className="field">
      <span className="lbl">{field.name} {field.required && <span className="req">*</span>}</span>
      <span className="help" style={{ marginTop: 0, marginBottom: 6 }}>{field.help}</span>
      {rows.length === 0 && <div className="empty" style={{ padding: 10 }}>no rows yet; add one below</div>}
      {rows.map((row, i) => (
        <div key={i} className="panel" style={{ padding: 10, marginBottom: 8 }}>
          <div className="btnrow" style={{ justifyContent: "space-between", marginBottom: 4 }}>
            <span className="help" style={{ margin: 0 }}>#{i + 1}</span>
            <button type="button" className="danger" onClick={() => onChange(rows.filter((_, j) => j !== i))}>
              remove
            </button>
          </div>
          {sub.map((sf) => (
            <label className="field" key={sf.name} style={{ marginBottom: 6 }}>
              <span className="lbl" style={{ fontSize: 12 }}>
                {sf.name} {sf.required && <span className="req">*</span>}
              </span>
              <input type={sf.type === "number" ? "number" : "text"} value={row[sf.name] ?? ""}
                     placeholder={sf.help} className={sf.name === "promql" ? "mono" : undefined}
                     onChange={(e) => setCell(i, sf.name, e.target.value)} />
            </label>
          ))}
        </div>
      ))}
      <button type="button" onClick={() => onChange([...rows, blank()])}>
        + Add {field.name === "queries" ? "query" : "row"}
      </button>
    </div>
  );
}

// Prometheus family picker: the metric families (name prefixes) as a scannable, sortable list —
// count-descending, each with a proportional weight bar so the real systems (pg 373) stand out
// from the long tail. Groups with only 1–2 metrics fold behind a toggle; select-all/none acts on
// whatever the filter currently shows.
function FamilyPicker({ groups, sel, q, setQ, onToggle, onSelect, onIntrospect, busy }: {
  groups: { prefix: string; count: number }[];
  sel: Set<string>; q: string; setQ: (v: string) => void;
  onToggle: (p: string) => void; onSelect: (s: Set<string>) => void;
  onIntrospect: () => void; busy: boolean;
}) {
  const max = groups[0]?.count ?? 1;
  const bar = (n: number) => Math.max(3, Math.round(100 * Math.log(n + 1) / Math.log(max + 1)));
  const match = (g: { prefix: string }) => !q || g.prefix.toLowerCase().includes(q.toLowerCase());
  const shown = groups.filter(match);
  const main = shown.filter((g) => g.count >= 3);
  const tail = shown.filter((g) => g.count < 3);
  const totalMetrics = groups.reduce((n, g) => n + g.count, 0);

  const Row = (g: { prefix: string; count: number }) => (
    <label key={g.prefix} className="fam-row">
      <input type="checkbox" checked={sel.has(g.prefix)} onChange={() => onToggle(g.prefix)} />
      <span className="mono fam-name">{g.prefix}<span className="fam-star">_*</span></span>
      <span className="fam-bar"><span style={{ width: `${bar(g.count)}%` }} /></span>
      <span className="fam-count">{g.count}</span>
    </label>
  );

  return (
    <div style={{ marginTop: 10 }}>
      <p className="subtitle" style={{ marginTop: 0 }}>
        {totalMetrics.toLocaleString()} metrics across {groups.length} systems; each is one exporter
        or app. Tick the ones you want to ingest.
      </p>
      <div className="btnrow" style={{ marginBottom: 6, alignItems: "center", gap: 8 }}>
        <input type="text" placeholder="filter systems…" value={q}
               onChange={(e) => setQ(e.target.value)} style={{ flex: 1, margin: 0 }} />
        <button type="button" onClick={() => onSelect(new Set([...sel, ...shown.map((g) => g.prefix)]))}>
          select shown
        </button>
        <button type="button" onClick={() => onSelect(new Set())}>clear</button>
      </div>
      <div className="fam-list">
        {main.map(Row)}
        {tail.length > 0 && (
          <details style={{ marginTop: 4 }}>
            <summary className="help" style={{ cursor: "pointer", padding: "4px 6px" }}>
              {tail.length} smaller {tail.length === 1 ? "group" : "groups"} (1–2 metrics each)
            </summary>
            <div style={{ marginTop: 4 }}>{tail.map(Row)}</div>
          </details>
        )}
        {shown.length === 0 && <div className="help" style={{ padding: 8 }}>no systems match “{q}”</div>}
      </div>
      <div className="btnrow" style={{ marginTop: 8, alignItems: "center" }}>
        <button type="button" className="primary" disabled={busy || sel.size === 0} onClick={onIntrospect}>
          Introspect {sel.size || ""} selected
        </button>
        <span className="help" style={{ margin: 0 }}>
          each becomes one compact query that ingests the whole family
        </span>
      </div>
    </div>
  );
}

// Reference connector form: upload documents (json/csv/md/txt), attach per-file labels, and pick
// which label is the entity key. No manual data entry — files are read client-side into text. The
// stored source mirrors this list (declarative), so re-opening + adding a file extends it.
// Module-level (NOT defined inside ReferenceForm): a component defined inside another re-mounts on
// every parent render, which detaches the <input> mid-dialog and drops the file selection.
function RefUpload({ onFiles, label }: { onFiles: (f: FileList | null) => void; label: string }) {
  return (
    <label style={{ display: "inline-flex", cursor: "pointer" }}>
      <span className="chip" style={{ padding: "8px 14px" }}>{label}</span>
      <input type="file" accept=".json,.csv,.md,.txt,.markdown,.text" multiple style={{ display: "none" }}
             onChange={(e) => { onFiles(e.target.files); e.target.value = ""; }} />
    </label>
  );
}

function ReferenceForm({ attachments, setAttachments }: {
  attachments: Attachment[]; setAttachments: (fn: (prev: Attachment[]) => Attachment[]) => void;
}) {
  // which (attachment, label) pairs are open for editing; a filled label shows read-only + pencil
  const [editing, setEditing] = useState<Set<string>>(new Set());
  const openEdit = (id: string, on: boolean) =>
    setEditing((prev) => { const n = new Set(prev); on ? n.add(id) : n.delete(id); return n; });

  const onFiles = (files: FileList | null) => {
    Array.from(files ?? []).forEach((f) => {
      const r = new FileReader();
      r.onload = () => {
        const ext = (f.name.split(".").pop() ?? "txt").toLowerCase();
        const format = ["json", "csv", "md", "txt"].includes(ext) ? ext : (ext === "markdown" ? "md" : "txt");
        setAttachments((prev) => [...prev,
          { name: f.name, format, content: String(r.result ?? ""), labels: [] }]);
      };
      r.readAsText(f);
    });
  };
  const patch = (i: number, labels: [string, string][]) =>
    setAttachments((prev) => prev.map((a, j) => (j === i ? { ...a, labels } : a)));
  const remove = (i: number) => setAttachments((prev) => prev.filter((_, j) => j !== i));
  const setPair = (i: number, li: number, next: [string, string]) =>
    patch(i, attachments[i].labels.map((p, j) => (j === li ? next : p)));
  const addPair = (i: number) => {
    patch(i, [...attachments[i].labels, ["", ""]]);
    openEdit(`${i}:${attachments[i].labels.length}`, true);
  };
  const delPair = (i: number, li: number) => patch(i, attachments[i].labels.filter((_, j) => j !== li));

  return (
    <div style={{ marginTop: 4 }}>
      <span className="lbl" style={{ display: "block" }}>documents</span>
      <p className="help" style={{ marginTop: 2, marginBottom: 10 }}>
        Upload json / csv / md / txt files and tag each with the entity's labels (e.g.{" "}
        <span className="mono">service=tares</span>). Those labels become real Tares labels you
        can correlate on; and the doc is always attached to that entity, no time window.
      </p>

      {attachments.length === 0 ? (
        <div style={{ border: "1px dashed var(--line)", borderRadius: 10, padding: 28,
                      textAlign: "center", marginBottom: 16 }}>
          <RefUpload onFiles={onFiles} label="Upload documents" />
          <div className="help" style={{ marginTop: 10 }}>json · csv · md · txt</div>
        </div>
      ) : (
        <>
          {attachments.map((a, i) => (
            <div key={i} className="ref-row">
              <div className="ref-file">
                <div className="mono" style={{ fontWeight: 600, wordBreak: "break-all" }}>{a.name}</div>
                <div className="help" style={{ margin: 0 }}>{a.format} · {a.content.length.toLocaleString()} chars</div>
              </div>
              <div className="ref-labels">
                {a.labels.map(([k, v], li) => {
                  const id = `${i}:${li}`;
                  const isEditing = editing.has(id) || !k.trim();
                  return isEditing ? (
                    <span key={li} className="ref-pair">
                      <input type="text" placeholder="label" value={k} style={{ width: 120 }} autoFocus
                             onChange={(e) => setPair(i, li, [e.target.value, v])} />
                      <span style={{ opacity: 0.5 }}>=</span>
                      <input type="text" placeholder="value" value={v} style={{ width: 150 }}
                             onChange={(e) => setPair(i, li, [k, e.target.value])} />
                      <button type="button" className="ref-x" title="done" style={{ color: "var(--ok)" }}
                              onClick={() => openEdit(id, false)}>✓</button>
                      <button type="button" className="ref-x" title="remove label"
                              onClick={() => delPair(i, li)}>×</button>
                    </span>
                  ) : (
                    <span key={li} className="chip mono" style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      {k}={v || <span className="help" style={{ margin: 0 }}>—</span>}
                      <button type="button" className="ref-x" title="edit" onClick={() => openEdit(id, true)}>✎</button>
                    </span>
                  );
                })}
                <button type="button" onClick={() => addPair(i)}>+ label</button>
              </div>
              <button type="button" className="linklike ref-remove" onClick={() => remove(i)}>remove</button>
            </div>
          ))}
          <div style={{ marginTop: 4, marginBottom: 4 }}><RefUpload onFiles={onFiles} label="+ Upload more" /></div>
        </>
      )}
    </div>
  );
}

// Prometheus alerts curation: the configured alerting rules (transparency — read-only), an optional
// severity filter (opt-out; blank = all), and the pending toggle. Unlike metrics you don't pick
// individual rules — you ingest all fired alerts, narrowed by severity. The list previews exactly
// what will be ingested as the severity filter changes.
function AlertRulesCuration({ rules, severities, sev, onSevToggle, includePending, onIncludePending,
                              onConfirm, busy, summary }: {
  rules: AlertRule[]; severities: string[]; sev: Set<string>; onSevToggle: (s: string) => void;
  includePending: boolean; onIncludePending: (v: boolean) => void;
  onConfirm: () => void; busy: boolean; summary: string;
}) {
  const [q, setQ] = useState("");
  const shown = rules.filter((r) =>
    (!sev.size || sev.has(r.severity)) &&
    (!q || `${r.name} ${r.group}`.toLowerCase().includes(q.toLowerCase())));
  const stateBadge = (s: string) =>
    s === "firing" ? <span className="badge" style={{ background: "var(--err-soft)", color: "var(--err)" }}>firing</span>
    : s === "pending" ? <span className="badge" style={{ background: "var(--warn-soft)", color: "var(--warn)" }}>pending</span>
    : null;
  return (
    <div style={{ marginTop: 4 }}>
      <p className="subtitle" style={{ marginTop: 0 }}>
        {summary}; you'll ingest these as they fire. Optionally narrow by severity.
      </p>
      <div className="btnrow" style={{ marginBottom: 8, alignItems: "center", flexWrap: "wrap", gap: 6 }}>
        <span className="help" style={{ margin: 0 }}>severity:</span>
        {severities.map((s) => (
          <button type="button" key={s} className={sev.has(s) ? "primary" : ""} onClick={() => onSevToggle(s)}>{s}</button>
        ))}
      </div>
      <label className="explore-item" style={{ cursor: "pointer", marginBottom: 8 }}>
        <input type="checkbox" checked={includePending} style={{ marginRight: 8 }}
               onChange={(e) => onIncludePending(e.target.checked)} />
        also ingest <span className="mono" style={{ margin: "0 4px" }}>pending</span> alerts (still waiting out their
        <span className="mono" style={{ marginLeft: 4 }}>for:</span> duration)
      </label>
      <input type="text" placeholder="filter rules…" value={q} onChange={(e) => setQ(e.target.value)}
             style={{ marginBottom: 6 }} />
      <div className="fam-list" style={{ maxHeight: 300 }}>
        {shown.map((r, i) => (
          <div key={`${r.group}/${r.name}/${r.severity}/${i}`} className="fam-row"
               style={{ gridTemplateColumns: "minmax(0,1fr) auto auto", cursor: "default" }}>
            <span className="mono fam-name">{r.name}</span>
            {r.severity ? <span className="chip">{r.severity}</span> : <span />}
            {stateBadge(r.state) ?? <span />}
          </div>
        ))}
        {!shown.length && <div className="help" style={{ padding: 8 }}>no rules match</div>}
      </div>
      <div className="btnrow" style={{ marginTop: 10, alignItems: "center" }}>
        <button type="button" className="primary" disabled={busy} onClick={onConfirm}>
          Ingest {sev.size ? `${[...sev].join(", ")} ` : "all "}alerts →
        </button>
        <span className="help" style={{ margin: 0 }}>{shown.length} of {rules.length} rules shown</span>
      </div>
    </div>
  );
}

// Prometheus metric picker: two tabs (by name pattern / by label) that both add to one shared
// basket. No PromQL — the user picks metric names; the connector compiles the basket into a single
// {__name__=~"(a|b|…)"} selector on confirm.
function MetricBasket({ catalog, basket, onAdd, onRemove, fetchLabelMetrics, onConfirm, busy }: {
  catalog: { metrics: string[]; labels: string[] };
  basket: Set<string>;
  onAdd: (names: string[]) => void;
  onRemove: (name: string) => void;
  fetchLabelMetrics: (label: string) => Promise<string[]>;
  onConfirm: () => void;
  busy: boolean;
}) {
  const [tab, setTab] = useState<"name" | "label">("name");
  const [nameQ, setNameQ] = useState("");
  const [labelQ, setLabelQ] = useState("");
  const [pickedLabel, setPickedLabel] = useState<string>();
  const [labelMetrics, setLabelMetrics] = useState<string[]>();
  const [loadingLabel, setLoadingLabel] = useState(false);

  const nameMatches = useMemo(() => {
    const q = nameQ.trim().toLowerCase();
    if (!q) return [];
    const star = q.endsWith("*");
    const pat = star ? q.slice(0, -1) : q;
    return catalog.metrics
      .filter((m) => { const lm = m.toLowerCase(); return star ? lm.startsWith(pat) : lm.includes(pat); })
      .slice(0, 500);
  }, [nameQ, catalog.metrics]);

  const labelMatches = useMemo(() => {
    const q = labelQ.trim().toLowerCase();
    return catalog.labels.filter((l) => !q || l.toLowerCase().includes(q)).slice(0, 300);
  }, [labelQ, catalog.labels]);

  const pickLabel = async (l: string) => {
    setPickedLabel(l); setLoadingLabel(true); setLabelMetrics(undefined);
    try { setLabelMetrics(await fetchLabelMetrics(l)); } finally { setLoadingLabel(false); }
  };

  const ResultList = ({ items }: { items: string[] }) => (
    <>
      <div className="btnrow" style={{ margin: "6px 0", alignItems: "center" }}>
        <button type="button" disabled={!items.length} onClick={() => onAdd(items)}>
          add all {items.length}
        </button>
        <span className="help" style={{ margin: 0 }}>{items.length} match{items.length === 1 ? "" : "es"}
          {items.length === 500 ? " (showing first 500; narrow the pattern)" : ""}</span>
      </div>
      <div className="fam-list" style={{ maxHeight: 240 }}>
        {items.map((m) => (
          <label key={m} className="fam-row" style={{ gridTemplateColumns: "auto minmax(0,1fr) auto" }}>
            <input type="checkbox" checked={basket.has(m)}
                   onChange={() => (basket.has(m) ? onRemove(m) : onAdd([m]))} />
            <span className="mono fam-name">{m}</span>
            {basket.has(m) && <span className="badge ok">added</span>}
          </label>
        ))}
      </div>
    </>
  );

  return (
    <div style={{ marginTop: 4 }}>
      <p className="subtitle" style={{ marginTop: 0 }}>
        Choose the metrics to ingest; by name, or by a label they carry. No PromQL to write.
      </p>
      <div className="btnrow" style={{ gap: 6, marginBottom: 8 }}>
        <button type="button" className={tab === "name" ? "primary" : ""} onClick={() => setTab("name")}>
          By metric name
        </button>
        <button type="button" className={tab === "label" ? "primary" : ""} onClick={() => setTab("label")}>
          By label
        </button>
      </div>

      {tab === "name" && (
        <div>
          <input type="text" placeholder="type a metric name or prefix; e.g. node_* or clickhouse"
                 value={nameQ} onChange={(e) => setNameQ(e.target.value)} />
          {nameQ.trim()
            ? <ResultList items={nameMatches} />
            : <p className="help">{catalog.metrics.length.toLocaleString()} metrics available; start typing to filter (append <span className="mono">*</span> for a prefix match)</p>}
        </div>
      )}

      {tab === "label" && (
        <div>
          <input type="text" placeholder="filter labels; e.g. namespace, service, clickhouse_org"
                 value={labelQ} onChange={(e) => setLabelQ(e.target.value)} />
          <div className="fam-list" style={{ maxHeight: 150, marginTop: 6 }}>
            {labelMatches.map((l) => (
              <label key={l} className="fam-row" style={{ gridTemplateColumns: "minmax(0,1fr) auto", cursor: "pointer" }}
                     onClick={(e) => { e.preventDefault(); void pickLabel(l); }}>
                <span className="mono fam-name">{l}</span>
                {pickedLabel === l && <span className="badge">selected</span>}
              </label>
            ))}
          </div>
          {loadingLabel && <p className="help">loading metrics for <span className="mono">{pickedLabel}</span>…</p>}
          {labelMetrics && !loadingLabel && (
            <div style={{ marginTop: 8 }}>
              <p className="help" style={{ margin: 0 }}>
                metrics carrying <span className="mono">{pickedLabel}</span>:
              </p>
              <ResultList items={labelMetrics} />
            </div>
          )}
        </div>
      )}

      <div style={{ marginTop: 12, borderTop: "1px solid var(--line)", paddingTop: 10 }}>
        <div className="btnrow" style={{ alignItems: "center" }}>
          <strong>{basket.size} metric{basket.size === 1 ? "" : "s"} selected</strong>
          {basket.size > 0 && (
            <button type="button" className="linklike" onClick={() => [...basket].forEach(onRemove)}>clear</button>
          )}
          <button type="button" className="primary" style={{ marginLeft: "auto" }}
                  disabled={busy || basket.size === 0} onClick={onConfirm}>
            Use these {basket.size || ""} metrics →
          </button>
        </div>
        {basket.size > 0 && (
          <div style={{ maxHeight: 120, overflowY: "auto", marginTop: 6 }}>
            {[...basket].map((m) => (
              <span key={m} className="chip mono" style={{ margin: "2px 4px 2px 0", display: "inline-flex", alignItems: "center", gap: 4 }}>
                {m}
                <button type="button" onClick={() => onRemove(m)}
                        style={{ border: "none", background: "none", cursor: "pointer", padding: 0, color: "inherit", fontWeight: 700 }}>×</button>
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// Prometheus queries rendered compactly: one line per query (a whole-family {__name__=~"fam_.*"}
// query or a derived series), with a remove button — instead of a fat multi-input panel per row.
// Full hand-editing stays available via the raw ListFieldEditor in the Advanced group below.
function PromQueriesView({ rows, onChange }:
  { rows: Array<Record<string, string>>; onChange: (r: Array<Record<string, string>>) => void }) {
  const summarize = (q: Record<string, string>) => {
    if (q.by_name) {
      // inner of __name__=~"..." → either a single family (prefix_.*) or an (a|b|c) basket
      const inner = (q.promql?.match(/=~"(.*)"/)?.[1] ?? "").replace(/^\(|\)$/g, "");
      if (/^[A-Za-z0-9_]+_\.\*$/.test(inner)) {
        return { icon: "▦", title: inner.replace(/_\.\*$/, "_*"),
                 note: `whole family${q.exclude ? ` · excludes ${q.exclude}` : ""}` };
      }
      const names = inner.split("|").filter(Boolean);
      return { icon: "▦", title: `${names.length} metric${names.length === 1 ? "" : "s"}`,
               note: names.slice(0, 4).join(", ") + (names.length > 4 ? ", …" : "") };
    }
    return { icon: "ƒ", title: (q.text ?? "").replace(" {key}={val}", "") || q.event_type || "derived",
             note: q.promql ?? "" };
  };
  return (
    <div className="field">
      <span className="lbl">metrics to ingest</span>
      <span className="help" style={{ marginTop: 0, marginBottom: 6 }}>
        the metrics this source will ingest. Remove anything you don't want, or “change metrics” above.
      </span>
      {rows.length === 0 && (
        <div className="empty" style={{ padding: 10 }}>none yet; run Discover above and pick metrics</div>
      )}
      {rows.map((q, i) => {
        const s = summarize(q);
        return (
          <div key={i} className="conn-summary" style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
            <span aria-hidden style={{ opacity: 0.6, flex: "none" }}>{s.icon}</span>
            <span className="mono" style={{ flex: "none" }}>{s.title}</span>
            <span className="help" style={{ margin: 0, flex: 1, minWidth: 0, overflow: "hidden",
                                            textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{s.note}</span>
            <button type="button" className="danger" style={{ marginLeft: "auto", flex: "none" }}
                    onClick={() => onChange(rows.filter((_, j) => j !== i))}>remove</button>
          </div>
        );
      })}
    </div>
  );
}

// Postgres column selection as a checklist (once discover knows the table's columns). Emits the
// same comma-separated string the API uses: all columns checked -> "" (SELECT *); a subset ->
// "a,b,c". The cursor/key/time columns are always pulled, so they're shown checked + locked.
function ColumnPicker({ columns, value, mandatory, onChange }: {
  columns: string[]; value: string; mandatory: Set<string>; onChange: (v: string) => void;
}) {
  const [q, setQ] = useState("");
  const listed = value.trim()
    ? new Set(value.split(",").map((s) => s.trim()).filter(Boolean))
    : null;   // null = "all"
  const isChecked = (c: string) => mandatory.has(c) || listed === null || listed.has(c);
  const emit = (checked: Set<string>) => {
    mandatory.forEach((m) => checked.add(m));
    onChange(checked.size >= columns.length ? "" : columns.filter((c) => checked.has(c)).join(","));
  };
  const toggle = (c: string) => {
    const checked = new Set(columns.filter(isChecked));
    checked.has(c) ? checked.delete(c) : checked.add(c);
    emit(checked);
  };
  const nChecked = columns.filter(isChecked).length;
  const shown = columns.filter((c) => !q || c.toLowerCase().includes(q.toLowerCase()));
  return (
    <div>
      <div className="btnrow" style={{ marginBottom: 6, alignItems: "center" }}>
        <button type="button" onClick={() => onChange("")}>All</button>
        <button type="button" onClick={() => emit(new Set())}>None</button>
        <span className="help">
          {nChecked}/{columns.length} selected{nChecked >= columns.length ? "; pulls every column" : ""}
        </span>
      </div>
      {columns.length > 8 && (
        <input type="text" placeholder="filter columns…" value={q}
               onChange={(e) => setQ(e.target.value)} style={{ marginBottom: 6 }} />
      )}
      <div style={{ maxHeight: 220, overflowY: "auto", border: "1px solid var(--line)", borderRadius: 6, padding: 4 }}>
        {shown.map((c) => (
          <label key={c} className="explore-item"
                 style={{ cursor: mandatory.has(c) ? "default" : "pointer", opacity: mandatory.has(c) ? 0.7 : 1 }}>
            <input type="checkbox" checked={isChecked(c)} disabled={mandatory.has(c)}
                   onChange={() => toggle(c)} style={{ marginRight: 8 }} />
            <span className="mono">{c}</span>
            {mandatory.has(c) && <span className="badge push" style={{ marginLeft: 8 }} title="cursor/key/time; always pulled">always</span>}
          </label>
        ))}
      </div>
    </div>
  );
}

// Table-shaped discover result (postgres): every column with its type, badged with the role the
// proposal assigns it (cursor / key / label), so the user sees the possible values before applying.
function ColumnsProposalView({ proposal, onApply }: { proposal: ColumnsProposal; onApply: () => void }) {
  const cfg = proposal.proposed_config as { cursor_column?: string; key_column?: string;
                                            labels?: { field?: string }[] };
  const labelFields = new Set((cfg.labels ?? []).map((l) => l.field).filter(Boolean));
  const role = (col: string) =>
    col === cfg.cursor_column ? "cursor" : col === cfg.key_column ? "key"
      : labelFields.has(col) ? "label" : "";
  return (
    <div style={{ marginTop: 12 }}>
      <p className="subtitle" style={{ marginTop: 0 }}>{proposal.summary}</p>
      <table>
        <thead><tr><th>column</th><th style={{ width: 180 }}>type</th><th style={{ width: 90 }}>proposed as</th></tr></thead>
        <tbody>
          {proposal.columns.map((c) => (
            <tr key={c.name}>
              <td className="mono">{c.name}</td>
              <td className="help">{c.type}</td>
              <td>{role(c.name) && <span className="chip">{role(c.name)}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <button type="button" className="primary" style={{ marginTop: 10 }} onClick={onApply}>
        Apply to form
      </button>
      <span className="help" style={{ marginLeft: 10 }}>
        fills the fields below; review, Test connection, then Create
      </span>
    </div>
  );
}

function ProposalView({ proposal, onApply }: { proposal: DiscoverProposal; onApply: () => void }) {
  const k = proposal.suggested_key;
  const families = proposal.families ?? [];
  // group the sampled preview metrics under the family each belongs to, so the preview is one
  // collapsible block per family rather than one flat dump across all of them
  const famOf = (name: string) => families.find((f) => name.startsWith(f + "_")) ?? name.split("_")[0];
  const byFamily = new Map<string, typeof proposal.metrics>();
  for (const m of proposal.metrics) {
    const f = famOf(m.name);
    (byFamily.get(f) ?? byFamily.set(f, []).get(f)!).push(m);
  }
  return (
    <div style={{ marginTop: 12 }}>
      <div className="field" style={{ marginBottom: 12 }}>
        <span className="lbl">will ingest</span>
        <div>
          {families.map((f) => <span className="chip mono" key={f}>{f}_*</span>)}
          <span className="help" style={{ marginLeft: 8 }}>
            {proposal.summary.total_metrics} metrics
            {families.length > 1 ? ` · ${families.length} families` : ""}
            {" "}· each family is one query (histogram buckets excluded)
          </span>
        </div>
      </div>

      <div className="field" style={{ marginBottom: 12 }}>
        <span className="lbl">entity key</span>
        <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
          <div>
            <span className="chip mono">{k.name}</span>
            <span className="help" style={{ marginLeft: 8 }}>
              {k.cardinality} distinct {k.cardinality === 1 ? "entity" : "entities"}
            </span>
          </div>
          {k.values_preview.length > 0 && (
            <span className="help" style={{ margin: 0 }}>
              e.g. {k.values_preview.slice(0, 5).join(", ")}{k.cardinality > 5 ? ", …" : ""}
            </span>
          )}
          {k.alternatives.length > 0 && (
            <span className="help" style={{ margin: 0 }}>or key on: {k.alternatives.join(", ")}</span>
          )}
        </div>
      </div>

      <div className="field" style={{ marginBottom: 12 }}>
        <span className="lbl">preview</span>
        <div>
          {[...byFamily].map(([f, ms]) => (
            <details key={f} style={{ marginBottom: 4 }}>
              <summary className="help" style={{ cursor: "pointer", padding: "2px 0" }}>
                <span className="mono">{f}_*</span> · {ms.length} sampled
              </summary>
              <div style={{ maxHeight: 220, overflowY: "auto", margin: "6px 0 6px 16px" }}>
                <table>
                  <thead><tr><th>metric</th><th style={{ width: 84 }}>type</th><th>detail</th></tr></thead>
                  <tbody>
                    {ms.map((m) => (
                      <tr key={m.name}>
                        <td className="mono">{m.name}</td>
                        <td><span className="chip">{m.type}</span></td>
                        <td className="help">{m.help || m.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>
          ))}
        </div>
      </div>

      {proposal.derived_suggestions.length > 0 && (
        <div className="field" style={{ marginBottom: 12 }}>
          <span className="lbl">derived series</span>
          <span className="help" style={{ marginTop: 0, marginBottom: 6 }}>
            extra computed series added alongside the raw metrics
          </span>
          {proposal.derived_suggestions.map((d) => (
            <div key={d.id} style={{ marginBottom: 4 }}>
              <span className="chip">{d.label}</span>
              <span className="help" style={{ marginLeft: 8 }}>{d.reason}</span>
            </div>
          ))}
        </div>
      )}

      {proposal.proposed_labels.length > 0 && (
        <div className="field" style={{ marginBottom: 12 }}>
          <span className="lbl">labels</span>
          <div>{proposal.proposed_labels.map((l) => <span className="chip mono" key={l}>{l}</span>)}</div>
        </div>
      )}

      <button type="button" className="primary" onClick={onApply}>Apply to form</button>
      <span className="help" style={{ marginLeft: 10 }}>
        fills the fields below; review, Test connection, then Create
      </span>
    </div>
  );
}
