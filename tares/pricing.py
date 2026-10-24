"""Model pricing, for turning per-call token usage into a stored USD cost.

Cost is computed at write time and stored with the run, never recomputed later: prices change,
and the history must keep the cost that was true when the tokens were bought. Model ids are
matched by prefix, longest first, so claude-opus-5-5 is not priced as claude-opus-5 and dated
snapshots like claude-haiku-4-5-20251001 price the same as their family; anything unmatched costs
None: the caller records the tokens and leaves cost NULL rather than guess.

Two tables, because the two wire formats count cached input differently (TR-304):
- Anthropic's `input_tokens` EXCLUDES cache reads and writes, so the three input buckets add.
- OpenAI's `prompt_tokens` INCLUDES the cached part, so cached tokens are a discounted subset.
A provider that reports its own cost (OpenRouter, LiteLLM with cost tracking) wins over both:
`price_usage` takes `cost_usd` from the usage dict when the adapter put one there.
"""
from __future__ import annotations

import re

# (model id prefix, USD per Mtok input, USD per Mtok output, USD per Mtok cache read), from
# Anthropic's pricing page. A cache read is a price per model, not a share of input: it is 0.025x
# of input for Fable 5.1, 0.05x for Sonnet 5.5 and 0.10x for most of the rest.
# Matched longest prefix first (sorted below), so list order does not matter.
# Haiku 5.5 bills 0.50 / 2.50 / 0.05 for a prompt over 100K tokens; usage here is summed per
# call with no prompt length, so it is priced at the up-to-100K rate.
MODEL_PRICING = [
    ("claude-fable-5-1", 10.0, 50.0, 0.25),
    ("claude-fable-5", 10.0, 50.0, 1.00),
    ("claude-opus-5-5", 4.0, 20.0, 0.20),
    ("claude-opus-5", 5.0, 25.0, 0.50),
    ("claude-opus-4-8", 5.0, 25.0, 0.50),
    ("claude-opus-4-7", 5.0, 25.0, 0.50),
    ("claude-opus-4-6", 5.0, 25.0, 0.50),
    ("claude-opus-4-5", 5.0, 25.0, 0.50),
    ("claude-opus-4-1", 15.0, 75.0, 1.50),
    ("claude-opus-4-0", 15.0, 75.0, 1.50),
    ("claude-opus-4", 15.0, 75.0, 1.50),
    ("claude-sonnet-5-5", 2.0, 10.0, 0.10),
    ("claude-sonnet-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-4-6", 3.0, 15.0, 0.30),
    ("claude-sonnet-4-5", 3.0, 15.0, 0.30),
    ("claude-sonnet-4-0", 3.0, 15.0, 0.30),
    ("claude-sonnet-4", 3.0, 15.0, 0.30),
    ("claude-haiku-5-5", 0.10, 0.50, 0.01),
    ("claude-haiku-4-5", 1.0, 5.0, 0.10),
    ("claude-haiku-3-5", 0.80, 4.0, 0.08),
]
MODEL_PRICING.sort(key=lambda row: -len(row[0]))
# What may follow a prefix: nothing, a date snapshot (-20251001) or an alias suffix. Another
# version number (claude-opus-4-2, claude-opus-5-6) is a model this table does not list, so it
# is unpriced instead of borrowing the family's rate. The same test is SQL in claude_usage_store.
NEXT_VERSION_SQL = "^-[0-9]{1,2}([^0-9]|$)"
_NEXT_VERSION = re.compile(r"-\d{1,2}(?!\d)")
# Cache write bills at 1.25x the input rate (2x for a write kept an hour).
CACHE_WRITE_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0

