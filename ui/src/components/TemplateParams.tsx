import { Picker } from "./bits";
import type { RecipeParam, Template } from "../types";

// A template's parameters as form fields, for the builder's project card (the assistant proposed
// a whole template; the user confirms its values here). Mirrors the generic wizard's field
// rendering: strings, numbers, booleans and choices get inputs; lists and objects are JSON.
// Kept separate from ProjectNewGeneric so this card can be dropped without touching the wizard.

export type Detected = { params: Record<string, unknown>; found: Record<string, string>;
                         missing: Record<string, string>; notes: string[] };

/** A param value as the form shows it. */
export function paramText(p: RecipeParam, v: unknown): string {
  if (v === undefined || v === null || v === "") {
    if (p.default == null) return p.type === "bool" ? "false" : "";
    return typeof p.default === "string" ? p.default : JSON.stringify(p.default);
  }
  if (p.type === "bool") return v ? "true" : "false";
  if (p.type === "list" || p.type === "json") return typeof v === "string" ? v : JSON.stringify(v, null, 2);
  return String(v);
}

/** The form values back into the shape POST /api/projects takes. Throws on bad JSON. */
export function paramsBody(template: Template, vals: Record<string, string>): Record<string, unknown> {
  const params: Record<string, unknown> = {};
  for (const [k, p] of Object.entries(template.params)) {
    const v = vals[k] ?? "";
    if (v === "" && !p.required) continue;
    if (p.type === "number") params[k] = Number(v);
    else if (p.type === "bool") params[k] = v === "true";
    else if (p.type === "list" || p.type === "json") params[k] = v ? JSON.parse(v) : undefined;
    else params[k] = v;
  }
  return params;
}

export default function TemplateParams({ template, vals, setVals, needs, models, defaultModel, detected }: {
  template: Template;
  vals: Record<string, string>;
  setVals: (v: Record<string, string>) => void;
  needs?: string[];            // params the user must fill: highlighted, never prefilled
  models: string[];
  defaultModel: string;
  detected?: Detected;
}) {
  return (
    <>
      {Object.entries(template.params).map(([k, p]) => {
        const mine = needs?.includes(k);
        return (
          <label className={"field" + (mine ? " needs" : "")} key={k}>
            <span className="lbl">
              {p.label ?? k}{p.required && <span className="req"> *</span>}
              {mine && <span className="badge" style={{ marginLeft: 8 }}>yours to fill</span>}
            </span>
            {p.type === "bool" ? (
              <Picker value={vals[k] ?? "false"} onChange={(v) => setVals({ ...vals, [k]: v })}
                      options={["true", "false"]} ariaLabel={k} />
            ) : k === "model" ? (
              <Picker value={vals[k] ?? ""} onChange={(v) => setVals({ ...vals, [k]: v })}
                      options={["", ...models.filter((m) => m !== defaultModel)]}
                      labels={{ "": defaultModel ? `${defaultModel} · instance default` : "instance default" }}
                      ariaLabel="model" />
            ) : p.choices?.length ? (
              <Picker value={vals[k] ?? ""} onChange={(v) => setVals({ ...vals, [k]: v })}
                      options={p.choices} ariaLabel={k} />
            ) : p.type === "list" || p.type === "json" ? (
              <textarea className="mono" rows={4} value={vals[k] ?? ""}
                        onChange={(e) => setVals({ ...vals, [k]: e.target.value })} placeholder="JSON" />
            ) : (
              <input type={p.secret ? "password" : p.type === "number" ? "number" : "text"} className="mono"
                     autoComplete={p.secret ? "new-password" : undefined} value={vals[k] ?? ""}
                     onChange={(e) => setVals({ ...vals, [k]: e.target.value })} />
            )}
            {p.help && <span className="help">{p.help}</span>}
            {detected?.found[k] && <span className="help" style={{ color: "var(--ok)" }}>detected: {detected.found[k]}</span>}
            {detected?.missing[k] && <span className="help" style={{ color: "var(--warn, var(--err))" }}>not detected: {detected.missing[k]}</span>}
          </label>
        );
      })}
    </>
  );
}
