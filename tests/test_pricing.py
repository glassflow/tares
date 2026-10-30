"""Model pricing: longest-prefix matching, per-model cache-read prices and the cost formula.

The cost stored with a run must be right at write time, and an unknown model must yield None
(recorded as tokens without a cost), never a guessed number.
"""
import sys

from tares.pricing import (CACHE_WRITE_1H_MULT, CACHE_WRITE_MULT, MODEL_PRICING, cost_usd,
                           openai_cost_usd, openai_price_for, price_for, price_row, price_usage)

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

# ── family-prefix resolution ─────────────────────────────────────────────────
ck("bare family id resolves", price_for("claude-sonnet-5") == (2.0, 10.0))
ck("dated snapshot resolves to its family", price_for("claude-haiku-4-5-20251001") == (1.0, 5.0))
ck("older sonnet resolves the same", price_for("claude-sonnet-4-6") == (3.0, 15.0))
ck("opus resolves", price_for("claude-opus-5") == (5.0, 25.0))
ck("fable resolves", price_for("claude-fable-5") == (10.0, 50.0))
ck("unknown claude model is unpriced", price_for("claude-mystery-9") is None)
ck("non-claude model is unpriced", price_for("gpt-4") is None)
ck("empty model is unpriced", price_for("") is None)

# ── the formula ──────────────────────────────────────────────────────────────
ck("input-only cost", abs(cost_usd("claude-sonnet-5", 1_000_000, 0) - 2.0) < 1e-9)
ck("output-only cost", abs(cost_usd("claude-sonnet-5", 0, 1_000_000) - 10.0) < 1e-9)
ck("cache write bills at 1.25x input",
   abs(cost_usd("claude-opus-5", 0, 0, cache_creation_input_tokens=1_000_000)
       - 5.0 * CACHE_WRITE_MULT) < 1e-9)
ck("cache read bills at the model's own price",
   abs(cost_usd("claude-opus-5", 0, 0, cache_read_input_tokens=1_000_000) - 0.50) < 1e-9)
ck("an hour's cache write bills at 2x input",
   abs(cost_usd("claude-opus-5", 0, 0, 1_000_000, 0, 1_000_000) - 5.0 * CACHE_WRITE_1H_MULT) < 1e-9)
ck("buckets are additive",
   abs(cost_usd("claude-haiku-4-5-20251001", 100_000, 10_000, 20_000, 50_000)
       - (100_000 * 1.0 + 10_000 * 5.0 + 20_000 * 1.0 * 1.25 + 50_000 * 1.0 * 0.10) / 1e6) < 1e-12)
# ── TR-672: per-model rows, from Anthropic's pricing page ────────────────────
# model id -> (input, output, cache read) in USD per Mtok
TABLE = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.00),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-opus-4-5": (5.0, 25.0, 0.50),
    "claude-opus-4-1": (15.0, 75.0, 1.50),
    "claude-opus-4-20250514": (15.0, 75.0, 1.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10),
    "claude-sonnet-5": (2.0, 10.0, 0.20),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-sonnet-4-5-20250929": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10),
    "claude-3-5-haiku-20241022": (0.80, 4.0, 0.08),
}
for m, want in TABLE.items():
    ck(f"{m} prices {want}", price_row(m) == want, str(price_row(m)))
ck("a dated snapshot of the newer model keeps the newer price",
   price_row("claude-opus-5-5-20261001") == (4.0, 20.0, 0.20), str(price_row("claude-opus-5-5-20261001")))
ck("the table is ordered longest prefix first",
   [len(r[0]) for r in MODEL_PRICING] == sorted((len(r[0]) for r in MODEL_PRICING), reverse=True))
ck("an unlisted version is unpriced, not the family's rate",
   all(price_row(m) is None for m in ("claude-opus-4-2", "claude-opus-5-6", "claude-sonnet-4-9",
                                      "claude-haiku-4-6", "claude-fable-5-2")))
ck("a version that runs on from a listed prefix is unpriced, not that row's rate",
   all(price_row(m) is None for m in ("claude-opus-4-10", "claude-fable-5-12", "claude-sonnet-45")),
   str([price_row(m) for m in ("claude-opus-4-10", "claude-fable-5-12", "claude-sonnet-45")]))
