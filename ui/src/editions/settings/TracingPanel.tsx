import { useCallback, useEffect, useState } from "react";

import { api, type TracingStatus } from "../../api";
import { Banner, Button, Input, Loading, Panel, Select, SkeletonLines, StatusDot, Switch } from "../../components/edition";

// Where agents' traces go, on the edition kit (TR-1392). Factory Settings > Observability and the
// platform Settings page (/settings/advanced?tab=observability) both show this.

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";
const CODE = "font-mono text-[12px] text-foreground";

const messageOf = (e: unknown) => String((e as Error)?.message ?? e);
/** The daemon's refusal, as a sentence under the field. */
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** Where a value comes from, as the end of a sentence. Unlike the Slack token, a value saved here
 *  wins over the environment, and clearing it goes back to the environment. */
function origin(source: string): string {
  if (source === "console") return "saved here";
  if (source.startsWith("env:")) return `from ${source.slice(4)} in the daemon's environment`;
  if (source === "preset") return "the provider's default";
  if (source === "default") return "the default";
  return "";
}

const PROVIDERS = [
  { value: "rius", label: "Rius (GlassFlow)" },
  { value: "otlp", label: "Any OpenTelemetry endpoint (OTLP/HTTP)" },
];

type Body = Parameters<typeof api.setTracing>[0];

