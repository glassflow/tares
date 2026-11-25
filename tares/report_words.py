"""Short report texts written from figures (TR-1645): the summary under an accepted milestone, and
later the weekly paragraph (TR-1675).

A caller computes a dict of figures (nested dicts and lists). `figures_text` asks the cell's default
model to write from those figures only, and shows what it wrote only when `numbers_ok` finds every
number of it in the figures. With no model, or a text that fails the check, fixed template sentences
say the same figures.

A new kind of report adds one line to PROMPTS and one function to TEMPLATES.

What the number check reads (the same reading for the text and for the strings of the figures):
- money ($12.50, 12 dollars, 12 USD), percents (46%, 46 percent), durations (1.3 h, 90 s,
  1 hour 4 minutes, 3-day), names that mix letters and digits (T12, TR-1675, M2, #42, v1.2.3, a
  date, a time of day), plain numbers (1,234 or 3rd) and numbers written as words (twelve).
- A name with digits must stand in one of the figures' strings as written.
- Any other number passes in one of two ways. Quoted: a string of the figures has the same number
  beside the same word, so "6 digits" out of a ticket title passes and a count invented from it does
  not. Counted: a numeric figure of the same kind equals it after the page's rounding.
- The kind of a numeric figure comes from its key: `_usd` is money; `pct` or `percent` is a percent
  (0 to 100); `share` or `ratio` is a fraction shown as a percent; `_ms`, `_s`, `_secs`, `_seconds`,
  `_min`, `_minutes`, `_h`, `_hours` and `_days` are durations; anything else is a plain number.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Callable, NamedTuple

from . import providers as providers_mod
from .models import add_usage, empty_usage
from .providers import resolve_provider

log = logging.getLogger("tares.report_words")

# where a text came from
MODEL, TEMPLATE, NO_KEY, REJECTED = "model", "template", "template:no-key", "template:rejected"

TIMEOUT_S = 60
MAX_TOKENS = 600
MAX_LINES = 8       # the prompt asks for five; more than this is not a short summary
MAX_CHARS = 1200

PROMPTS: dict[str, str] = {
    "milestone": (
        "You write a short summary of one accepted milestone for a manager. Use only the facts in "
        "the JSON. Never invent work, numbers, names or reasons. Write at most five short lines of "
        "plain text, no headings, no bullets, no markdown, no emoji, no em dashes: what was "
        "delivered, the decisions taken on the way, and what is left. Write every number in digits "
        "exactly as the facts give it; do not add up, count or convert numbers yourself. Do not "
        "use words of praise or judgement (such as great, smooth, significant, successfully, "
        "quickly) that the facts do not show."),
}


# ── the template sentences ───────────────────────────────────────────────────
TEMPLATE_TICKETS = 3      # ticket titles named after the delivered count
TEMPLATE_DECISIONS = 2    # decisions spelled out after their count
TEMPLATE_MILESTONES = 3   # later milestones named after the open count


def _plain_text(text: Any) -> str:
    """A person's text inside a template sentence: one line, no em dash."""
    return " ".join(str(text or "").replace("\u2014", ", ").split()).replace(" , ", ", ")


def _bare(text: Any) -> str:
    return _plain_text(text).rstrip(" .;:,")


def _sentence(text: Any, mark: str = ".") -> str:
    t = _plain_text(text).rstrip(" ;:,")
    return t if t.endswith((".", "?", "!")) else t + mark


def _count(n: int, one: str) -> str:
    return f"{n:,} {one}" + ("" if n == 1 else "s")


def _decision(d: dict) -> str:
    """One decision as sentences: the question, then what was decided."""
    question, answer = _plain_text(d["question"]), _plain_text(d["answer"])
    said = [_sentence(question, ":" if answer else ".")] if question else []
    return " ".join(said + ([_sentence(answer)] if answer else []))


