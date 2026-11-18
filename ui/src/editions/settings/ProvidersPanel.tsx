import { useCallback, useEffect, useState } from "react";

import { api } from "../../api";
import {
  Banner, Button, Collapse, Dialog, EmptyState, Input, ListRow, Panel, Select, SkeletonRows, StatusDot, StatusLabel,
} from "../../components/edition";
import { ageWords } from "../../lib/time";
import type { ModelProvider, ModelProviders } from "../../types";

// What agents and Ask run on, on the edition kit (TR-1392). Factory Settings > Models > Providers and
// the platform Settings page (/settings/advanced?tab=anthropic, which the AI SRE edition opens) both
// show this. A cell holds Anthropic, OpenAI or any OpenAI-compatible endpoint (LiteLLM, OpenRouter,
// Ollama, vLLM); one is the default, what Ask uses and what an agent without a provider of its own
// runs on. The Anthropic entry is the older key + gateway settings under another door, so a
// deployment that fills those shows up here unchanged. No key ever comes back from the daemon.

const TEXT = "m-0 text-[13px] leading-relaxed text-muted-foreground";
const CODE = "font-mono text-[12px] text-foreground";
const FORM = "grid gap-4 border border-border bg-muted/40 p-4";

const messageOf = (e: unknown) => String((e as Error)?.message ?? e);
/** The daemon's refusal, as a sentence. */
const sentence = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);
const lower = (s: string) => (s ? s[0].toLowerCase() + s.slice(1) : s);
const plural =(n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;

type Kind = ModelProvider["kind"];
const PLATFORM = "platform";

const KIND_HELP: Record<Kind, string> = {
  anthropic: "Claude models with your Anthropic API key.",
  openai: "GPT models with your OpenAI API key.",
  openai_compatible: "Anything that speaks OpenAI chat completions. The models it lists are what agents can pick.",
};

const PRESETS: { id: string; label: string; kind: Kind; name: string; base_url: string; key: boolean }[] = [
  { id: "anthropic", label: "Anthropic", kind: "anthropic", name: "Anthropic", base_url: "", key: true },
  { id: "openai", label: "OpenAI", kind: "openai", name: "OpenAI", base_url: "", key: true },
  { id: "litellm", label: "LiteLLM", kind: "openai_compatible", name: "LiteLLM", base_url: "http://localhost:4000/v1", key: true },
  { id: "openrouter", label: "OpenRouter", kind: "openai_compatible", name: "OpenRouter", base_url: "https://openrouter.ai/api/v1", key: true },
  { id: "xai", label: "xAI (Grok)", kind: "openai_compatible", name: "xAI", base_url: "https://api.x.ai/v1", key: true },
  { id: "ollama", label: "Ollama", kind: "openai_compatible", name: "Ollama", base_url: "http://localhost:11434/v1", key: false },
  { id: "other", label: "Other OpenAI-compatible", kind: "openai_compatible", name: "", base_url: "", key: true },
];

type Editing = { id: string; preset: string; kind: Kind; name: string; base_url: string; key: string };
type Note = { tone: "note" | "warn"; text: string };

/** Where the key comes from, as a sentence. */
function keyWords(p: ModelProvider): string {
  if (p.id === PLATFORM) return "Set by the platform.";
  if (p.source === "console") return "Key saved here.";
  if (p.source === "console:gateway") return "Gateway token saved here.";
  if (p.source.startsWith("env:")) return `Key from ${p.source.slice(4)} in the daemon's environment.`;
  if (p.configured) return "Needs no key.";
  return "No key yet.";
}

/** What the model list is and where it came from, as a sentence. */
function modelWords(p: ModelProvider): string {
  const n = p.models.length;
  if (p.models_error) {
    // models_at moves on a failed read too, so the list's age says nothing about where it came from
    const fallback = n > 0 ? ` Agents can pick from ${plural(n, "model", "models")}.` : " Agents can still name a model by hand.";
    const problem = sentence(p.models_problem || "the endpoint did not list its models");
    return `${problem}${/[.?]$/.test(problem) ? "" : "."}${fallback}`;
  }
  if (!p.models_at) return `${plural(n, "built-in model", "built-in models")}.`;
  return `${plural(n, "model", "models")} listed by the endpoint, read ${ageWords(Date.now() - Date.parse(p.models_at))}.`;
}

/** `title` and `intro` for a page with no heading of its own (the platform Settings page). */
export function ProvidersPanel({ title, intro = false }: { title?: string; intro?: boolean }) {
  const [data, setData] = useState<ModelProviders>();
  const [loadErr, setLoadErr] = useState<string>();
  const [rowErr, setRowErr] = useState<{ id: string; text: string }>();
  const [note, setNote] = useState<Note>();
  const [busy, setBusy] = useState<string>();
  const [editing, setEditing] = useState<Editing>();
  const [confirmRemove, setConfirmRemove] = useState<ModelProvider>();

  const load = useCallback(() => api.providers()
    .then((d) => { setData(d); setLoadErr(undefined); })
    .catch((e) => setLoadErr(messageOf(e))), []);
  useEffect(() => { void load(); }, [load]);

  const clear = () => { setNote(undefined); setRowErr(undefined); };
  const startAdd = () => {
    const p = PRESETS[0];
    setEditing({ id: "new", preset: p.id, kind: p.kind, name: p.name, base_url: p.base_url, key: "" });
    clear();
  };
  const startEdit = (p: ModelProvider) => {
    const preset = p.kind === "openai_compatible"
      ? (PRESETS.find((x) => x.base_url && p.base_url.startsWith(x.base_url))?.id ?? "other") : p.kind;
    setEditing({ id: p.id, preset, kind: p.kind, name: p.name,
                 base_url: p.kind === "anthropic" && !p.gateway_stored && !p.base_source ? "" : p.base_url, key: "" });
    clear();
  };

  /** One row action: Refresh models, Make default, Remove. A refusal shows in that row. */
  const act = async (id: string, kind: string, call: () => Promise<ModelProviders>, after?: (d: ModelProviders) => void) => {
    setBusy(`${kind}:${id}`); clear();
    try { const d = await call(); setData(d); after?.(d); }
    catch (e) { setRowErr({ id, text: sentence(messageOf(e)) }); }
    setBusy(undefined);
  };

  const rows = data?.providers.filter((p) => p.configured || p.stored) ?? [];
  const action = !editing && data && <Button onClick={startAdd} data-testid="providers-add">Add a provider</Button>;

  // With no title (Factory Settings heads the page itself) the button sits beside a line on the
  // default, not alone in an empty header bar
  return (
    <Panel title={title} action={(title && action) || undefined} data-testid="providers">
      <div className="grid gap-4">
        {!title && (
          <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
            <p className={TEXT}>The default is what Ask uses, and what an agent without its own provider works with.</p>
            {action}
          </div>
        )}
        {intro && (
          <p className={TEXT}>
            What Tares agents and Ask work with. Add the providers you hold keys for. Each agent picks a provider and a
            model, and the default is what Ask and any agent without its own choice use. Values saved here win over the
            daemon's environment (<code className={CODE}>ANTHROPIC_API_KEY</code>, <code className={CODE}>OPENAI_API_KEY</code>,{" "}
            <code className={CODE}>TARES_MODEL_PROVIDER</code>). Keys are never shown again and never exported.
          </p>
        )}
        <Collapse>{loadErr && data && <Banner tone="error">{loadErr}</Banner>}</Collapse>
        <Collapse>{note && <Banner tone={note.tone} data-testid="providers-note">{note.text}</Banner>}</Collapse>

        {editing && data && (
          <ProviderForm editing={editing} existing={data.providers.find((p) => p.id === editing.id)}
                        onChange={setEditing} onCancel={() => setEditing(undefined)}
                        onSaved={(d, saved) => {
                          setData(d); setEditing(undefined);
                          setNote(saved?.models_error
                            ? { tone: "warn", text: `Saved, but ${lower(modelWords(saved))}` }
                            : { tone: "note", text: `Saved. ${saved ? modelWords(saved) : "In use from the next call."}` });
                        }} />
        )}

        {!data ? (
          loadErr ? <Banner tone="error">{loadErr}</Banner> : <SkeletonRows count={2} />
        ) : rows.length === 0 ? (
          !editing && (
            <EmptyState data-testid="providers-empty">
              No model provider yet. Add one: agents cannot be switched on and Ask cannot answer until then.
            </EmptyState>
          )
        ) : (
          <div className="-mt-2 [--row-x:0px]">
            {rows.map((p) => (
              <ProviderRow key={p.id} p={p} kindLabel={data.kinds.find((k) => k.id === p.kind)?.label ?? p.kind}
                           busy={busy} error={rowErr?.id === p.id ? rowErr.text : undefined}
                           onRefresh={() => void act(p.id, "refresh", () => api.refreshProviderModels(p.id), (d) => {
                             const now = d.providers.find((x) => x.id === p.id);
                             if (now && !now.models_error) setNote({ tone: "note", text: modelWords(now) });
                           })}
                           onDefault={() => void act(p.id, "default", () => api.setDefaultProvider(p.id))}
                           onEdit={() => startEdit(p)} onRemove={() => { clear(); setConfirmRemove(p); }} />
            ))}
          </div>
        )}
      </div>

      {confirmRemove && (
        <Dialog open onOpenChange={(o) => { if (!o && !busy) setConfirmRemove(undefined); }} data-testid="providers-remove-dialog"
                title={`Remove ${confirmRemove.name}?`}
                description={confirmRemove.kind === "anthropic"
                  ? "The saved key and gateway are removed. If the daemon's environment has a key, that one is used again."
                  : `Agents that use ${confirmRemove.name} move to the default until you pick another provider for them.`}
                footer={<>
                  <Button variant="secondary" disabled={busy !== undefined} onClick={() => setConfirmRemove(undefined)}>Cancel</Button>
                  <Button busy={busy === `remove:${confirmRemove.id}`} data-testid="providers-remove-confirm"
                          onClick={() => void act(confirmRemove.id, "remove", () => api.deleteProvider(confirmRemove.id))
                            .then(() => setConfirmRemove(undefined))}>Remove</Button>
                </>} />
      )}
    </Panel>
  );
}

