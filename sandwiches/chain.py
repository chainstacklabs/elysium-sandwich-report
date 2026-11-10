"""Elysium addresses, event topics, swap decoding and the on-disk cache."""
import gzip
import json
import os

CACHE = os.environ.get("SANDWICH_CACHE", "cache")

NATIVE = "0x0000000000000000000000000000000000000000"
WHYPE = "0xba876237b0f021a6c6d852d6c26e107997cc5e81"
USDC = "0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65"  # 6 decimals; many fake "USDC" tokens exist
STRESS_TEST = (87_023, 142_652)  # 2026-10-31 team stress test: dust transfers from 1,000 wallets, no trading
PUBLIC_LAUNCH = 1794229200  # 2026-11-09 13:00 UTC

V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
V3_SWAP = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V4_INIT = "0xdd466e674ea557f56295e2d0218a125ea4b4f0f6f3307b95f85e6110838d6438"
BIN_SWAP = "0xcad09799f755048f0fd388c6b2fcc8e0cebefd15756c6903522cc2a95f661f70"  # BinSwapped(int256,uint256,uint256)
LB_SWAP = "0xad7d6f97abf51ce18e17a38f4d70e975be9c0708474987bb3e26ad21bd93ca70"   # Liquidity Book Swap
TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
WETH_DEPOSIT = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"
WETH_WITHDRAWAL = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"

APPS = {  # names for the contracts victims called; unknown ones stay unnamed
    "0x1b341fdd55d56f8e08409a2c25ddfad93eb03c1a": "Uniswap Universal Router 2.2.0",
    "0xa13f7611115752deb2b13813bdde16a5073e4b45": "Uniswap Universal Router 2.1.2",
    "0xca11bde05977b3631167028862be2a173976ca11": "Multicall3",
}


def is_hype(token):
    return token in (NATIVE, WHYPE)


def _int256(word):
    v = int(word, 16)
    return v - (1 << 256) if v >= 1 << 255 else v


def parse_swap(log):
    """A Uniswap-shaped Swap log -> {pool, z (token0 in), in, out, sender, b, ti, li, tx}, or None."""
    t, d = log["topics"], log["data"][2:]
    w = [d[i:i + 64] for i in range(0, len(d), 64)]
    if t[0] == V4_SWAP:  # amounts are the caller's deltas: negative = paid in
        pool, sender, a0, a1 = log["address"].lower() + ":" + t[1], t[2], _int256(w[0]), _int256(w[1])
        z = a0 < 0
        amt_in, amt_out = (-a0, a1) if z else (-a1, a0)
    elif t[0] == V3_SWAP:  # amounts are the pool's deltas: positive = received
        pool, sender, a0, a1 = log["address"].lower(), t[1], _int256(w[0]), _int256(w[1])
        z = a0 > 0
        amt_in, amt_out = (a0, -a1) if z else (a1, -a0)
    elif t[0] == V2_SWAP:
        pool, sender = log["address"].lower(), t[1]
        i0, i1, o0, o1 = (int(x, 16) for x in w[:4])
        z = i0 > 0
        amt_in, amt_out = (i0, o1) if z else (i1, o0)
    else:
        return None
    if amt_in <= 0 or amt_out <= 0:
        return None
    return {"pool": pool, "z": z, "in": amt_in, "out": amt_out, "sender": "0x" + sender[-40:],
            "b": int(log.get("blockNumber", "0x0"), 16), "ti": int(log.get("transactionIndex", "0x0"), 16),
            "li": int(log.get("logIndex", "0x0"), 16), "tx": log.get("transactionHash")}


def load(name, default=None):
    path = os.path.join(CACHE, name)
    return json.load(open(path)) if os.path.exists(path) else default


def save(name, obj):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name)
    json.dump(obj, open(path + ".tmp", "w"))
    os.replace(path + ".tmp", path)


def write_rows(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with gzip.open(path + ".tmp", "wt") as f:
        for r in rows:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    os.replace(path + ".tmp", path)