def _milestone_template(f: dict) -> str:
    """Three lines: delivered, decided on the way, left."""
    delivered, decisions, left = f["delivered"], f["decisions"], f["left"]

    n = delivered["count"]
    if not n:
        first = "Delivered: no tickets."
    else:
        titles = [_bare(t["title"]) for t in delivered["tickets"][:TEMPLATE_TICKETS]]
        more = "Among them: " if n > len(titles) else ""
        first = f"Delivered: {_count(n, 'ticket')}. {more}{_sentence('; '.join(titles))}"

    n = decisions["count"]
    if not n:
        second = "No decisions were recorded on these tickets."
    else:
        shown = decisions["items"][:TEMPLATE_DECISIONS]
        said = " ".join(_decision(d) for d in shown)
        more = "Among them: " if n > len(shown) else ""
        second = f"Decided on the way: {n:,}. {more}{said}"

    n = left["count"]
    if not n:
        third = "Nothing is left in later milestones."
    else:
        shown = left["later_milestones"][:TEMPLATE_MILESTONES]
        named = "; ".join(f"{_bare(m['name'])}, {m['open_count']:,}" for m in shown)
        later, lead = "later milestones", ""
        if left["milestone_count"] > len(shown):
            later, lead = f"{left['milestone_count']:,} later milestones", "The next ones: "
        third = f"Left: {_count(n, 'open ticket')} in {later}. {lead}{named}."

    return "\n".join([first, second, third])


TEMPLATES: dict[str, Callable[[dict], str]] = {"milestone": _milestone_template}


# ── reading numbers ──────────────────────────────────────────────────────────
class Mention(NamedTuple):
    kind: str          # money, percent, duration, name or number
    value: Any         # Decimal (seconds for a duration); a name's lower-cased text
    decimals: int
    written: str
    parts: tuple       # a duration's ((amount, seconds per unit, decimals), ...)
    beside: tuple      # (the word before, the word after), "" where there is none worth comparing


_NUM = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_END = r"(?![A-Za-z0-9_])"
_UNIT = (r"(?:(?i:milliseconds?|seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?)"
         r"|ms|s|m|h|d)")
_PART = rf"{_NUM}[ \t\-]?{_UNIT}{_END}"
_CHUNK = r"[A-Za-z0-9_]+(?:[.\-_'’][A-Za-z0-9_]+)*"
_TOKEN = re.compile(
    rf"(?:US)?\$\s?(?P<money>{_NUM}){_END}"
    rf"|(?<![A-Za-z0-9_.])(?P<money2>{_NUM})\s?(?:USD|(?i:dollars?)){_END}"
    rf"|(?<![A-Za-z0-9_.])(?P<percent>{_NUM})\s?(?:%|(?:(?i:percent|per cent|pct)){_END})"
    rf"|(?<![A-Za-z0-9_.])(?P<duration>{_PART}(?:(?:\s*,\s*|\s+and\s+|\s+){_PART})*)"
    rf"|(?<![A-Za-z0-9_])(?P<stamp>\d{{4}}-\d{{2}}-\d{{2}}(?:[T ]\d{{2}}:\d{{2}}(?::\d{{2}})?Z?)?"
    rf"|\d{{1,2}}:\d{{2}}(?::\d{{2}})?|\d+(?:\.\d+){{2,}}){_END}"
    rf"|(?P<hash>#\d+){_END}"
    rf"|(?<![A-Za-z0-9_.])(?P<grouped>\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?){_END}"
    rf"|(?P<chunk>{_CHUNK})")
_PART_RE = re.compile(rf"(?P<n>{_NUM})[ \t\-]?(?P<u>{_UNIT}){_END}")
_ORDINAL = re.compile(r"(\d+)(?:st|nd|rd|th)", re.I)
_NUMBER_WORD = re.compile(r"(\d+(?:\.\d+)?)-([A-Za-z]+(?:-[A-Za-z]+)*)")

# words too common to tell where a number came from: the next one out is compared instead
_STOP = {"a", "an", "the", "of", "to", "in", "at", "and", "is", "are", "was", "were", "for", "on",
         "with", "by", "or"}
_MONTHS = {"january": "jan", "february": "feb", "march": "mar", "april": "apr", "june": "jun",
           "july": "jul", "august": "aug", "september": "sep", "sept": "sep", "october": "oct",
           "november": "nov", "december": "dec"}

_SMALL = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * i for i, w in enumerate(
    "twenty thirty forty fifty sixty seventy eighty ninety".split(), 2)}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 10 ** 6, "billion": 10 ** 9}
_NUMBER_WORDS = "|".join(sorted([*_SMALL, *_TENS, *_SCALES], key=len, reverse=True))
_SPELLED = re.compile(
    rf"(?<![A-Za-z0-9_])(?:{_NUMBER_WORDS}){_END}"
    rf"(?:(?:(?<=hundred) and |(?<=thousand) and |[ \-])(?:{_NUMBER_WORDS}){_END})*", re.I)