function ProviderForm({ editing, existing, onChange, onCancel, onSaved }: {
  editing: Editing; existing?: ModelProvider; onChange: (e: Editing) => void; onCancel: () => void;
  onSaved: (d: ModelProviders, saved?: ModelProvider) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string>();
  const isNew = editing.id === "new";
  const preset = PRESETS.find((x) => x.id === editing.preset);
  const compatible = editing.kind === "openai_compatible";
  const keyOptional = compatible && preset?.key === false;
  const ok = (!compatible || Boolean(editing.base_url.trim() && editing.name.trim()))
    && (Boolean(editing.key.trim()) || Boolean(existing?.configured) || keyOptional);
  // a refusal names the field it is about; anything else shows above the buttons
  const field = !err ? undefined : /base url/i.test(err) ? "url" : /name|reserved/i.test(err) && compatible && isNew ? "name" : "form";
  const set = (patch: Partial<Editing>) => { onChange({ ...editing, ...patch }); setErr(undefined); };

  const save = async () => {
    if (!ok || busy) return;
    setBusy(true); setErr(undefined);
    try {
      const r = await api.saveProvider(editing.id, { kind: editing.kind, name: editing.name.trim(), key: editing.key.trim(), base_url: editing.base_url.trim() });
      onSaved(r, r.providers.find((p) => p.id === r.id));
    } catch (e) { setErr(sentence(messageOf(e))); setBusy(false); }
  };

  return (
    <form className={FORM} data-testid="providers-form" onSubmit={(e) => { e.preventDefault(); void save(); }}>
      {isNew ? (
        <Select label="Provider" options={PRESETS.map((p) => ({ value: p.id, label: p.label }))} value={editing.preset}
                hint={KIND_HELP[editing.kind]} data-testid="providers-preset"
                onValueChange={(id) => {
                  const p = PRESETS.find((x) => x.id === id)!;
                  set({ preset: id, kind: p.kind, name: p.name, base_url: p.base_url });
                }} />
      ) : (
        <p className={TEXT}><strong className="text-foreground" data-user-text>{existing?.name}</strong>. {KIND_HELP[editing.kind]}</p>
      )}
      <div className="grid items-start gap-4 sm:grid-cols-2">
        {compatible && (
          <Input label="Name" className="font-mono" spellCheck={false} placeholder="LiteLLM" value={editing.name} disabled={!isNew}
                 hint={isNew ? "What agents pick it by." : "The name stays; remove it and add a new one to rename."}
                 error={field === "name" ? err : undefined} onChange={(e) => set({ name: e.target.value })} />
        )}
        <Input label={keyOptional ? "API key (optional)" : "API key"} type="password" autoComplete="new-password"
               className="font-mono" spellCheck={false} value={editing.key} data-testid="providers-key"
               placeholder={existing?.configured ? "Leave empty to keep the saved key" : editing.kind === "anthropic" ? "sk-ant-…" : editing.kind === "openai" ? "sk-…" : ""}
               hint={[
                 "Once saved it is never shown again.",
                 existing?.source.startsWith("env:") ? `The key from ${existing.source.slice(4)} is in use; a key saved here replaces it.` : "",
                 existing?.gateway_token_stored ? "A gateway token is saved and sent as a bearer header." : "",
               ].filter(Boolean).join(" ")}
               onChange={(e) => set({ key: e.target.value })} />
      </div>
      <Input label={compatible ? "Base URL" : "Base URL (optional)"} className="font-mono" spellCheck={false} value={editing.base_url}
             data-testid="providers-url" error={field === "url" ? err : undefined}
             placeholder={editing.kind === "anthropic" ? "https://api.anthropic.com" : editing.kind === "openai" ? "https://api.openai.com/v1" : "http://localhost:4000/v1"}
             hint={editing.kind === "anthropic"
               ? (existing?.base_source?.startsWith("env:") && !editing.base_url
                 ? `Calls go through ${existing.base_url}, from ${existing.base_source.slice(4)}. A URL saved here replaces it.`
                 : "Only for a gateway that speaks the Anthropic format.")
               : editing.kind === "openai" ? "Only for Azure or a proxy." : "Usually ends with /v1."}
             onChange={(e) => set({ base_url: e.target.value })} />
      <Collapse>{field === "form" && <Banner tone="error">{err}</Banner>}</Collapse>
      <div className="flex flex-wrap gap-2">
        <Button type="submit" busy={busy} disabled={!ok} data-testid="providers-save">Save</Button>
        <Button variant="secondary" disabled={busy} onClick={onCancel}>Cancel</Button>
      </div>
    </form>
  );
}

function ProviderRow({ p, kindLabel, busy, error, onRefresh, onDefault, onEdit, onRemove }: {
  p: ModelProvider; kindLabel: string; busy?: string; error?: string;
  onRefresh: () => void; onDefault: () => void; onEdit: () => void; onRemove: () => void;
}) {
  const rejected = Boolean(p.models_problem?.startsWith("the endpoint rejected the key"));
  const endpoint = p.base_url || (p.kind === "anthropic" ? "api.anthropic.com" : "");
  const platform = p.id === PLATFORM;
  const any = busy !== undefined;
  return (
    <ListRow className="hover:bg-transparent" data-testid="providers-row" data-id={p.id}>
      <div className="grid min-w-0 flex-[1_1_320px] gap-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <span className="text-sm font-semibold" data-user-text>{p.name}</span>
          {p.default && <StatusLabel tone="ready" data-testid="providers-default">Default</StatusLabel>}
          {kindLabel !== p.name && <span className="text-xs text-muted-foreground">{kindLabel}</span>}
          {endpoint && <span className="font-mono text-xs text-muted-foreground" data-user-text>{endpoint}</span>}
        </div>
        <p className="m-0 flex flex-wrap items-baseline gap-x-2 text-[13px]" data-testid="providers-key-state">
          <StatusDot tone={!p.configured || rejected ? "bad" : "ready"} />
          <span>{rejected ? "The endpoint rejected the key. Edit it and save a valid one."
            : !p.configured ? "No key yet. Edit it and add one." : keyWords(p)}</span>
        </p>
        <p className="m-0 text-[13px] text-muted-foreground" data-testid="providers-models"
           title={p.models_error || p.models.slice(0, 40).join("\n") || undefined}>{modelWords(p)}</p>
        {error && <p className="m-0 text-[13px] text-err" role="alert" data-testid="providers-row-error">{error}</p>}
      </div>
      <div className="flex flex-wrap gap-2">
        {!p.default && p.configured && (
          <Button variant="secondary" busy={busy === `default:${p.id}`} disabled={any} onClick={onDefault}>Make default</Button>
        )}
        {p.discovers && p.configured && (
          <Button variant="secondary" busy={busy === `refresh:${p.id}`} disabled={any} onClick={onRefresh}>Refresh models</Button>
        )}
        {!platform && <Button variant="secondary" disabled={any} onClick={onEdit}>Edit</Button>}
        {!platform && (p.stored || p.kind !== "anthropic") && (
          <Button variant="quiet" className="text-err hover:text-err" disabled={any} onClick={onRemove}>Remove</Button>
        )}
      </div>
    </ListRow>
  );
}