ck("the made-up haiku 3.5 id matches no row", price_row("claude-haiku-3-5-20241022") is None)
ck("a version-less alias and a date snapshot still price",
   price_row("claude-opus-4-0") == (15.0, 75.0, 1.50) and price_row("claude-opus-4-5-20251101") == (5.0, 25.0, 0.50))
ck("an unlisted family is unpriced", price_row("claude-haiku-9") is None and price_row("claude-mythos-5-1") is None)
# the ticket's four calls
ck("opus 5.5 input+output", abs(cost_usd("claude-opus-5-5", 1_000_000, 1_000_000) - 24.0) < 1e-9)
ck("opus 5.5 cache read", abs(cost_usd("claude-opus-5-5", 0, 0, 0, 1_000_000) - 0.20) < 1e-9)
ck("sonnet 5.5 input+output", abs(cost_usd("claude-sonnet-5-5", 1_000_000, 1_000_000) - 12.0) < 1e-9)
ck("fable 5.1 cache read", abs(cost_usd("claude-fable-5-1", 0, 0, 0, 1_000_000) - 0.25) < 1e-9)
ck("cache writes stay 1.25x input on a model with a cheaper read",
   abs(cost_usd("claude-opus-5-5", 0, 0, 1_000_000) - 4.0 * CACHE_WRITE_MULT) < 1e-9)
ck("unknown model costs None, not zero", cost_usd("claude-mystery-9", 1000, 1000) is None)
ck("zero usage on a known model is 0.0", cost_usd("claude-sonnet-5", 0, 0) == 0.0)

# ── OpenAI (TR-304): prompt_tokens include the cached part ───────────────────
ck("gpt-5 resolves", openai_price_for("gpt-5") == (1.25, 10.0, 0.10))
ck("longer prefix wins: gpt-5-mini is not priced as gpt-5", openai_price_for("gpt-5-mini") == (0.25, 2.0, 0.10))
ck("dated snapshot resolves to its family", openai_price_for("gpt-4o-2024-11-20") == (2.5, 10.0, 0.5))
ck("a router alias with a vendor prefix matches on the last segment", openai_price_for("openai/gpt-4.1") == (2.0, 8.0, 0.25))
ck("unknown OpenAI model is unpriced", openai_price_for("gpt-99") is None)
ck("a Claude id is not in the OpenAI table", openai_price_for("claude-sonnet-5") is None)
ck("cached tokens are a discounted subset of the prompt, not added on top",
   abs(openai_cost_usd("gpt-4.1", 1_000_000, 0, cache_read_input_tokens=400_000)
       - (600_000 * 2.0 + 400_000 * 2.0 * 0.25) / 1e6) < 1e-9)
ck("cached cannot exceed the prompt", abs(openai_cost_usd("gpt-4.1", 100, 0, cache_read_input_tokens=1000)
                                         - 100 * 2.0 * 0.25 / 1e6) < 1e-12)
ck("output priced", abs(openai_cost_usd("gpt-5", 0, 1_000_000) - 10.0) < 1e-9)

# ── price_usage: by provider kind, provider-reported cost wins ──────────────
u = {"input_tokens": 1_000_000, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
ck("anthropic kind uses the Claude table", abs(price_usage("anthropic", "claude-sonnet-5", u) - 2.0) < 1e-9)
ck("openai kind uses the OpenAI table", abs(price_usage("openai", "gpt-4.1", u) - 2.0) < 1e-9)
ck("openai kind on a Claude alias behind a router is unpriced", price_usage("openai", "claude-sonnet-5", u) is None)
ck("anthropic kind on a GPT id is unpriced", price_usage("anthropic", "gpt-4.1", u) is None)
ck("a cost the provider reported wins over the table", price_usage("openai", "gpt-4.1", {**u, "cost_usd": 0.0042}) == 0.0042)
ck("a reported cost prices an unknown model too", price_usage("openai", "llama-3-70b", {**u, "cost_usd": 0.01}) == 0.01)
ck("an unknown kind is unpriced", price_usage("other", "gpt-4.1", u) is None)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
