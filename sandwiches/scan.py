"""Download every swap on Elysium into cache/logs/, one gzip file per 10k-block chunk.

Uniswap-shaped pools (Uniswap V2/V3/V4 and forks that reuse their events) are decoded from their Swap logs.
Bin AMM and Liquidity Book pools use other event formats, so their swaps are rebuilt from the token transfers
in and out of the pool: what the pool received is `in`, what it sent is `out`.
"""

import glob
import gzip
import json
import os
from collections import defaultdict

from . import rpc
from .chain import (
    BIN_SWAP,
    CACHE,
    CUTOFF,
    LB_SWAP,
    STRESS_TEST,
    TRANSFER,
    V2_SWAP,
    V3_SWAP,
    V4_INIT,
    V4_SWAP,
    parse_swap,
    save,
    write_rows,
)
from .clock import _first_blocks

LOGS = os.path.join(CACHE, "logs")


def chunks(head):
    """(start, end, complete) for each 10k-block range outside the stress test."""
    for start in range(1, head + 1, rpc.LOG_RANGE):
        end = min(start + rpc.LOG_RANGE - 1, head)
        if end < STRESS_TEST[0] or start > STRESS_TEST[1]:
            yield start, end, end == start + rpc.LOG_RANGE - 1


def uniswap_rows(start, end):
    rows = []
    for log in rpc.logs(start, end, [[V4_SWAP, V3_SWAP, V2_SWAP, V4_INIT]]):
        if log["topics"][0] == V4_INIT:  # pool creation: currencies and hook of a V4 pool
            d = log["data"][2:]
            rows.append(
                {
                    "pool_tokens": log["address"].lower() + ":" + log["topics"][1],
                    "c0": "0x" + log["topics"][2][-40:],
                    "c1": "0x" + log["topics"][3][-40:],
                    "hooks": "0x" + d[152:192],
                    "b": int(log["blockNumber"], 16),
                    "li": int(log["logIndex"], 16),
                }
            )
        elif (swap := parse_swap(log)) is not None:
            swap["venue"] = "v4" if log["topics"][0] == V4_SWAP else "v2v3"
            rows.append(swap)
    return rows


def transfer_rows(start, end):
    events = rpc.logs(start, end, [[BIN_SWAP, LB_SWAP]])
    first = {}  # (tx, pool) -> its first swap event
    for log in events:
        first.setdefault(
            (log["transactionHash"], log["address"].lower()), (log, "bin" if log["topics"][0] == BIN_SWAP else "lb")
        )
    pools = sorted({pool for _, pool in first})
    flow = defaultdict(lambda: {"in": defaultdict(int), "out": defaultdict(int), "sender": None})
    for i in range(0, len(pools), 40):
        padded = ["0x" + "0" * 24 + p[2:] for p in pools[i : i + 40]]
        for log in rpc.logs(start, end, [TRANSFER, padded]) + rpc.logs(start, end, [TRANSFER, None, padded]):
            if len(log["topics"]) < 3:
                continue
            src, dst = "0x" + log["topics"][1][-40:], "0x" + log["topics"][2][-40:]
            amount = rpc.num(log["data"])
            for pool, side in ((dst, "in"), (src, "out")):
                key = (log["transactionHash"], pool)
                if key in first:
                    flow[key][side][log["address"].lower()] += amount
                    if side == "in" and flow[key]["sender"] is None:
                        flow[key]["sender"] = src
    rows, seen = [], set()
    for (tx, pool), (log, venue) in first.items():
        f = flow.get((tx, pool))
        if not f or len(f["in"]) != 1 or len(f["out"]) != 1:
            continue  # not a plain one-token-in, one-token-out swap
        (t_in, a_in), (t_out, a_out) = next(iter(f["in"].items())), next(iter(f["out"].items()))
        if t_in == t_out:
            continue
        c0, c1 = sorted((t_in, t_out))
        b = int(log["blockNumber"], 16)
        if pool not in seen:
            seen.add(pool)
            rows.append({"pool_tokens": pool, "c0": c0, "c1": c1, "venue": venue, "b": b, "li": -1})
        rows.append(
            {
                "pool": pool,
                "venue": venue,
                "z": t_in == c0,
                "in": a_in,
                "out": a_out,
                "sender": f["sender"],
                "b": b,
                "ti": int(log["transactionIndex"], 16),
                "li": int(log["logIndex"], 16),
                "tx": tx,
            }
        )
    return rows


def run():
    tip = rpc.head()
    first_after = _first_blocks({"cutoff": CUTOFF}, tip).get("cutoff")
    head = first_after - 1 if first_after else tip
    for start, end, complete in chunks(head):
        path = os.path.join(LOGS, f"{start:09d}.jsonl.gz")
        if complete and os.path.exists(path):
            continue
        rows = uniswap_rows(start, end) + transfer_rows(start, end)
        rows.sort(key=lambda r: (r["b"], r["li"]))
        write_rows(path, rows)
        print(f"scan {end:,} rows={len(rows)}", flush=True)
    save("head.json", {"block": head, "ts": int(rpc.call("eth_getBlockByNumber", [hex(head), False])["timestamp"], 16)})


def rows():
    """Every cached swap and pool-token row, in chain order."""
    for path in sorted(glob.glob(os.path.join(LOGS, "*.jsonl.gz"))):
        with gzip.open(path, "rt") as f:
            for line in f:
                yield json.loads(line)
