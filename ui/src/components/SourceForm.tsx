import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "../api";
import { cn } from "@/lib/utils";
import { formatCount } from "../lib/number";
import type { ColumnsProposal, ConnectorField, ConnectorSpec, EnvScan, GithubCredential, TestResult } from "../types";
import { Banner, Button, Checkbox, Dialog, Input, Panel, Textarea } from "./edition";
import {
  AlertRulesCuration, ColumnPicker, ColumnsProposalView, MetricBasket, PromQueriesView, type AlertRule,
} from "./source-form/discover";
import { JsonTree, KeyValueRows, ListFieldEditor, ReferenceForm } from "./source-form/fields";
import { LabelsEditor, labelsToRows, rowToSpec, type Attachment, type LabelRow } from "./source-form/labels";
import { CODE, GROUP_LABEL, Pick, SuggestInput, TABLE, TEXT, cap } from "./source-form/parts";

// Structured push connectors know the shape of what they send, so a fresh connection starts with
// sensible label axes (the user can still edit or remove them).
const DEFAULT_LABELS: Record<string, Array<Record<string, unknown>>> = {
  // "source" is the name of Vercel's own field, not console copy
  vercel: [{ name: "project", field: "project", primary: true },
           { name: "environment", field: "environment" }, { name: "source", field: "source" }],   // words-ok
  otlp: [{ name: "service", field: "resourceAttributes.service.name", primary: true }],
};

// Connector-appropriate example names; the generic fallback suits metric-ish connections.
const NAME_PLACEHOLDER: Record<string, string> = {
  github: "my-repo-commits",
  postgres: "orders-table",
};

// What Discover does, per connector.
const DISCOVER_HINT: Record<string, string> = {
  docker_logs: "Lists the containers Tares can see. Pick one to fill in this form.",
  github: "Pick the repo above, then Discover reads its default branch and author labels.",
  postgres: "Enter the connection URL above, then Discover. It lists the tables it can see; pick one and it proposes the cursor, the key and the labels from its columns.",
  prometheus: "Enter the URL and any auth above, then Discover. It lists the metrics and labels so you can pick what to receive, by name or by label. No PromQL to write.",
  prometheus_alerts: "Enter the URL and any auth above, then Discover. It lists the alerting rules Prometheus already has, and Tares receives them as they go off, filtered by severity if you like.",
  http_poll: "Fill in the request above, then Discover. Tares calls the API once, finds the items in the reply and proposes the fields, labels, time and id from what came back.",
};
const DISCOVER_FALLBACK = "Fill in the URL above, then Discover proposes what to receive. No PromQL to write by hand.";

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
  default_key: "Fallback key",
};

/** "a value the daemon wrote" reads as a sentence: capitalised, with a full stop. */
const sentence = (s?: string) => {
  const t = s?.trim();
  if (!t) return "";
  return cap(t) + (/[.!?]$/.test(t) ? "" : ".");
};

/** The help under a field, in a person's words, where the daemon's own text is written for agents
 *  (it says entity, ingest, source). Keyed connector.field, and connector.field.item for a list's columns. */
const HELP: Record<string, string> = {
  "prometheus.default_key": "The key for series that carry no key label.",
  "prometheus.queries": "The metrics to receive, each a PromQL query.",
  "prometheus.queries.promql": "PromQL. A bare metric name keeps every value as it is.",
  "prometheus.queries.key_label": "The series label the key is read from, for example service.",
  "docker_logs.compose_file": "The path to docker-compose.yml, if compose starts the container.",
  "github.branch": "The branch to follow. Empty follows the repo's default branch, under its real name. One connection follows one branch; add one per branch to follow several.",
  "postgres.dsn": "postgresql://user:pass@host:port/dbname. The part after the slash picks the database; without it, Postgres uses a database named after the user. Tares must be able to reach it. Stored as a secret: hidden in the API and left out of exports.",
  "postgres.time_column": "The timestamp column that gives the event's time. Empty uses the cursor column when it is a timestamp, else the time Tares read the row.",
  "postgres.columns": "Comma-separated columns to pull, for example id,status,amount. Empty pulls every column. The cursor, key and time columns are always pulled, and database and table are available as fields too.",
};

