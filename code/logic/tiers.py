"""Single source of truth for AutoSE complexity tiers.

Tiers are ordered from least to most complex. The classifier picks one tier
per prompt; a session ratchets up through them and never downgrades.

Everything that needs to know "what tiers exist" derives it from here:
  - classifier.py  -> the set of valid classifications and the fallback scan
  - logic/main.py  -> the ``--mode`` CLI choices
  - tui/session.py -> the tier ranking used to ratchet complexity up

Editing this tuple therefore updates all of those at once. This is the public
edition, which intentionally ships without the ``advanced`` tier: its
Design/Plan/Execute/Validate workflow is not part of this repository, so the
classifier must never select it. Do not add ``"advanced"`` here.
"""

from __future__ import annotations

TIERS: tuple[str, ...] = ("lite", "standard")

# Rank by position so a session can compare tiers and never downgrade to a
# simpler one within the same session.
TIER_RANK: dict[str, int] = {tier: index for index, tier in enumerate(TIERS)}
