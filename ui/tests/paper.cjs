// The paper check (TR-1274): a page drawn on the paper opens each section with a 1px rule
// over its heading, and nothing taller than a chip is a box (a border on all four sides).
// It reads computed styles, so a box counts however it got its border.
//
// Left out on purpose: controls, code and dialogs; a Banner (role alert or status), which is a
// callout; and anything a caller names in `skip` (a selector), for a piece another page shares
// and that moves with that page's PR.

async function checkPaper(page, ck, label, { skip = "", root = "main" } = {}) {
  const r = await page.evaluate(({ skip, root }) => {
    const out = (e) => skip && e.closest(skip);
    const heads = [...document.querySelectorAll(`${root} h2`)].filter((h) => !out(h)).map((h) => {
      const cs = getComputedStyle(h.parentElement);
      return { t: h.textContent.trim(), ok: cs.borderTopWidth === "1px" && cs.borderTopStyle === "solid" };
    });
    const exempt = "button, a, input, textarea, pre, code, kbd, [role=dialog], [role=progressbar], [role=alert], [role=status]";
    const boxes = [...document.querySelectorAll(`${root} *`)].filter((e) => {
      if (e.closest(exempt) || out(e)) return false;
      const cs = getComputedStyle(e);
      const all = ["Top", "Right", "Bottom", "Left"].every((k) => parseFloat(cs[`border${k}Width`]) > 0 && cs[`border${k}Style`] !== "none");
      return all && e.getBoundingClientRect().height > 60;
    }).map((e) => `${e.tagName.toLowerCase()}.${String(e.className).slice(0, 50)}`);
    return { heads, boxes };
  }, { skip, root });
  ck(`${label}: every section heading opens with a 1px rule (TR-1274)`,
     r.heads.every((h) => h.ok), JSON.stringify(r.heads.filter((h) => !h.ok)));
  ck(`${label}: no section is a box (TR-1274)`, r.boxes.length === 0, r.boxes.slice(0, 3).join(" | "));
  return r;
}

module.exports = { checkPaper };
