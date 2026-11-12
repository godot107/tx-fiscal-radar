"""Which answers Fiscal Note Desk may put in its answer cache.

The cache (Function App, blob container `answercache`) is keyed by the normalized question plus the time of the last
warehouse load, so tonight's load invalidates everything, and personalized (memory) answers never enter it. This module
decides the remaining question: is this answer good enough to hand to everyone who asks the same thing today?
"""
from __future__ import annotations

import os

# Share of an answer the groundedness check may flag before the answer is kept out of the cache. The check reads only
# fiscal-note text, so it can flag correct warehouse numbers too; a skipped answer costs one rerun, a cached weak one
# is repeated all day.
MAX_UNGROUNDED = float(os.environ.get("TXFR_CACHE_MAX_UNGROUNDED", "0.25"))
OK_STATUSES = ("RunStatus.COMPLETED", "refused_by_policy")


def cacheable(body: dict, max_ungrounded: float = MAX_UNGROUNDED) -> bool:
    """Complete, error-free answers that the groundedness check didn't flag (or didn't need to check)."""
    if body.get("status") not in OK_STATUSES or body.get("error"):
        return False
    g = (body.get("guard") or {}).get("groundedness") or {}
    return not (g.get("ungrounded") and (g.get("ungrounded_pct") or 0) > max_ungrounded)
