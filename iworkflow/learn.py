"""Empirical routing — adjust capability priors with what the ledger has seen.

Capability priors (routing.py) decide the *order* of candidates; this nudges that
order using real outcomes: a provider that has been failing a lot (low success
rate over enough attempts) is pushed to the BACK of the candidate list, so the
scheduler stops leading with a subscription that's been throttling/erroring.

It's a tiebreaker, not an override: capability order is preserved among healthy
providers — we only demote consistently-unreliable ones. Data comes from
stats.provider_stats (the run ledger). No quota.
"""

from __future__ import annotations

from typing import Any


def adjust_order(order: list[str], stats: dict[str, dict[str, Any]],
                 min_success: float = 0.3, min_samples: int = 3) -> list[str]:
    """Stable-partition `order`: healthy/unknown providers first (capability
    order kept), demoted (low success rate over >= min_samples) providers last."""
    def demoted(p: str) -> bool:
        s = stats.get(p)
        if not s:
            return False
        decided = s.get("done", 0) + s.get("rate_limited", 0) + s.get("error", 0)
        rate = s.get("success_rate")
        return decided >= min_samples and rate is not None and rate < min_success

    healthy = [p for p in order if not demoted(p)]
    weak = [p for p in order if demoted(p)]
    return healthy + weak
