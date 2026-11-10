"""Count swap events of other AMM formats over the whole chain, to show what the Uniswap-shaped scan misses."""

from collections import Counter

from . import rpc
from .chain import BIN_SWAP, LB_SWAP, save
from .scan import chunks, rows

FORMATS = {
    BIN_SWAP: "Bin AMM pools",
    LB_SWAP: "Liquidity Book pools",
    "0xa6fee24309b1d83d9ec7b9e4dbb73c6f882746efbfb26db7b7d9e9f2fb6dc95a": "Aggregator Exchange events",
    "0x89f5adc174562e07c9c9b1cae7109bbecb21cf9d1b2847e550042b8653c54a0e": "Bonding-curve Buy / Sell events",
    "0xa082022e93cfcd9f1da5f9236718053910f7e840da080c789c7845698dc032ff": "Bonding-curve Buy / Sell events",
    "0x121cb44ee54098b1a04743c487e7460d8dd429b27f88b1f4d4767396e1a59f79": "Algebra, PancakeSwap V3, Curve, Balancer",
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83": "Algebra, PancakeSwap V3, Curve, Balancer",
    "0x8b3e96f2b889fa771c53c981b40daf005f63f637f1869f707052d15a3dd97140": "Algebra, PancakeSwap V3, Curve, Balancer",
    "0xb2e76ae99761dc136e598d4a629bb347eccb9532a5f8bbd72e18467c3c34cc98": "Algebra, PancakeSwap V3, Curve, Balancer",
    "0x2170c741c41531aec20e7c107c24eecfdd15e69c9bb0a8dd37b1840b9e0b207b": "Algebra, PancakeSwap V3, Curve, Balancer",
}


def run():
    counts = Counter({name: 0 for name in FORMATS.values()})
    for r in rows():  # Uniswap-shaped swaps are already in the cache
        if r.get("venue") in ("v4", "v2v3") and "pool" in r:
            counts["Uniswap V4" if r["venue"] == "v4" else "Uniswap V2/V3"] += 1
    for start, end, _ in chunks(rpc.head()):
        for log in rpc.logs(start, end, [list(FORMATS)]):
            counts[FORMATS[log["topics"][0]]] += 1
    save("venues.json", dict(counts))
    print(dict(counts))
