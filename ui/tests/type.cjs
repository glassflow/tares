// The type check (TR-1270, .impeccable.md Type): Geist Mono is for operational labels, table heads,
// state words, ids and code; Doto for headline numerals. Buttons, headings and body text are Inter.
// It reads the computed font, so a mono button counts however it got its face (a class, a legacy
// selector, an inherited family).
//
// What it looks at: every button (and role=button), every heading (h1-h6, role=heading), and every
// paragraph that reads as a sentence (five words or more ending in . ? or !). An id or a stamp on its
// own line stays mono and is not a sentence. Code, pre and kbd are left out, and so is anything a caller
// names in `skip` (a selector).

async function checkType(page, ck, label, { skip = "", root = "body" } = {}) {
  const bad = await page.evaluate(({ skip, root }) => {
    const face = (e) => {
      const f = getComputedStyle(e).fontFamily.toLowerCase();
      return f.startsWith('"geist mono"') || f.startsWith("geist mono") ? "mono" : f.includes("doto") ? "pixel" : "";
    };
    const out = (e) => e.closest("code, pre, kbd") || (skip && e.closest(skip));
    const seen = (e) => e.getClientRects().length > 0;
    const say = (e, kind, f) => `${kind} in ${f}: "${(e.innerText || e.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim().slice(0, 40)}"`;
    const found = [];
    for (const e of document.querySelectorAll(`${root} button, ${root} [role=button]`)) {
      const f = face(e);
      if (f && !out(e) && seen(e)) found.push(say(e, "button", f));
    }
    for (const e of document.querySelectorAll(`${root} :is(h1, h2, h3, h4, h5, h6, [role=heading])`)) {
      const f = face(e);
      if (f && !out(e) && seen(e)) found.push(say(e, "heading", f));
    }
    for (const e of document.querySelectorAll(`${root} p`)) {
      const t = e.innerText.replace(/\s+/g, " ").trim();
      const f = face(e);
      if (f && !out(e) && seen(e) && t.split(" ").length >= 5 && /[.?!]$/.test(t)) found.push(say(e, "sentence", f));
    }
    return found;
  }, { skip, root });
  ck(`${label}: no button, heading or sentence in mono or Doto (TR-1270)`, bad.length === 0, bad.slice(0, 4).join(" | "));
  return bad;
}

module.exports = { checkType };