# OpenAI: (prefix, USD per Mtok input, USD per Mtok output, cached-input multiplier).
# List prices at the time of writing; a wrong or missing family costs None, never a guess.
OPENAI_PRICING = [
    ("gpt-5-nano", 0.05, 0.40, 0.10),
    ("gpt-5-mini", 0.25, 2.00, 0.10),
    ("gpt-5", 1.25, 10.00, 0.10),
    ("gpt-4.1-nano", 0.10, 0.40, 0.25),
    ("gpt-4.1-mini", 0.40, 1.60, 0.25),
    ("gpt-4.1", 2.00, 8.00, 0.25),
    ("gpt-4o-mini", 0.15, 0.60, 0.50),
    ("gpt-4o", 2.50, 10.00, 0.50),
    ("o4-mini", 1.10, 4.40, 0.25),
    ("o3", 2.00, 8.00, 0.25),
]


def price_row(model: str) -> tuple[float, float, float] | None:
    """(usd_per_mtok_in, usd_per_mtok_out, usd_per_mtok_cache_read) for an Anthropic model id,
    or None when unknown."""
    model = model or ""
    for prefix, p_in, p_out, p_read in MODEL_PRICING:
        if model.startswith(prefix) and not _NEXT_VERSION.match(model, len(prefix)):
            return p_in, p_out, p_read
    return None


def price_for(model: str) -> tuple[float, float] | None:
    """(usd_per_mtok_in, usd_per_mtok_out) for an Anthropic model id, or None when unknown."""
    row = price_row(model)
    return None if row is None else row[:2]


def openai_price_for(model: str) -> tuple[float, float, float] | None:
    """(usd_per_mtok_in, usd_per_mtok_out, cached multiplier) for an OpenAI model id. A router
    alias with a vendor prefix (`openai/gpt-4.1`) is matched on its last path segment."""
    name = (model or "").rsplit("/", 1)[-1]
    for prefix, p_in, p_out, cached in OPENAI_PRICING:
        if name.startswith(prefix):
            return p_in, p_out, cached
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int,
             cache_creation_input_tokens: int = 0,
             cache_read_input_tokens: int = 0,
             cache_creation_1h_input_tokens: int = 0) -> float | None:
    """USD cost of summed Anthropic-format usage, or None when the model is unpriced.
    `cache_creation_1h_input_tokens` is the part of the cache writes kept for an hour (Claude
    Code's default on a subscription), which bills at the higher rate."""
    p = price_row(model)
    if p is None:
        return None
    p_in, p_out, p_read = p
    hour = min(max(int(cache_creation_1h_input_tokens or 0), 0), int(cache_creation_input_tokens or 0))
    return (input_tokens * p_in
            + output_tokens * p_out
            + (cache_creation_input_tokens - hour) * p_in * CACHE_WRITE_MULT
            + hour * p_in * CACHE_WRITE_1H_MULT
            + cache_read_input_tokens * p_read) / 1e6


def openai_cost_usd(model: str, input_tokens: int, output_tokens: int,
                    cache_read_input_tokens: int = 0) -> float | None:
    """USD cost of summed OpenAI-format usage: `input_tokens` is the whole prompt, of which
    `cache_read_input_tokens` were served from cache at the discounted rate."""
    p = openai_price_for(model)
    if p is None:
        return None
    p_in, p_out, cached_mult = p
    cached = min(max(int(cache_read_input_tokens or 0), 0), int(input_tokens or 0))
    return ((input_tokens - cached) * p_in
            + cached * p_in * cached_mult
            + output_tokens * p_out) / 1e6


def price_usage(kind: str, model: str, usage: dict) -> float | None:
    """The cost of one usage dict (the adapter's field names), by provider kind. A cost the
    provider reported itself wins; else the table for the kind; else None."""
    reported = usage.get("cost_usd")
    if reported is not None:
        return float(reported)
    if kind == "anthropic":
        return cost_usd(model, int(usage.get("input_tokens") or 0),
                        int(usage.get("output_tokens") or 0),
                        int(usage.get("cache_creation_input_tokens") or 0),
                        int(usage.get("cache_read_input_tokens") or 0))
    if kind == "openai":
        return openai_cost_usd(model, int(usage.get("input_tokens") or 0),
                               int(usage.get("output_tokens") or 0),
                               int(usage.get("cache_read_input_tokens") or 0))
    return None
