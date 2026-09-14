"""Model pricing — family-prefix matching and the cost formula.

The cost stored with a run must be right at write time, and an unknown model must yield None
(recorded as tokens without a cost), never a guessed number.
"""
import sys

from tares.pricing import (CACHE_READ_MULT, CACHE_WRITE_MULT, cost_usd, openai_cost_usd,
                           openai_price_for, price_for, price_usage)

P = F = 0
def ck(l, c, d=""):
    global P, F; P += 1 if c else 0; F += 0 if c else 1
    print(("  ok   " if c else "  FAIL ") + l + ("" if c else f"  {d}"))

# ── family-prefix resolution ─────────────────────────────────────────────────
ck("bare family id resolves", price_for("claude-sonnet-5") == (3.0, 15.0))
ck("dated snapshot resolves to its family", price_for("claude-haiku-4-5-20251001") == (1.0, 5.0))
ck("older sonnet resolves the same", price_for("claude-sonnet-4-6") == (3.0, 15.0))
ck("opus resolves", price_for("claude-opus-5") == (5.0, 25.0))
ck("fable resolves", price_for("claude-fable-5") == (10.0, 50.0))
ck("unknown claude model is unpriced", price_for("claude-mystery-9") is None)
ck("non-claude model is unpriced", price_for("gpt-4") is None)
ck("empty model is unpriced", price_for("") is None)

# ── the formula ──────────────────────────────────────────────────────────────
ck("input-only cost", abs(cost_usd("claude-sonnet-5", 1_000_000, 0) - 3.0) < 1e-9)
ck("output-only cost", abs(cost_usd("claude-sonnet-5", 0, 1_000_000) - 15.0) < 1e-9)
ck("cache write bills at 1.25x input",
   abs(cost_usd("claude-opus-5", 0, 0, cache_creation_input_tokens=1_000_000)
       - 5.0 * CACHE_WRITE_MULT) < 1e-9)
ck("cache read bills at 0.10x input",
   abs(cost_usd("claude-opus-5", 0, 0, cache_read_input_tokens=1_000_000)
       - 5.0 * CACHE_READ_MULT) < 1e-9)
ck("buckets are additive",
   abs(cost_usd("claude-haiku-4-5-20251001", 100_000, 10_000, 20_000, 50_000)
       - (100_000 * 1.0 + 10_000 * 5.0 + 20_000 * 1.0 * 1.25 + 50_000 * 1.0 * 0.10) / 1e6) < 1e-12)
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
ck("anthropic kind uses the Claude table", abs(price_usage("anthropic", "claude-sonnet-5", u) - 3.0) < 1e-9)
ck("openai kind uses the OpenAI table", abs(price_usage("openai", "gpt-4.1", u) - 2.0) < 1e-9)
ck("openai kind on a Claude alias behind a router is unpriced", price_usage("openai", "claude-sonnet-5", u) is None)
ck("anthropic kind on a GPT id is unpriced", price_usage("anthropic", "gpt-4.1", u) is None)
ck("a cost the provider reported wins over the table", price_usage("openai", "gpt-4.1", {**u, "cost_usd": 0.0042}) == 0.0042)
ck("a reported cost prices an unknown model too", price_usage("openai", "llama-3-70b", {**u, "cost_usd": 0.01}) == 0.01)
ck("an unknown kind is unpriced", price_usage("other", "gpt-4.1", u) is None)

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