/** A field's key as words, for a field the daemon gives no label: "bearer_token" reads as "bearer token", "url" as "URL". */
const words = (key: string) => key.split("_").map((w) => (w === "url" || w === "id" || w === "dsn" ? w.toUpperCase() : w)).join(" ");
const ADVANCED = "grid gap-4 [&>summary]:cursor-pointer [&>summary]:text-sm [&>summary]:font-semibold [&>summary]:text-foreground";
const SUMMARY_ROW = "flex flex-wrap items-center gap-x-4 gap-y-2 border border-border bg-muted/40 px-4 py-2.5 text-sm";
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
      else if (f.type === "json" || f.type === "map") v[f.name] = JSON.stringify(cur, null, 2);
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
  // http_poll: what Discover saw, so the mapping reads as decisions with the evidence beside them
  const [apiSample, setApiSample] = useState<{ fields: { name: string; example: unknown }[];
    sample_item: Record<string, unknown>; sample_reply: unknown; per_poll: number; payload_key?: string }>();
  const [mapByHand, setMapByHand] = useState(false);
  const [sampleOpen, setSampleOpen] = useState(false);
  const [observed, setObserved] = useState<{ sampled: number; fields: { name: string; coverage: number }[] }>();
  useEffect(() => {
    if (!initial?.name) return;
    api.sourceFields(initial.name)
      .then((p) => setObserved({ sampled: p.sampled,
                                 fields: p.fields.map((f) => ({ name: f.name, coverage: f.coverage })) }))
      .catch(() => {});
  }, [initial?.name]);
  const coverage = new Map((observed?.fields ?? []).map((f) => [f.name, f.coverage]));
  const sampleExample = new Map((apiSample?.fields ?? []).map((f) => [f.name, String(f.example)]));
  const labelFieldOpts = Array.from(new Set([
    ...(spec.provides ?? []).map((p) => p.name),
    ...(observed?.fields ?? []).map((f) => f.name),
    ...(apiSample?.fields ?? []).map((f) => f.name),   // what Discover saw in the reply
  ])).sort((a, b) => (coverage.get(b) ?? -1) - (coverage.get(a) ?? -1) || a.localeCompare(b));
  const labelFieldHints = observed
    ? Object.fromEntries(labelFieldOpts
        .filter((n) => coverage.has(n))
        .map((n) => [n, `${coverage.get(n)} / ${observed.sampled} events`]))
    : apiSample
    ? Object.fromEntries(labelFieldOpts.filter((n) => sampleExample.has(n)).map((n) => [n, `e.g. ${sampleExample.get(n)}`]))
    : undefined;
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
      if ((f.type === "json" || f.type === "map") && values[f.name]?.trim()) {
        try { JSON.parse(values[f.name]); } catch (e) { errs[f.name] = "Not valid JSON: " + ((e as Error).message ?? e); }
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
      if (!attachments.length) throw new Error("Add at least one document.");
      if (!name.trim()) throw required("name");
      // declare the union of label names as real Tares labels, so the source's Labels panel shows
      // them and triggers can correlate on them (field maps to the payload label surfaced by label_context)
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
              if (sf.required) throw new Error(`Every ${f.name === "queries" ? "query" : "row"} needs its ${words(sf.name)}`);
              continue;
            }
            obj[sf.name] = sf.type === "number" ? Number(raw) : raw;
          }
          out.push(obj);
        }
        if (f.required && !out.length) throw new Error(`Add at least one ${f.name === "queries" ? "query" : "row"}`);
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
        if (f.required) throw required(f.name);
        continue;
      }
      if (f.type === "json" || f.type === "map") config[f.name] = JSON.parse(raw);
      else if (f.type === "number") config[f.name] = Number(raw);
      else config[f.name] = raw;
    }
    const labels = labelRows.filter((r) => r.name.trim()).map(rowToSpec);
    if (labels.length) config.labels = labels;
    if (!name.trim()) throw required("name");
    return { name: name.trim(), type, connector, poll: poll.trim() || spec.poll || "5s", config };
  };

  // a list field with its help, and its columns' help, in a person's words
  const withHelp = (f: ConnectorField): ConnectorField => ({
    ...f, help: HELP[`${connector}.${f.name}`] ?? f.help,
    item: f.item?.map((sf) => ({ ...sf, help: HELP[`${connector}.${f.name}.${sf.name}`] ?? sf.help })),
  });
  // a field's name as a person reads it: the daemon's label, ours for postgres and prometheus, else its key in words
  const plainLabel = (f: ConnectorField) =>
    cap(f.label || (connector === "postgres" && PG_LABEL[f.name]) || (connector === "prometheus" && PROM_LABEL[f.name]) || words(f.name));
  // the field a required-check failed on: its row turns red until it is filled in
  const [missing, setMissing] = useState<string>();
  const required = (field: string) => {
    const f = spec.fields.find((x) => x.name === field);
    const err = new Error(`${f ? plainLabel(f) : cap(field)} is required`) as Error & { field?: string };
    err.field = field;
    return err;
  };
  const run = async (fn: () => Promise<void>) => {
    setBusy(true);
    setError(undefined);
    setMissing(undefined);
    try { await fn(); } catch (e) {
      setError(String((e as Error).message ?? e));
      setMissing((e as { field?: string }).field);
      // bring the field into view: the button was at the bottom, the field may be at the top
      requestAnimationFrame(() => formRef.current?.querySelector(".missing")?.scrollIntoView({ behavior: "smooth", block: "center" }));
    }
    setBusy(false);
  };
  const formRef = useRef<HTMLFormElement>(null);

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
      if (!raw) continue;
      // structured inputs go to Discover too: an API behind a required header needs them
      if (f.type === "json" || f.type === "map") { try { cfg[f.name] = JSON.parse(raw); } catch { /* shown as invalid */ } }
      else cfg[f.name] = f.type === "number" ? Number(raw) : raw;
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
      const pc = (p as { proposed_config?: Record<string, unknown> }).proposed_config ?? {};
      applyConfig(pc);
      const summary = (p as { summary?: unknown }).summary;
      if (typeof summary === "string") setTest({ ok: true, note: summary });
      if (connector === "http_poll") {
        const ps = p as unknown as { fields?: { name: string; example: unknown }[]; sample_item?: Record<string, unknown>; sample_reply?: unknown; per_poll?: number };
        setApiSample({ fields: ps.fields ?? [], sample_item: ps.sample_item ?? {}, sample_reply: ps.sample_reply ?? ps.sample_item ?? {}, per_poll: ps.per_poll ?? 1,
                       payload_key: typeof pc.payload_key === "string" ? pc.payload_key : undefined });
      }
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
        if (c[f.name] == null) continue;
        // structured values keep their JSON form; String() of an object is "[object Object]"
        nv[f.name] = (f.type === "json" || f.type === "map") && typeof c[f.name] === "object"
          ? JSON.stringify(c[f.name], null, f.type === "json" ? 2 : 0)
          : String(c[f.name]);
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

  // Editing labels re-processes every stored event for the source; warn before that happens.
  // Only meaningful when editing (initial present) — a fresh source has no stored events.
  const canonRows = (rs: LabelRow[]) =>
    JSON.stringify(rs.filter((r) => r.name.trim()).map(rowToSpec));
  const labelsChanged = !!initial && canonRows(labelRows) !== canonRows(labelsToRows(initial?.config?.labels));

  const fieldLabel = (f: ConnectorField) => plainLabel(f) + (f.required ? " (required)" : "");
  const fieldDetected = (f: ConnectorField) =>       // value came from Discover, not the user
    connector === "postgres" && !!pgColumns?.length && !!values[f.name]?.trim()
    && ["cursor_column", "time_column"].includes(f.name);
  // The hint under a field: the daemon's help (or what a stored secret does), then where the value came from.
  const fieldHint = (f: ConnectorField) => [
    secretSet(f)
      ? (cleared[f.name] ? "Removed when you save." : "A value is set. Type to replace it, or leave it blank to keep it.")
      : HELP[`${connector}.${f.name}`] ?? sentence(f.help),
    fieldDetected(f) ? "Detected by Discover." : "",
    highlight?.includes(f.name) ? "Yours to fill." : "",
  ].filter(Boolean).join(" ") || undefined;
  const isMissing = (f: string, v = values[f]) => missing === f && !v?.trim();
  const fieldError = (f: ConnectorField) => jsonErrors[f.name] ?? (isMissing(f.name) ? "Fill this in." : undefined);
  const set = (k: string) => (v: string) => setValues((cur) => ({ ...cur, [k]: v }));

  // One field as a kit control: postgres column checklist or single-column picker, a fixed list of
  // choices, a checkbox, header rows, a JSON text area, or a plain field (with secret handling).
  const fieldControl = (f: ConnectorField) => {
    const label = fieldLabel(f), hint = fieldHint(f), error = fieldError(f);
    if (connector === "postgres" && f.name === "columns" && pgColumns?.length) {
      // always keep the cursor/time columns + any column a label reads (the key is a primary label)
      const mandatory = new Set([
        values.cursor_column, values.time_column,
        ...labelRows.filter((r) => r.kind === "field").map((r) => r.value),
      ].map((v) => v?.trim()).filter(Boolean) as string[]);
      return (
        <div className="grid gap-1.5">
          <p className={GROUP_LABEL}>{label}</p>
          {hint && <p className="m-0 text-xs text-muted-foreground">{hint}</p>}
          <ColumnPicker columns={pgColumns} value={values[f.name] ?? ""} mandatory={mandatory} onChange={set(f.name)} />
        </div>
      );
    }
    if (connector === "postgres" && pgColumns?.length && ["cursor_column", "time_column"].includes(f.name)) {
      const cur = values[f.name] ?? "";
      const cols = pgColumns.includes(cur) || !cur ? pgColumns : [cur, ...pgColumns];   // keep a stale value selectable
      return <Pick label={label} hint={hint} error={error} value={cur} placeholder="Pick a column"
                   options={f.required ? cols : ["", ...cols]} onChange={set(f.name)} />;
    }
    if (f.choices?.length) {
      // the daemon refuses any other value, so the form only offers these
      const cur = values[f.name] ?? "";
      const opts = [...(!f.required && f.default == null ? [""] : []),
                    ...(cur && !f.choices.includes(cur) ? [cur] : []), ...f.choices];
      return <Pick label={label} hint={hint} error={error} value={cur} options={opts} onChange={set(f.name)} />;
    }
    if (f.type === "bool")
      return <Checkbox label={label} hint={hint} checked={values[f.name] === "true"}
                       onCheckedChange={(c) => set(f.name)(c === true ? "true" : "")} />;
    if (f.type === "map")
      return (
        <div className="grid gap-1.5">
          <p className={GROUP_LABEL}>{label}</p>
          {hint && <p className="m-0 text-xs text-muted-foreground">{hint}</p>}
          <KeyValueRows value={values[f.name] ?? ""} noun={f.name === "headers" ? "header" : "entry"} onChange={set(f.name)} />
        </div>
      );
    if (f.type === "json")
      return <Textarea label={label} hint={hint} error={error} className="min-h-32 font-mono text-[12px]" spellCheck={false}
                       value={values[f.name]} onChange={(e) => set(f.name)(e.target.value)} />;
    return (
      <>
        <Input label={label} hint={hint} error={error}
               type={f.secret ? "password" : f.type === "number" ? "number" : "text"}
               value={values[f.name]} autoComplete={f.secret ? "off" : undefined}
               disabled={secretSet(f) && cleared[f.name]}
               placeholder={secretSet(f) && !cleared[f.name] ? "Leave blank to keep" : undefined}
               onChange={(e) => set(f.name)(e.target.value)} />
        {secretSet(f) && (
          <div>
            {cleared[f.name]
              ? <Button variant="quiet" onClick={() => setCleared({ ...cleared, [f.name]: false })}>Keep the stored value</Button>
              : <Button variant="quiet" className="text-err hover:text-err"
                        onClick={() => { setCleared({ ...cleared, [f.name]: true }); setValues({ ...values, [f.name]: "" }); }}>
                  Remove the stored value
                </Button>}
          </div>
        )}
      </>
    );
  };

  // One field, in a wrapper the required-check can scroll to. (List fields keep their own editor.)
  const renderField = (f: ConnectorField) => {
    if (f.type === "list")
      return <ListFieldEditor key={f.name} field={f} rows={rows[f.name] ?? []}
                              onChange={(r) => setRows({ ...rows, [f.name]: r })} />;
    return (
      <div key={f.name} data-testid={`field-${f.name}`} className={cn("grid min-w-0 gap-1.5", isMissing(f.name) && "missing")}>
        {fieldControl(f)}
      </div>
    );
  };

  // GitHub: access (a stored token, or a new one) and the repo picked from what it can see, in
  // place of the generic repo / credential / token rows.
  const renderGithubFields = () => {
    const creds = ghCreds ?? [];
    const credOpts = creds.map((c) => c.name);
    const credLabels = Object.fromEntries(creds.map((c) => [c.name, c.account ? `${c.name} (${c.account})` : c.name]));
    const stored = !!values.credential && !ghNew;
    const addingNew = ghNew || (ghCreds !== undefined && creds.length === 0 && !ghOwnToken);
    const repoField = spec.fields.find((f) => f.name === "repo")!;
    const cancelNew = () => { setGhNew(false); setValues((v) => ({ ...v, token: "" })); };
    return (
      <>
        <div className="grid min-w-0 gap-3" data-testid="github-access">
          <div className="grid gap-1.5">
            <p className={GROUP_LABEL}>Access</p>
            <p className="m-0 text-xs text-muted-foreground">
              {stored ? "A token stored in this cell. The connection reads it at every poll, so rotating it under Settings rotates this connection too."
                : addingNew ? "A GitHub token. Private repos need one, and without one GitHub allows 60 requests an hour."
                : ghOwnToken ? "This connection carries its own token."
                : "No token: public repos only, 60 requests an hour."}
            </p>
          </div>
          {ghCreds === undefined && <p className={TEXT}>Loading the stored tokens…</p>}
          {ghCreds !== undefined && !addingNew && (
            <div className="grid items-end gap-3 sm:grid-cols-[minmax(0,1fr)_auto_auto]">
              {creds.length > 0 ? (
                <Pick label="Stored token" value={values.credential} onChange={ghPick} labels={credLabels}
                      options={values.credential && !credOpts.includes(values.credential) ? [values.credential, ...credOpts] : credOpts} />
              ) : <span />}
              {stored ? <Button variant="secondary" busy={ghTest?.busy} onClick={ghTestCred}>Test</Button> : <span />}
              <Button variant="secondary" onClick={() => { setGhNew(true); setValues((v) => ({ ...v, token: "" })); }}>Add a new token</Button>
            </div>
          )}
          {ghTest && !ghTest.busy && !addingNew && (ghTest.ok
            ? <p className={TEXT} data-testid="github-test">Signed in as <span className={CODE} data-user-text>{ghTest.login}</span>.</p>
            : <Banner tone="error" data-testid="github-test"><span data-user-text>{sentence(ghTest.error) || "The test failed."}</span></Banner>)}
          {ghOwnToken && !addingNew && !stored && (
            <p className={TEXT}>Kept as it is{creds.length > 0 ? ". Pick a stored token above to use that instead." : "."}</p>
          )}
          {addingNew && (
            <div className="grid gap-3">
              <Input label="GitHub token" type="password" className="font-mono" autoComplete="new-password" data-1p-ignore data-lpignore="true"
                     value={values.token} placeholder="github_pat_…"
                     onChange={(e) => setValues({ ...values, token: e.target.value, credential: "" })} />
              <div>
                <Dialog title="Token permissions" description="What the GitHub token needs, to read the repos this cell watches."
                        trigger={<Button variant="quiet">Which permissions does the token need?</Button>}>
                  <p className={TEXT}>
                    Create a fine-grained personal access token on GitHub (Settings, Developer settings, Personal access tokens,
                    Fine-grained tokens). Under Repository access pick the repos this cell should read. Repository permissions:
                    Contents, Read-only, to read commits and diffs; Metadata, Read-only, added by GitHub automatically, to list the repos.
                    A token that agents also write with needs Contents and Pull requests as Read and write.
                  </p>
                </Dialog>
              </div>
              <Checkbox label="Save it in this cell, so other connections and agents can use it too"
                        checked={ghSave} onCheckedChange={(c) => setGhSave(c === true)} />
              {ghSave ? (
                <div className="grid items-end gap-3 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)_auto_auto]">
                  <Input label="Token name" autoComplete="off" data-1p-ignore data-lpignore="true" value={ghName}
                         onChange={(e) => setGhName(e.target.value)} />
                  <Input label="GitHub Enterprise API URL (optional)" autoComplete="off" data-1p-ignore data-lpignore="true"
                         className="font-mono" value={ghApi} onChange={(e) => setGhApi(e.target.value)} />
                  <Button variant="primary" disabled={busy || !values.token.trim() || !ghName.trim()} onClick={ghSaveToken}>Save token</Button>
                  {creds.length > 0 ? <Button variant="secondary" onClick={cancelNew}>Cancel</Button> : <span />}
                </div>
              ) : (
                <div className="flex flex-wrap items-center gap-3">
                  <p className={TEXT}>Used by this connection only. It is stored with the connection and never shown again.</p>
                  {creds.length > 0 && <Button variant="secondary" onClick={cancelNew}>Cancel</Button>}
                </div>
              )}
            </div>
          )}
        </div>
        <div data-testid="field-repo" className={cn("grid min-w-0 gap-1.5", isMissing("repo") && "missing")}>
          {(() => {
            const hint = stored
              ? ghRepos ? `Pick one of the ${formatCount(ghRepos.length)} repos the token can see, or type owner/name.`
                : ghReposErr ? "Could not list the token's repos. Type owner/name."
                : "Listing the repos the token can see…"
              : HELP["github.repo"] ?? (sentence(repoField.help) || undefined);
            const error = isMissing("repo") ? "Fill this in." : undefined;
            return stored && ghRepos ? (
              <SuggestInput label="Repo (required)" hint={hint} error={error} className="font-mono" value={values.repo} placeholder="owner/name"
                            options={ghRepos.map((r) => r.full_name)}
                            hints={Object.fromEntries(ghRepos.map((r) => [r.full_name, `${r.private ? "Private" : "Public"}, ${r.default_branch}`]))}
                            onChange={(e) => setValues({ ...values, repo: e.target.value.trim() })} />
            ) : (
              <Input label="Repo (required)" hint={hint} error={error} className="font-mono" value={values.repo} placeholder="owner/name"
                     autoComplete="off" onChange={(e) => setValues({ ...values, repo: e.target.value })} />
            );
          })()}
        </div>
      </>
    );
  };

  // HTTP API: the request in the order a person thinks about it. How (method), where (url),
  // who am I (a credential, only if the API wants one, as one header name and value), anything
  // else the API wants (more headers), what to send (the body, POST only).
  const [credOn, setCredOn] = useState<boolean>(() => !!initial?.config?.credential
    || !!(initial?.config?.credential_header && initial.config.credential_header !== "Authorization"));
  const renderHttpPollFields = (fields: ConnectorField[]) => {
    const by = Object.fromEntries(fields.map((f) => [f.name, f]));
    const credField = by.credential, headerField = by.credential_header;
    const turnCredOff = () => {
      setCredOn(false);
      setValues({ ...values, credential: "", credential_header: "" });
      if (credField && secretSet(credField)) setCleared({ ...cleared, credential: true });
    };
    return (
      <>
        {by.method && renderField(by.method)}
        {by.url && renderField(by.url)}
        {credField && headerField && (
          <div className="grid min-w-0 gap-3" data-testid="http-credentials">
            <Pick label="Credentials" hint="Does the API want a key or a token? It is sent as one request header and stored as a secret."
                  value={credOn ? "yes" : "no"} options={["no", "yes"]} labels={{ no: "None", yes: "A key or a token" }}
                  onChange={(v) => (v === "yes" ? setCredOn(true) : turnCredOff())} />
            {credOn && (
              <div className="grid items-start gap-3 sm:grid-cols-[minmax(0,12rem)_minmax(0,1fr)]">
                <Input label="Header name" value={values.credential_header ?? ""} placeholder="Authorization" className="font-mono"
                       onChange={(e) => setValues({ ...values, credential_header: e.target.value })} />
                <Input label="Header value" type="password" autoComplete="off" value={values.credential ?? ""}
                       hint="The whole value: Bearer xyz for a token, or the key when the API says X-API-Key."
                       disabled={secretSet(credField) && cleared.credential}
                       placeholder={secretSet(credField) ? "Leave blank to keep" : "Bearer xyz, or the API key"}
                       onChange={(e) => setValues({ ...values, credential: e.target.value })} />
              </div>
            )}
          </div>
        )}
        {by.headers && (
          <div className="grid gap-1.5" data-testid="field-headers">
            <p className={GROUP_LABEL}>Additional headers</p>
            <p className="m-0 text-xs text-muted-foreground">Anything else the API wants on every request, for example Accept: application/json.</p>
            <KeyValueRows value={values.headers ?? ""} noun="header" onChange={set("headers")} />
          </div>
        )}
        {by.body && (values.method || "GET").toUpperCase() === "POST" && renderField(by.body)}
        {fields.filter((f) => !["method", "url", "credential", "credential_header", "headers", "body"].includes(f.name)).map(renderField)}
      </>
    );
  };

  // HTTP API, after Discover: how a reply becomes events, as decisions already made from the
  // sample, each with the value that justified it, editable in place. Before Discover there is
  // nothing to decide on, so the section says so instead of showing blanks with internal names.
  const renderHttpPollMapping = (fields: ConnectorField[]) => {
    const mapping = new Set(["payload_key", "event_time_field", "id_field", "event_type", "event_type_field", "text_template"]);
    const rest = fields.filter((f) => !mapping.has(f.name));
    if (!apiSample && !mapByHand) {
      return (
        <>
          <div className="grid gap-1.5" data-testid="http-mapping-empty">
            <p className={GROUP_LABEL}>What becomes an event</p>
            <p className={TEXT}>Discover above reads one real reply and fills this in from it.</p>
            <div><Button variant="quiet" onClick={() => setMapByHand(true)}>Set it by hand instead</Button></div>
          </div>
          {rest.map(renderField)}
        </>
      );
    }
    if (!apiSample) return <>{fields.map(renderField)}</>;
    const names = apiSample.fields.map((f) => f.name);
    const example = (n: string) => { const f = apiSample.fields.find((x) => x.name === n); return f ? String(f.example) : ""; };
    const items = values.payload_key?.trim();
    const preview = renderTemplate(values.text_template ?? "", apiSample.sample_item);
    const fixedType = !(values.event_type_field?.trim());
    return (
      <>
        <details className={cn(ADVANCED, "border border-border bg-muted/40 p-4")} data-testid="sample-reply"
                 open={sampleOpen} onToggle={(e) => setSampleOpen((e.target as HTMLDetailsElement).open)}>
          <summary>What Tares fetched ({formatCount(apiSample.fields.length)} fields)</summary>
          <p className={TEXT}>
            One reply from the API, as it came back
            {apiSample.per_poll > 1 ? `; lists cut to their first entry, ${formatCount(apiSample.per_poll)} in the real reply` : ""}.
            The choices below are read from it.
          </p>
          <div data-user-text><JsonTree value={apiSample.sample_reply} /></div>
        </details>
        <Input label="Payload key" className="font-mono" value={values.payload_key ?? ""} placeholder="Empty for the entire JSON"
               hint={(items ? `Discover found a list at ${items}, ${formatCount(apiSample.per_poll)} entries, one event each. ` : "")
                 + "The key in the reply that holds the payload, for example user or data.items. An object there is the event, a list is one event per entry."}
               onChange={(e) => set("payload_key")(e.target.value)} />
        <Pick label="Time of the event" value={values.event_time_field ?? ""} options={["", ...names]} labels={{ "": "The poll time" }}
              hint={values.event_time_field ? `From the item, for example ${example(values.event_time_field)}.` : "When Tares polled."}
              onChange={set("event_time_field")} />
        <Pick label="Deduplication key" value={values.id_field ?? ""} options={["", ...names]} labels={{ "": "None, store every poll" }}
              hint={values.id_field
                ? (values.id_field === values.event_time_field
                  ? "The reading's own time, so a reading is stored once however often Tares polls."
                  : `The entry's ${values.id_field.split(".").pop()}, so an entry already stored is not stored again.`)
                : "A field whose value is the same for a reading already stored; those are skipped."}
              onChange={set("id_field")} />
        <div className="grid items-start gap-3 sm:grid-cols-2" data-testid="event-kind">
          <Pick label="Kind of event" hint="The word rules filter on and count by." value={values.event_type_field ?? ""}
                options={["", ...names]} labels={{ "": "The same for every event" }} onChange={set("event_type_field")} />
          {fixedType
            ? <Input label="Event type" className="font-mono" value={values.event_type ?? ""} placeholder="api_reading"
                     onChange={(e) => set("event_type")(e.target.value)} />
            : <p className={cn(TEXT, "sm:pt-6")}>For example <span className={CODE} data-user-text>{example(values.event_type_field!)}</span>.</p>}
        </div>
        <div className="grid gap-1.5">
          <Input label="Event summary" className="font-mono" value={values.text_template ?? ""} placeholder="wind {current_weather.windspeed} km/h"
                 hint="One line per event, with values from the payload in braces. Empty stores the payload's JSON."
                 onChange={(e) => set("text_template")(e.target.value)} />
          <p className={TEXT} data-testid="summary-preview">
            Reads as: <span className={cn(CODE, "break-all")} data-user-text>{preview || JSON.stringify(apiSample.sample_item).slice(0, 120)}</span>
          </p>
        </div>
        {rest.map(renderField)}
      </>
    );
  };

  // Prometheus: the discovered queries as a compact list, with the fallback-key field + a raw
  // query editor tucked into a collapsed Advanced group (so the form isn't 80 fat panels tall).
  const renderPromFields = (fields: ConnectorField[]) => {
    const queriesField = fields.find((f) => f.name === "queries");
    const rest = fields.filter((f) => f.name !== "queries");
    return (
      <>
        {queriesField && <PromQueriesView rows={rows.queries ?? []} onChange={(r) => setRows({ ...rows, queries: r })} />}
        <details className={ADVANCED} data-testid="advanced">
          <summary>Advanced</summary>
          {rest.map(renderField)}
          {queriesField && (
            <ListFieldEditor field={withHelp(queriesField)} rows={rows.queries ?? []} onChange={(r) => setRows({ ...rows, queries: r })} />
          )}
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
        <div className="grid gap-1.5" data-testid="pg-poll">
          <p className={GROUP_LABEL}>
            {found ? <>What Tares reads from <code className={CODE} data-user-text>{values.table || "the table"}</code></> : "How to poll the table"}
          </p>
          <p className={TEXT}>{found
            ? "Discover picked these from the columns; change any that are not right."
            : "Discover above fills these from the table, or set them by hand."}</p>
        </div>
        {main.map(renderField)}
        {advanced.length > 0 && (
          <details className={ADVANCED} data-testid="advanced">
            <summary>Advanced</summary>
            {advanced.map(renderField)}
          </details>
        )}
      </>
    );
  };

  // Where the connection points, once Discover has confirmed it: the database (and host) or the URL's host.
  const connectedTo = () => {
    try {
      if (connector === "postgres") {
        const u = new URL(values.dsn); const db = u.pathname.replace(/^\//, "");
        return <> to <code className={CODE} data-user-text>{db || u.hostname}</code>{db && u.hostname ? <>, <span data-user-text>{u.hostname}</span></> : null}</>;
      }
      return <> to <code className={CODE} data-user-text>{new URL(values.url).host}</code></>;
    } catch { return null; }
  };
  const ready = !(connector === "prometheus" && !metricsConfirmed) && !(connector === "prometheus_alerts" && !alertsConfirmed);

  return (
    <form ref={formRef} className="grid min-w-0 max-w-3xl gap-5" data-testid="connection-form"
          onSubmit={(e) => { e.preventDefault(); run(async () => onSubmit(build())); }}>

      <div data-testid="field-name" className={cn("grid max-w-sm gap-1.5", isMissing("name", name) && "missing")}>
        <Input label="Name (required)" value={name} disabled={lockName}
               placeholder={NAME_PLACEHOLDER[connector] ?? "metrics"}
               error={isMissing("name", name) ? "Fill this in." : undefined}
               onChange={(e) => setName(e.target.value)} />
      </div>

      {spec.mode === "poll" && (() => {
        // split the stored "5m" string into a number + unit so the user can't type a malformed
        // duration; recombine on change. Save-time validation (_check_duration) still applies.
        const pm = /^\s*(\d+(?:\.\d+)?)\s*([smh])\s*$/.exec(poll);
        const num = pm ? pm[1] : "5";
        const unit = pm ? pm[2] : "s";
        return (
          <div className="grid max-w-sm grid-cols-2 items-start gap-3" data-testid="poll-interval">
            <Input label="Poll every" type="number" min="1" step="1" value={num}
                   onChange={(e) => setPoll(`${e.target.value}${unit}`)} />
            <Pick label="Unit" value={unit} onChange={(v) => setPoll(`${num}${v}`)}
                  options={["s", "m", "h"]} labels={{ s: "Seconds", m: "Minutes", h: "Hours" }} />
          </div>
        );
      })()}

      {spec.discover && !editConn
        && ((connector === "postgres" && pgColumns?.length) || (connector === "prometheus" && catalog)
            || (connector === "prometheus_alerts" && alertDiscover)) ? (
        <div className={SUMMARY_ROW} data-testid="conn-summary">
          <span className="min-w-0 flex-1">Connected{connectedTo()}.</span>
          <Button variant="quiet" onClick={() => setEditConn(true)}>Edit connection</Button>
        </div>
      ) : connector === "reference" ? (
        <ReferenceForm attachments={refAttachments} setAttachments={setRefAttachments} />
      ) : connector === "github" ? (
        renderGithubFields()
      ) : (
        connector === "http_poll"
          ? renderHttpPollFields(spec.fields.filter((f) => f.discover_input))
          : (spec.discover ? spec.fields.filter((f) => f.discover_input) : spec.fields).map(renderField)
      )}

      {/* postgres keeps the panel while a columns proposal waits to be applied: it collapses the
          connection as the columns arrive, and the proposal lives in this panel */}
      {spec.discover && !(connector === "postgres" && pgColumns?.length && !editConn && !colProposal)
        && !(connector === "prometheus" && metricsConfirmed)
        && !(connector === "prometheus_alerts" && alertsConfirmed) && (
        <Panel title="Discover" data-testid="discover-panel">
          <div className="grid gap-4">
            {!(connector === "prometheus" && catalog) && !(connector === "prometheus_alerts" && alertDiscover) && !colProposal && (
              <div className="grid gap-2">
                <p className={TEXT}>{DISCOVER_HINT[connector] ?? DISCOVER_FALLBACK}</p>
                <div>
                  <Button variant="secondary" busy={discovering} disabled={busy && !discovering} onClick={() => discover()}>
                    {connector === "docker_logs" ? "Discover containers" : "Discover"}
                  </Button>
                </div>
              </div>
            )}
            {discoverErr && <Banner tone="error" data-testid="discover-error"><span data-user-text>{sentence(discoverErr)}</span></Banner>}
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
              <table className={TABLE} data-testid="pg-tables">
                <thead><tr><th>Table</th><th><span className="sr-only">Pick</span></th></tr></thead>
                <tbody>
                  {tables.map((t) => (
                    <tr key={t}>
                      <td className="font-mono" data-user-text>{t}</td>
                      <td className="text-right">
                        <Button variant="secondary" disabled={busy} aria-label={`Use ${t}`} onClick={() => pickTable(t)}>Use this</Button>
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
            {containers && (containers.length === 0
              ? <p className={TEXT}>No containers found.</p>
              : (
                <table className={TABLE} data-testid="containers">
                  <thead><tr><th>Service</th><th>Image</th><th><span className="sr-only">Pick</span></th></tr></thead>
                  <tbody>
                    {containers.map((c) => (
                      <tr key={c.name}>
                        <td className="font-mono" data-user-text>{c.service}</td>
                        <td className="break-all text-muted-foreground" data-user-text>{c.image}</td>
                        <td className="text-right">
                          <Button variant="secondary" aria-label={`Use ${c.service}`} onClick={() => pickContainer(c)}>Use this</Button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ))}
          </div>
        </Panel>
      )}

      {connector === "prometheus" && metricsConfirmed && (
        <div className={SUMMARY_ROW} data-testid="metrics-summary">
          <span className="min-w-0 flex-1">{formatCount(basket.size)} {basket.size === 1 ? "metric" : "metrics"} chosen.</span>
          <Button variant="quiet" onClick={() => setMetricsConfirmed(false)}>Change metrics</Button>
        </div>
      )}

      {connector === "prometheus_alerts" && alertsConfirmed && (
        <div className={SUMMARY_ROW} data-testid="alerts-summary">
          <span className="min-w-0 flex-1">
            Receiving {alertSev.size ? [...alertSev].join(", ") : "all"} alerts
            {values.include_pending === "true" ? ", pending ones included" : ""}.
          </span>
          <Button variant="quiet" onClick={() => setAlertsConfirmed(false)}>Change filter</Button>
        </div>
      )}

      {spec.discover && (connector === "postgres"
        ? renderPgFields(spec.fields.filter((f) => !f.discover_input))
        : connector === "prometheus"
        ? (metricsConfirmed ? renderPromFields(spec.fields.filter((f) => !f.discover_input)) : null)
        : connector === "prometheus_alerts"
        ? null   // include_pending + severities are set in the curation section
        : connector === "http_poll"
        ? renderHttpPollMapping(spec.fields.filter((f) => !f.discover_input))
        : spec.fields.filter((f) => !f.discover_input).map(renderField))}

      {connector !== "reference" && ready && (
        <LabelsEditor rows={labelRows} onChange={setLabelRows} sourceName={initial?.name}
                      fields={labelFieldOpts} fieldHints={labelFieldHints}
                      examples={apiSample ? Object.fromEntries(sampleExample) : undefined} />
      )}

      {labelsChanged && (
        <Banner data-testid="labels-changed">
          Label changes apply to new events from now on. Events already stored keep the labels they arrived with.
        </Banner>
      )}

      {/* Outcomes sit with the buttons that cause them: a test result or a save error at the top
          of a long form is off screen when the button was pressed at the bottom. */}
      {error && <Banner tone="error" data-testid="form-error"><span data-user-text>{sentence(error)}</span></Banner>}
      {test && (test.ok ? (
        <div className="grid gap-2" data-testid="test-result">
          <Banner>
            <span>The connection works. <span data-user-text>{test.note ? sentence(test.note) : `${formatCount(test.events ?? 0)} ${test.events === 1 ? "event" : "events"} on a test poll.`}</span></span>
          </Banner>
          {test.sample?.length ? (
            <pre className="m-0 max-h-64 overflow-auto border border-border bg-muted/40 p-3 font-mono text-[12px] text-foreground" data-user-text>
              {test.sample.join("\n")}
            </pre>
          ) : null}
        </div>
      ) : (
        <Banner tone="error" data-testid="test-result"><span>The test failed: <span data-user-text>{sentence(test.error)}</span></span></Banner>
      ))}
      {ready && (
        <div className="flex flex-wrap gap-3">
          <Button type="submit" variant="primary" busy={busy && !discovering} disabled={busy || Object.keys(jsonErrors).length > 0}>
            {submitLabel}
          </Button>
          {spec.mode === "poll" && (
            <Button variant="secondary" disabled={busy}
                    onClick={() => run(async () => setTest(await api.testSource(build())))}>
              Test connection
            </Button>
          )}
        </div>
      )}
    </form>
  );
}

function renderTemplate(template: string, item: Record<string, unknown>): string {
  if (!template) return "";
  let missing = false;
  const out = template.replace(/\{([A-Za-z0-9_.]+)\}/g, (_, path: string) => {
    let cur: unknown = item;
    for (const part of path.split(".")) {
      cur = cur && typeof cur === "object" ? (cur as Record<string, unknown>)[part] : undefined;
    }
    if (cur === undefined || cur === null) { missing = true; return ""; }
    return String(cur);
  });
  return missing ? "" : out;
}
