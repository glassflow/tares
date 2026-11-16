import words from "./words.json";

// The words list for the factory and sre editions (see WORDS.md). The same rule is in
// ui/tests/words.cjs (CI and browser scripts) and tares/words.py (API tests); keep the three alike.

type Entry = { word: string; plural: string; allow: string[] };

const EM_DASH = words.punctuation[0];
const WORD_CHAR = "A-Za-z0-9_";
const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

const rules = (words.forbidden as Entry[]).map((e) => ({
  names: [e.word, e.plural],
  allow: e.allow.map(
    (a) => new RegExp(`(?<![${WORD_CHAR}])${escape(a.replace(/\s+/g, " ").trim())}(?![${WORD_CHAR}])`, "gi"),
  ),
  find: new RegExp(`(?<![${WORD_CHAR}])(?:${escape(e.plural)}|${escape(e.word)})(?![${WORD_CHAR}])`, "gi"),
}));

/** The forbidden words in a text (whole words, any case, minus the allowed phrases), plus "em dash". */
export function forbiddenIn(text: string): string[] {
  const t = text.replace(/\s+/g, " ");
  const found: { at: number; name: string }[] = [];
  if (t.includes(EM_DASH)) found.push({ at: t.indexOf(EM_DASH), name: "em dash" });
  for (const r of rules) {
    let clean = t;
    for (const a of r.allow) clean = clean.replace(a, (m) => " ".repeat(m.length));
    for (const m of clean.matchAll(r.find)) found.push({ at: m.index ?? 0, name: m[0].toLowerCase() });
  }
  found.sort((a, b) => a.at - b.at);
  return [...new Set(found.map((f) => f.name))];
}
