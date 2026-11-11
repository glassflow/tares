import * as React from "react";

// Colour for a tool card's input and output (TR-1177, review of PR 602). Every token is a React <span>
// holding text, never HTML, so markup in a transcript still shows as text. The blocks are dark in both
// themes, so the colours are the dark-scale tokens (as on the login card): green and red for a diff,
// the accent tones for JSON.

export const KEY = "text-[color:var(--kit-accent)]";
export const STR = "text-[color:var(--green-300)]";
export const NUM = "text-[color:var(--kit-accent-hover)]";
export const LIT = "text-[color:var(--sand-300)]";
export const ADD = "text-[color:var(--green-300)]";
export const DEL = "text-[color:var(--red-300)]";
export const HUNK = "text-[color:var(--on-brown-500)]";

const JSON_TOKEN = /("(?:\\.|[^"\\\n])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;

/** JSON text (pretty-printed by the cell, maybe cut short) with keys, strings, numbers and literals coloured. */
export function JsonText({ text }: { text: string }) {
  const out: React.ReactNode[] = [];
  let last = 0;
  for (const m of text.matchAll(JSON_TOKEN)) {
    const i = m.index ?? 0;
    if (i > last) out.push(text.slice(last, i));
    if (m[1] !== undefined) {
      out.push(<span key={i} className={m[2] ? KEY : STR}>{m[1]}</span>);
      if (m[2]) out.push(m[2]);
    } else if (m[3] !== undefined) {
      out.push(<span key={i} className={LIT}>{m[3]}</span>);
    } else {
      out.push(<span key={i} className={NUM}>{m[0]}</span>);
    }
    last = i + m[0].length;
  }
  if (last < text.length) out.push(text.slice(last));
  return <>{out}</>;
}

/** True when most lines read as a diff: they start with +, -, @@ or a space, and some add or take away. */
export function looksLikeDiff(text: string): boolean {
  const lines = text.split("\n").filter((l) => l.length > 0);
  if (lines.length < 2) return false;
  const marked = lines.filter((l) => /^[+\- ]|^@@/.test(l)).length;
  const changes = lines.filter((l) => /^[+-]/.test(l) && !/^(\+\+\+|---)( |$)/.test(l)).length;
  return changes > 0 && marked / lines.length >= 0.8;
}

/** Diff text, a line at a time: added green, removed red, hunk headers muted. */
export function DiffText({ text }: { text: string }) {
  const lines = text.split("\n");
  return (
    <>
      {lines.map((l, i) => {
        const cls = /^@@/.test(l) ? HUNK : /^\+/.test(l) ? ADD : /^-/.test(l) ? DEL : undefined;
        return <React.Fragment key={i}>{cls ? <span className={cls}>{l}</span> : l}{i < lines.length - 1 ? "\n" : ""}</React.Fragment>;
      })}
    </>
  );
}

const lined = (prefix: string, s: string) => s.split("\n").map((l) => `${prefix}${l}`).join("\n");

/** An Edit or Write call's input as a diff: what it took out (red) and put in (green). Null when the input
 *  is not one (another tool, or JSON the list cut short). */
export function changeOf(name: string, input: string): { file: string; diff: string } | null {
  if (!["Edit", "MultiEdit", "Write"].includes(name)) return null;
  let o: unknown;
  try { o = JSON.parse(input); } catch { return null; }
  if (!o || typeof o !== "object") return null;
  const r = o as Record<string, unknown>;
  const file = typeof r.file_path === "string" ? r.file_path : "";
  const pair = (a: unknown, b: unknown) => (typeof a === "string" && typeof b === "string" ? `${lined("-", a)}\n${lined("+", b)}` : null);
  if (name === "Write") return typeof r.content === "string" ? { file, diff: lined("+", r.content) } : null;
  if (name === "Edit") { const d = pair(r.old_string, r.new_string); return d === null ? null : { file, diff: d }; }
  const edits = Array.isArray(r.edits) ? r.edits : [];
  const parts = edits.map((e) => (e && typeof e === "object" ? pair((e as Record<string, unknown>).old_string, (e as Record<string, unknown>).new_string) : null));
  return parts.length && parts.every((p) => p !== null) ? { file, diff: (parts as string[]).join("\n@@\n") } : null;
}