# "one" that points at a thing and counts nothing
_ONE_AFTER = {"no", "the", "this", "that", "which", "each", "every", "any", "some"}
_ONE_BEFORE = {"another", "of"}
_VAGUE = re.compile(r"(?<![A-Za-z])(dozens?|couple|half|halved|twice|thrice|doubled|tripled)(?![A-Za-z])",
                    re.I)

# seconds per unit, for what is written and for a figure's key
_SECONDS = {"ms": Decimal("0.001"), "s": Decimal(1), "m": Decimal(60), "h": Decimal(3600),
            "d": Decimal(86400), "w": Decimal(604800)}
_KEY_SECONDS = {"_ms": "ms", "_s": "s", "_secs": "s", "_seconds": "s", "_min": "m",
                "_minutes": "m", "_h": "h", "_hours": "h", "_days": "d"}
# the fewest decimals a figure that is not whole may be rounded to
MIN_DECIMALS = {"number": 1, "money": 2, "percent": 0, "duration": 1}


def _digits(text: str) -> str:
    """The same text with numbers written as words turned into digits."""
    def turn(m: re.Match) -> str:
        words = re.findall(r"[a-z]+", m.group(0).lower())
        words = [w for w in words if w != "and"]
        if words == ["one"]:
            before = re.findall(r"[A-Za-z]+\s*$", m.string[:m.start()])
            after = re.match(r"(\s+|-)([A-Za-z]+)", m.string[m.end():])
            if before and before[0].strip().lower() in _ONE_AFTER:
                return m.group(0)
            if after and (after.group(1) == "-" or after.group(2).lower() in _ONE_BEFORE):
                return m.group(0)
        out: list[int] = []
        total = current = 0
        last = ""
        for w in words:
            if w in _SCALES:
                scale = _SCALES[w]
                if scale == 100:
                    current = (current or 1) * scale
                else:
                    total += (current or 1) * scale
                    current = 0
                last = "scale"
                continue
            kind = "tens" if w in _TENS else "small"
            value = _TENS.get(w, _SMALL.get(w, 0))
            if last == "scale" or (last == "tens" and kind == "small" and 0 < value < 10):
                current += value
            else:
                if last:
                    out.append(total + current)
                    total = 0
                current = value
            last = kind
        out.append(total + current)
        return " ".join(str(n) for n in out)

    return _SPELLED.sub(turn, text)


def _word(chunk: str) -> str:
    """A neighbouring word as compared: lower case, no plural s, months cut to three letters."""
    w = chunk.lower()
    w = _MONTHS.get(w, w)
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        w = w[:-1]
    return "" if w in _STOP else w


def _decimal(written: str) -> tuple[Decimal, int]:
    s = written.replace(",", "")
    return Decimal(s), len(s.split(".")[1]) if "." in s else 0


def _unit(written: str) -> str:
    u = written.lower()
    if u == "ms" or u.startswith("millisecond"):
        return "ms"
    return u[0]   # s(econd), m(inute), h(our), d(ay), w(eek)


def _durations(written: str) -> list[tuple[list, str]]:
    """The durations in one matched run: parts join while each unit is smaller than the one before."""
    out: list[tuple[list, str]] = []
    start = end = 0
    parts: list = []
    for m in _PART_RE.finditer(written):
        value, places = _decimal(m.group("n"))
        per = _SECONDS[_unit(m.group("u"))]
        if parts and per >= parts[-1][1]:
            out.append((parts, written[start:end]))
            parts = []
        if not parts:
            start = m.start()
        parts.append((value, per, places))
        end = m.end()
    if parts:
        out.append((parts, written[start:end]))
    return out


