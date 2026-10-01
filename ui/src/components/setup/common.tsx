import { useEffect, useRef, useState } from "react";

import { ApiError } from "../../api";

// Pieces the goal-first setup steps share: the stepper, copy buttons, switches, number steppers
// and the SKILL.md reader for skills added before the project exists.

export type FlowStep = "goal" | "plan" | "connect" | "try";
const ORDER: FlowStep[] = ["goal", "plan", "connect", "try"];
const LABELS: Record<FlowStep, string> = { goal: "Goal", plan: "Plan", connect: "Connect", try: "Try it" };

export const errText = (e: unknown) => String((e as Error)?.message ?? e);
export const errStatus = (e: unknown) => (e instanceof ApiError ? e.status : undefined);

/** Goal, Plan, Connect, Try it: where the person is and what is done. */
export function Stepper({ at }: { at: FlowStep }) {
  const i = ORDER.indexOf(at);
  return (
    <ol className="su-stepper" aria-label="Setup steps">
      {ORDER.map((k, n) => {
        const state = n < i ? "done" : n === i ? "current" : "todo";
        return (
          <li key={k} className={`su-step ${state}`} aria-current={state === "current" ? "step" : undefined}>
            <span className="su-step-mark" aria-hidden="true">{state === "done" ? "✓" : n + 1}</span>
            {LABELS[k]}
            {state === "done" && <span className="sr-only"> (done)</span>}
          </li>
        );
      })}
    </ol>
  );
}

/** Copies `text`; says so for a moment, out loud too. */
export function CopyButton({ text, what, className }: { text: string; what: string; className?: string }) {
  const [done, setDone] = useState(false);
  const timer = useRef<number>();
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return (
    <button type="button" className={className} aria-label={done ? `Copied ${what}` : `Copy ${what}`}
            onClick={() => {
              navigator.clipboard?.writeText(text);
              setDone(true);
              window.clearTimeout(timer.current);
              timer.current = window.setTimeout(() => setDone(false), 1500);
            }}>
      {done ? "Copied" : "Copy"}
    </button>
  );
}

/** A value to copy: the text in a code box, the button beside it. */
export function CopyLine({ text, what, multiline }: { text: string; what: string; multiline?: boolean }) {
  return (
    <div className={`su-copy${multiline ? " multi" : ""}`}>
      <code>{text}</code>
      <CopyButton text={text} what={what} />
    </div>
  );
}

/** An on/off switch: a real checkbox with the switch role, labelled by its children. */
export function Switch({ checked, onChange, children, disabled }: {
  checked: boolean; onChange: (on: boolean) => void; children: React.ReactNode; disabled?: boolean;
}) {
  return (
    <label className="su-switch">
      <input type="checkbox" role="switch" checked={checked} disabled={disabled}
             onChange={(e) => onChange(e.target.checked)} />
      <span className="su-switch-track" aria-hidden="true" />
      <span className="su-switch-text">{children}</span>
    </label>
  );
}

/** A number with - and + beside it; typing works too. */
export function NumberStepper({ value, min, max, label, onChange }: {
  value: number; min: number; max: number; label: string; onChange: (v: number) => void;
}) {
  const clamp = (v: number) => Math.min(max, Math.max(min, Math.round(v)));
  const [text, setText] = useState(String(value));
  useEffect(() => { setText(String(value)); }, [value]);
  return (
    <span className="su-num" role="group" aria-label={label}>
      <button type="button" aria-label={`Lower ${label}`} disabled={value <= min}
              onClick={() => onChange(clamp(value - 1))}>-</button>
      <input type="text" inputMode="numeric" aria-label={label} value={text}
             onChange={(e) => {
               setText(e.target.value);
               const n = Number(e.target.value);
               if (e.target.value.trim() !== "" && Number.isFinite(n)) onChange(clamp(n));
             }}
             onBlur={() => setText(String(value))} />
      <button type="button" aria-label={`Raise ${label}`} disabled={value >= max}
              onClick={() => onChange(clamp(value + 1))}>+</button>
    </span>
  );
}

/** A SKILL.md as the backend's upload reads it: front matter with name and description, then the
 *  body. Setup reads it in the browser, because the project it would upload to does not exist yet. */
export function parseSkillMd(text: string): { name: string; description: string; body: string } {
  const src = text.replace(/^﻿/, "").replace(/\r\n/g, "\n");
  const m = /^---\n([\s\S]*?)\n---\n?([\s\S]*)$/.exec(src);
  if (!m) throw new Error("This file has no front matter. A SKILL.md starts with --- then name: and description: lines, then ---.");
  const fields: Record<string, string> = {};
  const lines = m[1].split("\n");
  for (let i = 0; i < lines.length; i++) {
    const kv = /^([A-Za-z_][\w-]*):\s*(.*)$/.exec(lines[i]);
    if (!kv) continue;
    let v = kv[2].trim();
    if (v === ">" || v === "|" || v === ">-" || v === "|-") {
      const cont: string[] = [];
      while (i + 1 < lines.length && /^\s+/.test(lines[i + 1])) cont.push(lines[++i].trim());
      v = cont.join(v.startsWith(">") ? " " : "\n");
    } else if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
      v = v.slice(1, -1);
    }
    fields[kv[1]] = v;
  }
  const name = (fields.name ?? "").trim();
  const description = (fields.description ?? "").trim();
  if (!name) throw new Error("The front matter has no name: line.");
  if (!description) throw new Error("The front matter has no description: line.");
  return { name, description, body: m[2].replace(/^\n+/, "") };
}
