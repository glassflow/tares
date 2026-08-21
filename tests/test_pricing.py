"""Model pricing — family-prefix matching and the cost formula.

The cost stored with a run must be right at write time, and an unknown model must yield None
(recorded as tokens without a cost), never a guessed number.
"""
import sys

from tares.pricing import CACHE_READ_MULT, CACHE_WRITE_MULT, cost_usd, price_for

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

print(f"\n{P} passed, {F} failed")
sys.exit(1 if F else 0)