/** `title` and `intro` for a page with no heading of its own (the platform Settings page). */
export function TracingPanel({ title, intro = false }: { title?: string; intro?: boolean }) {
  const [st, setSt] = useState<TracingStatus>();
  const [provider, setProvider] = useState<string>();
  const [endpoint, setEndpoint] = useState<string>();
  const [apiKey, setApiKey] = useState("");
  const [headers, setHeaders] = useState("");
  const [busy, setBusy] = useState<"switch" | "save" | "clear">();
  const [loadErr, setLoadErr] = useState<string>();
  const [saveErr, setSaveErr] = useState<string>();

  const load = useCallback(() => api.tracingStatus()
    .then((s) => { setSt(s); setProvider(undefined); setEndpoint(undefined); setLoadErr(undefined); })
    .catch((e) => setLoadErr(messageOf(e))), []);
  useEffect(() => { void load(); }, [load]);

  const apply = async (body: Body, kind: "switch" | "save" | "clear") => {
    setBusy(kind); setSaveErr(undefined);
    try {
      await api.setTracing(body);
      setApiKey(""); setHeaders("");
      await load();
    } catch (e) {
      if (kind === "save") setSaveErr(sentence(messageOf(e)));
      else setLoadErr(messageOf(e));
    }
    setBusy(undefined);
  };

  if (!st) {
    return (
      <Panel title={title} data-testid="tracing">
        <div className="grid gap-4">
          {loadErr ? <Banner tone="error">{loadErr}</Banner>
            : <Loading label="Loading tracing"><SkeletonLines widths={["30%", "60%"]} /></Loading>}
        </div>
      </Panel>
    );
  }

  const prov = provider ?? st.provider;
  const ep = endpoint ?? (st.endpoint_source === "preset" ? "" : st.endpoint);
  const envOnly = st.provider_source.startsWith("env:") || st.key_source.startsWith("env:");
  const providerChanged = prov !== st.provider;
  const canSave = prov === "rius"
    ? Boolean(apiKey.trim()) || providerChanged
    : Boolean(ep.trim()) && (providerChanged || ep.trim() !== st.endpoint || Boolean(headers.trim()));
  const save = () => {
    if (!canSave) return;
    void apply(prov === "rius"
      ? { provider: "rius", ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}) }
      : { provider: "otlp", endpoint: ep.trim(), ...(headers.trim() ? { headers: headers.trim() } : {}) }, "save");
  };
  const enabledFrom = st.enabled_source.startsWith("env:") ? ` Set ${origin(st.enabled_source)}.` : "";
  // The daemon calls Rius "active" without a key (the endpoint is the provider's own), but Rius
  // refuses every trace sent without one, so on and keyless reads as a problem, not as on.
  const keyless = st.enabled && st.active && st.provider === "rius" && !st.key_configured;

  return (
    <Panel title={title} data-testid="tracing">
      <div className="grid gap-5">
        {intro && (
          <p className={TEXT}>
            Everything an agent does, and every Ask turn, becomes an OpenTelemetry trace: each model call with its
            prompt, answer, tokens and cost, and each tool call. Each agent is its own service, named{" "}
            <code className={CODE}>{st.instance}/&lt;agent&gt;</code>.
          </p>
        )}
        {loadErr && <Banner tone="error">{loadErr}</Banner>}

        <div className="grid gap-2">
          <Switch label="Send agent traces" checked={st.enabled} disabled={busy !== undefined} data-testid="tracing-switch"
                  onCheckedChange={(on) => void apply({ enabled: on }, "switch")} />
          <p className="m-0 flex items-baseline gap-2 text-sm" data-testid="tracing-state">
            <StatusDot tone={!st.enabled ? "quiet" : st.active && !keyless ? "ready" : "bad"} />
            <span>
              {!st.enabled ? "Off. Nothing is sent."
                : !st.active ? "On, but there is nowhere to send them. Choose where below and save."
                : keyless ? "On, but nothing reaches Rius until there is an API key. Paste one below."
                : st.provider === "rius" ? "On. Traces go to Rius."
                : <>On. Traces go to <code className={CODE} data-user-text>{st.endpoint}</code>.</>}
              {enabledFrom}
            </span>
          </p>
        </div>

        {envOnly && (
          <p className={TEXT}>
            Where traces go comes from the daemon's environment. A value saved here replaces it on this daemon;
            clearing it goes back to the environment.
          </p>
        )}

        <form className="grid max-w-[560px] gap-4" onSubmit={(e) => { e.preventDefault(); save(); }}>
          <Select label="Where traces go" options={PROVIDERS} value={prov} disabled={busy !== undefined}
                  hint={origin(st.provider_source) && sentence(`${origin(st.provider_source)}.`)}
                  onValueChange={(v) => { setProvider(v); setSaveErr(undefined); }} data-testid="tracing-provider" />

          {prov === "rius" ? (
            <div className="grid gap-2">
              <p className="m-0 flex items-baseline gap-2 text-sm" data-testid="tracing-key-state">
                <StatusDot tone={st.key_configured ? "ready" : "quiet"} />
                <span>{st.key_configured ? `API key set, ${origin(st.key_source)}.` : "No API key yet. Nothing reaches Rius until there is one."}</span>
              </p>
              <Input type="password" autoComplete="off" spellCheck={false} className="font-mono" label="Rius API key"
                     hint="Create one in the Rius console under Settings, API keys. Once saved it is never shown again."
                     error={saveErr} value={apiKey} onChange={(e) => { setApiKey(e.target.value); setSaveErr(undefined); }} />
              <p className="m-0 text-[13px]">
                <a href={st.rius_console_url} target="_blank" rel="noreferrer">Open the Rius console</a>
              </p>
            </div>
          ) : (
            <>
              <Input className="font-mono" spellCheck={false} label="Endpoint" error={saveErr}
                     hint={`An http(s) URL, for example https://collector:4318.${origin(st.endpoint_source) ? ` Now ${origin(st.endpoint_source)}.` : ""}`}
                     value={ep} onChange={(e) => { setEndpoint(e.target.value); setSaveErr(undefined); }} />
              <Input type="password" autoComplete="off" spellCheck={false} className="font-mono" label="Headers"
                     hint={`name=value, separated by commas. Once saved they are never shown again.${st.headers_configured ? ` Set, ${origin(st.headers_source)}.` : ""}`}
                     value={headers} onChange={(e) => setHeaders(e.target.value)} />
            </>
          )}

          <div className="flex flex-wrap gap-2">
            <Button type="submit" busy={busy === "save"} disabled={!canSave || (busy !== undefined && busy !== "save")}>Save</Button>
            {prov === "rius" && st.key_stored && (
              <Button variant="secondary" busy={busy === "clear"} disabled={busy !== undefined && busy !== "clear"}
                      onClick={() => void apply({ api_key: "" }, "clear")}>Clear the saved key</Button>
            )}
            {prov === "otlp" && st.headers_configured && st.headers_source === "console" && (
              <Button variant="secondary" busy={busy === "clear"} disabled={busy !== undefined && busy !== "clear"}
                      onClick={() => void apply({ headers: "" }, "clear")}>Clear the saved headers</Button>
            )}
          </div>
        </form>
      </div>
    </Panel>
  );
}
