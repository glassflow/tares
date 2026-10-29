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
  let t = text.replace(/\s+/g, " ");
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

/** A length of time in words: "17 seconds", "8 minutes", "6 days", "1 day, 18 hours".
 *  One unit, floored; when that unit's count is 1 the next unit follows if it is not zero. */
export function durationWords(seconds: number): string {
  const s = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0;
  const units: [string, number][] = [["day", 86400], ["hour", 3600], ["minute", 60], ["second", 1]];
  const noun = (n: number, u: string) => `${n} ${u}${n === 1 ? "" : "s"}`;
  for (let i = 0; i < units.length; i++) {
    const [name, size] = units[i];
    if (s < size && i < units.length - 1) continue;
    const n = Math.floor(s / size);
    const rest = Math.floor((s - n * size) / (units[i + 1]?.[1] ?? 1));
    if (n === 1 && i < units.length - 1 && rest > 0) return `${noun(n, name)}, ${noun(rest, units[i + 1][0])}`;
    return noun(n, name);
  }
  return noun(0, "second");
}
