"""The meter's units and the anchor bridge: how much one unit is worth.

One home for the question "how much is a tick and how does a Δpp become a
paid dollar" (methodology v1 §3, precision policy v1.1 §4): the tick the
Ollama meter quantizes readings to, the residue band that comparison logic
uses around a tick boundary, and the anchor's amortization into $/pp. The
mirror of pricing.py on the legacy side: pricing owns what a token is worth
on the new plan, meter owns what a pp is worth on the legacy one.

Leaf module: zero internal imports, ever. Its value is being below every
consumer (runner reads the meter's tick; analyze and predict read the
bridge); the first internal import added here is reverted.
"""

TICK_PP = 0.1  # one 0.001 meter tick, in percentage points
# Relative float-residue band around a tick boundary, for COMPARISON logic only
# (the deltas stay exact — precision policy): a reading of exactly one tick must
# not read as a collapse on 0.09999999999999998, nor a true exact-boundary value
# miss a threshold by ~1e-13 relative. Nothing persisted is ever rounded by it.
TICK_BAND = 1e-9
WEEKS_PER_MONTH = 4.345  # the anchor's amortization (methodology v1's cost model)
# The session window's secondary $/pp is DERIVED, never an independent anchor
# (methodology v1 §3): session $/pp = weekly $/pp / R, R the session:weekly
# tick ratio, live-verified at 6.22 (expected range 5-7) -> ~$0.037/session pp.
SESSION_R = 6.22
# The paid-dollar re-denomination every comparison point applies by default
# (methodology v1.3 §3): the new plan sells credits at a per-tier multiplier
# (Max $100 → $300 credits), so the anchor tier's ratio is 3.0.
DEFAULT_CREDIT_RATIO = 3.0


def usd_per_pp(ancla: float) -> float:
    """The anchor bridge: P_LEGADO amortized per week, divided by the 100 pp window."""
    return (ancla / WEEKS_PER_MONTH) / 100.0


def session_usd_per_pp(usd_weekly: float) -> float:
    """The session window's derived $/pp: the weekly bridge divided by R."""
    return usd_weekly / SESSION_R