def mentions(text: str) -> list[Mention]:
    """Every number and name with digits in a text, in order."""
    text = _digits(str(text))
    found: list[tuple[str, Any, int, str, tuple, int, int]] = []
    words: list[tuple[int, int, str]] = []

    def signed(at: int, value: Decimal) -> Decimal:
        if at and text[at - 1] in "-−" and (at == 1 or text[at - 2] in " \t\n("):
            return -value
        return value

    def add(kind, value, places, written, start, end, parts=()):
        found.append((kind, value, places, written, parts, start, end))

    for m in _TOKEN.finditer(text):
        kind, got, start, end = m.lastgroup, m.group(m.lastgroup), m.start(), m.end()
        if kind in ("money", "money2", "percent", "grouped"):
            value, places = _decimal(got)
            add({"money2": "money", "grouped": "number"}.get(kind, kind), signed(start, value),
                places, m.group(0), start, end)
        elif kind == "duration":
            for parts, written in _durations(got):
                total = sum(v * per for v, per, _ in parts)
                add("duration", signed(start, total), parts[-1][2], written, start, end, tuple(parts))
        elif kind in ("stamp", "hash"):
            add("name", got.lower(), 0, got, start, end)
        else:
            got = re.sub(r"['\u2019]s$", "", got)
            if not any(c.isdigit() for c in got):
                words.append((start, end, got))
                continue
            ordinal = _ORDINAL.fullmatch(got)
            counted = _NUMBER_WORD.fullmatch(got)
            if ordinal:
                add("number", Decimal(ordinal.group(1)), 0, got, start, end)
            elif counted:
                value, places = _decimal(counted.group(1))
                add("number", signed(start, value), places, counted.group(1), start,
                    start + len(counted.group(1)))
                words.append((start + len(counted.group(1)) + 1, end, counted.group(2)))
            elif any(c.isalpha() or c == "_" for c in got):
                add("name", got.lower(), 0, got, start, end)
            else:   # digits joined by . or - : each is a number of its own
                for n in re.finditer(r"\d+(?:\.\d+)?", got):
                    value, places = _decimal(n.group(0))
                    add("number", signed(start + n.start(), value), places, n.group(0),
                        start + n.start(), start + n.end())

    out = []
    for i, (kind, value, places, written, parts, start, end) in enumerate(found):
        # the words between this number and its neighbours: one number's words are not another's
        low = found[i - 1][6] if i else 0
        high = found[i + 1][5] if i + 1 < len(found) else len(text)
        before = next((_word(w) for s, e, w in reversed(words)
                       if low <= s and e <= start and _word(w)), "")
        after = next((_word(w) for s, e, w in words if s >= end and e <= high and _word(w)), "")
        out.append(Mention(kind, value, places, written, parts, (before, after)))
    return out


def _figure_kind(key: str, value: Decimal) -> tuple[str, Decimal]:
    k = key.lower()
    if k == "usd" or k.endswith("_usd"):
        return "money", value
    if "pct" in k or "percent" in k:
        return "percent", value
    if k in ("share", "ratio") or k.endswith(("_share", "_ratio")):
        return "percent", value * 100
    for suffix, unit in _KEY_SECONDS.items():
        if k.endswith(suffix):
            return "duration", value * _SECONDS[unit]
    return "number", value


def _read_figures(figures: Any) -> tuple[list[tuple[str, Decimal]], list[str]]:
    """(every numeric figure with its kind, every string) of a nested dict or list. A key with a
    digit in it is a string too."""
    numbers: list[tuple[str, Decimal]] = []
    strings: list[str] = []

    def walk(v: Any, key: str) -> None:
        if isinstance(v, dict):
            for k, x in v.items():
                if any(c.isdigit() for c in str(k)):
                    strings.append(str(k))
                walk(x, str(k))
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x, key)
        elif isinstance(v, str):
            strings.append(v)
        elif isinstance(v, (int, float, Decimal)) and not isinstance(v, bool) and math.isfinite(v):
            numbers.append(_figure_kind(key, Decimal(str(v))))

    walk(figures, "")
    return numbers, strings


def _rounds_to(figure: Decimal, written: Decimal, places: int, kind: str) -> bool:
    if figure == written:
        return True
    if figure == figure.to_integral_value() or places < MIN_DECIMALS[kind]:
        return False
    return figure.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP) == written


def _counted(m: Mention, kind: str, figure: Decimal) -> bool:
    if m.kind != kind:
        return False
    if kind != "duration":
        return _rounds_to(figure, m.value, m.decimals, kind)
    if m.value == figure:
        return True
    sign = -1 if m.value < 0 else 1
    seconds, written = abs(figure), abs(m.value)
    if sign * figure < 0:
        return False
    whole = all(places == 0 for _, _, places in m.parts)
    if len(m.parts) == 1:
        amount, per, places = m.parts[0]
        if _rounds_to(seconds / per, amount, places, kind):
            return True
        # cut down to the largest unit that fits, as the page words an age ("2 days")
        largest = next((u for u in (_SECONDS["d"], _SECONDS["h"], _SECONDS["m"]) if seconds >= u),
                       _SECONDS["s"])
        return whole and per == largest and (seconds / per).to_integral_value(ROUND_DOWN) == amount
    # several parts, cut down to the smallest one ("1 hour 4 minutes")
    last = m.parts[-1][1]
    return whole and (seconds / last).to_integral_value(ROUND_DOWN) * last == written


