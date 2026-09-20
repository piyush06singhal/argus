"""Promotional banners (ARGUS demo counterexample).

This module is changed by the most recent commit in the demo history, and it
has nothing to do with the checkout failure. It exists so Phase 6 has to
distinguish "recently changed" from "relevant": a change with no path to the
affected component, no presence in the failing trace and no overlap with the
reproduced failure must be reported as temporally recent but unconnected.
"""

from __future__ import annotations

from typing import Dict, List

BANNER_VARIANTS: List[str] = ["spring_sale", "free_shipping", "loyalty"]


def active_banner(region: str) -> Dict[str, str]:
    """Return the banner variant shown for ``region``."""
    variant = BANNER_VARIANTS[hash(region) % len(BANNER_VARIANTS)]
    return {"region": region, "variant": variant}
