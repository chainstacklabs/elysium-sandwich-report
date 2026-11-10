"""Block -> UTC time, and the daily HYPE price in USD from Elysium's own USDC pools."""

import bisect
import datetime as dt
import statistics
from collections import defaultdict

from . import rpc
from .chain import USDC, is_hype, load, save

START = dt.datetime(2026, 10, 19, tzinfo=dt.UTC)  # Elysium mainnet block 1


def _first_blocks(targets, head):
    """First block at or after each unix time, by binary search over all targets at once."""
    lo, hi = {k: 1 for k in targets}, {k: head + 1 for k in targets}
    while any(lo[k] < hi[k] for k in targets):
        open_ = [k for k in targets if lo[k] < hi[k]]
        mids = {k: (lo[k] + hi[k]) // 2 for k in open_}
        blocks = rpc.batch([("eth_getBlockByNumber", [hex(min(mids[k], head)), False]) for k in open_])
        for k, blk in zip(open_, blocks, strict=True):
            if mids[k] > head or int(blk["timestamp"], 16) >= targets[k]:
                hi[k] = mids[k]
            else:
                lo[k] = mids[k] + 1
    return {k: lo[k] for k in targets if lo[k] <= head}


def build():
    """Cache the first block of every UTC hour since block 1."""
    head = load("head.json")
    hours = load("hours.json", {})
    t = START
    want = {}
    while t.timestamp() <= head["ts"]:
        key = t.strftime("%Y-%m-%dT%H")
        if key not in hours:
            want[key] = int(t.timestamp())
        t += dt.timedelta(hours=1)
    hours.update(_first_blocks(want, head["block"]))
    save("hours.json", dict(sorted(hours.items())))


class Clock:
    def __init__(self):
        hours = load("hours.json")
        self.head = load("head.json")
        self.names = sorted(hours, key=hours.get)
        self.starts = [hours[k] for k in self.names]

    def time(self, block):
        """Unix time of a block, interpolated inside its hour."""
        i = max(0, bisect.bisect_right(self.starts, block) - 1)
        t0 = dt.datetime.strptime(self.names[i], "%Y-%m-%dT%H").replace(tzinfo=dt.UTC).timestamp()
        if i + 1 < len(self.starts):
            b1, t1 = self.starts[i + 1], t0 + 3600
        else:
            b1, t1 = self.head["block"], self.head["ts"]
        return int(t0 + (t1 - t0) * (block - self.starts[i]) / max(1, b1 - self.starts[i]))

    def day(self, block):
        return self.hour(block)[:10]

    def hour(self, block):
        return self.names[max(0, bisect.bisect_right(self.starts, block) - 1)]


def prices(rows, pool_tokens):
    """Median USD per HYPE for each UTC day, from swaps of at least 0.01 HYPE in HYPE/USDC pools.
    Days with fewer than 20 such swaps use the nearest day that has them."""
    clock = Clock()
    samples = defaultdict(list)
    for r in rows:
        tokens = pool_tokens.get(r.get("pool"))
        if not tokens or USDC not in tokens or not all(t == USDC or is_hype(t) for t in tokens):
            continue
        t_in = tokens[0] if r["z"] else tokens[1]
        usd, hype = (r["in"], r["out"]) if t_in == USDC else (r["out"], r["in"])
        if hype >= 10**16:
            samples[clock.day(r["b"])].append((usd / 1e6) / (hype / 1e18))
    good = {d: statistics.median(v) for d, v in samples.items() if len(v) >= 20}
    days = sorted({k[:10] for k in clock.names})
    near = lambda d: min(good, key=lambda g: abs((dt.date.fromisoformat(g) - dt.date.fromisoformat(d)).days))  # noqa: E731
    out = {d: good.get(d) or good[near(d)] for d in days}
    save("prices.json", out)
    return out
