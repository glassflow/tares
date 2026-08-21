"""Anthropic model pricing, for turning per-call token usage into a stored USD cost.

Cost is computed at write time and stored with the run, never recomputed later: prices change,
and the history must keep the cost that was true when the tokens were bought. Model ids are
matched by family prefix (first match wins) so dated snapshots like claude-haiku-4-5-20251001
price the same as their family; anything unmatched costs None — the caller records the tokens
and leaves cost NULL rather than guess.
"""
from __future__ import annotations

# (model id prefix, USD per Mtok input, USD per Mtok output); first match wins.
MODEL_PRICING = [
    ("claude-fable-5", 10.0, 50.0),
    ("claude-opus-", 5.0, 25.0),
    ("claude-sonnet-", 3.0, 15.0),
    ("claude-haiku-", 1.0, 5.0),
]
# Cache write bills at 1.25x the input rate, cache read at 0.10x. `input_tokens` in the API's
# usage block already excludes cached tokens, so the three input buckets are additive.
CACHE_WRITE_MULT = 1.25
CACHE_READ_MULT = 0.10


def price_for(model: str) -> tuple[float, float] | None:
    """(usd_per_mtok_in, usd_per_mtok_out) for a model id, or None when unknown."""
    for prefix, p_in, p_out in MODEL_PRICING:
        if (model or "").startswith(prefix):
            return p_in, p_out
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int,
             cache_creation_input_tokens: int = 0,
             cache_read_input_tokens: int = 0) -> float | None:
    """USD cost of one or more calls' summed usage, or None when the model is unpriced."""
    p = price_for(model)
    if p is None:
        return None
    p_in, p_out = p
    return (input_tokens * p_in
            + output_tokens * p_out
            + cache_creation_input_tokens * p_in * CACHE_WRITE_MULT
            + cache_read_input_tokens * p_in * CACHE_READ_MULT) / 1e6
