"""How the count changes with the rule: the report's matches sliced by how long the bot held.

Detection runs once with the longest window (30 s); a shorter window keeps the matches whose sell came within it, and
the first version's rule keeps those with the sell at most 2 blocks after the buy. Counting is the report's own (proofs
and proven operators), so the 30 s row equals the report's totals.
"""

import random

from .chain import PUBLIC_LAUNCH, load, save
from .detect import WINDOW


def _within(w):
    return lambda s: s["seconds"] <= w


ROWS = [("2 blocks (first version)", lambda s: s["back"]["b"] - s["front"]["b"] <= 2)]
ROWS += [(f"{w} s", _within(w)) for w in (5, 15) if w < WINDOW]
ROWS.append((f"{WINDOW} s (this report)", lambda s: True))


def run():
    found = load("sandwiches.json")
    hdr = load("headers.json")
    out = []
    for label, keep in ROWS:
        rs = [s for s in found if keep(s)]
        counted = [s for s in rs if s["counted"]]
        out.append(
            {
                "rule": label,
                "matches": len(rs),
                "wallets": len({s["wallet"] for s in rs}),
                "proven": sum(1 for s in rs if s.get("proofs")),
                "counted": len(counted),
                "counted_operators": len({s["operator"] for s in counted}),
                "after_launch": sum(1 for s in counted if hdr[str(s["front"]["b"])][0] >= PUBLIC_LAUNCH),
            }
        )
        print(f"rules {label}: {out[-1]}", flush=True)
    save("rules.json", out)
    spot_check(found)


def spot_check(found, k=30, seed=11):
    """Of k random matches counted without a proof of their own, in how many the victim lost and the bot profited."""
    pool = [s for s in found if s["counted"] and not s.get("proofs")]
    pick = random.Random(seed).sample(pool, min(k, len(pool)))
    out = {
        "checked": len(pick),
        "victim_lost": sum(1 for s in pick if (s["loss"] or 0) > 0),
        "bot_profited": sum(1 for s in pick if (s["loss"] or 0) > 0 and (s["profit"] or 0) > 0),
    }
    print(f"spot check: {out}", flush=True)
    save("handcheck.json", out)