def unbacked(text: str, figures: Any) -> list[str]:
    """The numbers of a text that the figures do not back, as written. Empty means all are."""
    numbers, strings = _read_figures(figures)
    quoted = [m for s in strings for m in mentions(s)]
    names = {m.value for m in quoted if m.kind == "name"}
    said = " \n ".join(strings).lower()
    bad: list[str] = []
    for m in mentions(text):
        if m.kind == "name":
            ok = m.value in names
        else:
            ok = any(q.kind == m.kind and q.value == m.value
                     and any(a and a == b for a, b in zip(m.beside, q.beside)) for q in quoted)
            ok = ok or any(_counted(m, kind, figure) for kind, figure in numbers)
        if not ok:
            bad.append(m.written)
    # "a dozen", "half": amounts with no digits to check
    bad += [w.group(0) for w in _VAGUE.finditer(str(text))
            if not re.search(rf"(?<![a-z]){re.escape(w.group(0).lower())}(?![a-z])", said)]
    return bad


def numbers_ok(text: str, figures: Any) -> bool:
    """Every number in the text is one of the figures' numbers, rounded as the page rounds."""
    return not unbacked(text, figures)


# ── writing ──────────────────────────────────────────────────────────────────
def _pick_model(store) -> tuple[Any, str, str | None, str]:
    provider, origin = resolve_provider(store)
    if provider is None:
        return None, "", None, ""
    pid = providers_mod.default_id(store)
    return provider, origin, pid, providers_mod.default_model_for(store, pid)


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]*\u2014[ \t]*", ", ", str(text or ""))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


async def figures_text(prompt_kind: str, figures: dict, *, store, record_usage=None,
                       timeout: float = TIMEOUT_S) -> tuple[str, str]:
    """(text, source) for one kind of report. `source` is `model`, `template`, `template:no-key` or
    `template:rejected`. Never raises for the model's sake: whatever goes wrong with it, the template
    sentences are returned. `record_usage(model, usage, key_source, kind, provider_id)` is called
    for a model call that answered."""
    system, template = PROMPTS[prompt_kind], TEMPLATES[prompt_kind]
    try:
        provider, origin, pid, model = await asyncio.to_thread(_pick_model, store)
        if provider is None:
            return template(figures), NO_KEY
        user = "Facts:\n" + json.dumps(figures, ensure_ascii=False, indent=1)
        reply = await asyncio.wait_for(
            provider.complete(model=model, system=system, tools=[],
                              messages=[{"role": "user", "content": user}],
                              max_tokens=MAX_TOKENS, tools_allowed=False),
            timeout=timeout)
    except Exception as e:  # noqa: BLE001 -- the template says the same figures
        log.warning("%s summary: no model answer (%s: %s), using the template", prompt_kind,
                    type(e).__name__, e)
        return template(figures), TEMPLATE

    if record_usage is not None:
        try:
            usage = empty_usage()
            add_usage(usage, reply.usage)
            await asyncio.to_thread(record_usage, model, usage, origin, provider.kind, pid)
        except Exception:  # noqa: BLE001 -- the count is not worth the text
            log.exception("%s summary: could not record the model usage", prompt_kind)

    text = _tidy(reply.text)
    if not text or reply.stop_reason in ("max_tokens", "length"):
        log.warning("%s summary: the model answer was empty or cut off, using the template", prompt_kind)
        return template(figures), TEMPLATE
    if len(text) > MAX_CHARS or len(text.splitlines()) > MAX_LINES:
        log.warning("%s summary: the model answer was too long, using the template", prompt_kind)
        return template(figures), REJECTED
    bad = unbacked(text, figures)
    if bad:
        log.warning("%s summary: numbers not in the figures (%s), using the template", prompt_kind,
                    ", ".join(bad[:5]))
        return template(figures), REJECTED
    return text, MODEL
