"""Find sandwiches: a bot's buy, then other people's buys in the same pool, then the bot's sell.

1. From swap logs: in one pool, a front swap, then one or more swaps in the same direction by other
   transactions, then a back swap in the opposite direction that sells within 5% of what the front bought,
   all within 2 blocks.
2. From transactions: the front and back come from the same wallet, each victim from a different one.
3. Tier A: one block, only victims between the legs. B: one block, other transactions between. C: the back
   lands 1-2 blocks after the front.
"""

from collections import defaultdict, deque

from . import rpc
from .chain import APPS, NATIVE, is_hype, load, save
from .clock import Clock
from .scan import rows

WINDOW = 2  # max blocks from front to back
TOLERANCE = 0.05  # the back sells within 5% of what the front bought
MAX_VICTIMS = 5


def pool_tokens():
    """(token0, token1) for every pool: from V4 Initialize logs, transfer-built pools and token0()/token1() calls."""
    tokens = load("pool_tokens.json", {})
    missing, hooks = set(), set()
    for r in rows():
        if "pool_tokens" in r:
            tokens[r["pool_tokens"]] = [r["c0"], r["c1"]]
            if r.get("hooks", NATIVE) != NATIVE:
                hooks.add(r["hooks"])
        elif r["pool"] not in tokens:
            missing.add(r["pool"])
    missing = sorted(p for p in missing if ":" not in p and p not in tokens)
    res = rpc.batch(
        [
            c
            for p in missing
            for c in (
                ("eth_call", [{"to": p, "data": "0x0dfe1681"}, "latest"]),
                ("eth_call", [{"to": p, "data": "0xd21220a7"}, "latest"]),
            )
        ]
    )
    for i, p in enumerate(missing):
        a, b = res[2 * i], res[2 * i + 1]
        if isinstance(a, str) and isinstance(b, str) and len(a) >= 66 and len(b) >= 66:
            tokens[p] = ["0x" + a[-40:], "0x" + b[-40:]]
    save("pool_tokens.json", tokens)
    save("hooks.json", sorted(hooks))  # V4 hooks take fees; they are market infrastructure, not operators
    return tokens


def hype_side(swap, tokens):
    """HYPE amount of a swap (paid or received), 0 if neither side is HYPE/WHYPE."""
    t = tokens.get(swap["pool"])
    if not t:
        return 0
    t_in, t_out = (t[0], t[1]) if swap["z"] else (t[1], t[0])
    return swap["in"] if is_hype(t_in) else (swap["out"] if is_hype(t_out) else 0)


def candidates(tokens):
    """Sandwich-shaped triples from swap logs, plus HYPE volume per day (net HYPE moved per transaction)."""
    clock = Clock()
    recent = defaultdict(deque)
    found = []
    volume = defaultdict(int)
    tx, net = None, 0
    for x in rows():
        if "pool" not in x:
            continue
        if x["tx"] != tx:
            volume[clock.day(x["b"])] += abs(net)
            tx, net = x["tx"], 0
        t = tokens.get(x["pool"])
        if t:  # flash accounting lets one tx swap large amounts in and out, so volume is the net per tx
            t_in, t_out = (t[0], t[1]) if x["z"] else (t[1], t[0])
            net += (x["in"] if is_hype(t_in) else 0) - (x["out"] if is_hype(t_out) else 0)
        q = recent[x["pool"]]
        while q and x["b"] - q[0]["b"] > WINDOW:
            q.popleft()
        for i, front in enumerate(q):
            if front["z"] == x["z"] or front["tx"] == x["tx"] or abs(x["in"] - front["out"]) > TOLERANCE * front["out"]:
                continue
            victims = [v for v in list(q)[i + 1 :] if v["z"] == front["z"] and v["tx"] not in (front["tx"], x["tx"])]
            if victims:
                found.append({"pool": x["pool"], "front": front, "victims": victims[:MAX_VICTIMS], "back": x})
                break
        q.append(x)
        if len(q) > 40:
            q.popleft()
    volume[clock.day(x["b"])] += abs(net)
    save("volume.json", {d: v / 1e18 for d, v in sorted(volume.items())})
    return found


def lookup(hashes):
    """from, to, nonce and fee fields of each transaction, cached."""
    txs = load("txs.json", {})
    need = sorted(set(hashes) - set(txs))
    for i in range(0, len(need), 1000):
        chunk = need[i : i + 1000]
        for h, t in zip(chunk, rpc.batch([("eth_getTransactionByHash", [h]) for h in chunk]), strict=True):
            if isinstance(t, dict) and "from" in t:
                txs[h] = {
                    "from": t["from"].lower(),
                    "to": (t["to"] or "").lower(),
                    "nonce": int(t["nonce"], 16),
                    "tip": t.get("maxPriorityFeePerGas"),
                    "cap": t.get("maxFeePerGas"),
                    "price": t.get("gasPrice"),
                }
        print(f"tx lookups {i + len(chunk):,}/{len(need):,}", flush=True)
    save("txs.json", txs)
    return txs


def base_fees(blocks):
    fees = load("base_fees.json", {})
    need = sorted(set(blocks) - {int(b) for b in fees})
    for b, blk in zip(need, rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in need]), strict=True):
        fees[str(b)] = int(blk["baseFeePerGas"], 16)
    save("base_fees.json", fees)
    return fees


def effective_tip(t, base):
    if t["tip"] is not None:
        return min(int(t["tip"], 16), int(t["cap"], 16) - base)
    return int(t["price"], 16) - base if t["price"] else 0


def classify(cands, txs, tokens):
    fees = base_fees([c["front"]["b"] for c in cands if c["front"]["b"] == c["back"]["b"]])
    out = []
    for c in cands:
        f, k = txs.get(c["front"]["tx"]), txs.get(c["back"]["tx"])
        if not f or not k or f["from"] != k["from"]:
            continue
        victims = [v for v in c["victims"] if v["tx"] in txs and txs[v["tx"]]["from"] != f["from"]]
        if not victims:
            continue
        same_block = c["front"]["b"] == c["back"]["b"]
        if same_block:
            tier = "A" if c["back"]["ti"] - c["front"]["ti"] - 1 == len({v["tx"] for v in victims}) else "B"
            base = fees[str(c["front"]["b"])]
            tf, tv, tk = (effective_tip(t, base) for t in (f, txs[victims[0]["tx"]], k))
            tip_signature = tf > tv >= tk
        else:
            tier, tip_signature = "C", None
        out.append(
            {
                "pool": c["pool"],
                "tier": tier,
                "front": c["front"],
                "back": c["back"],
                "victims": victims,
                "wallet": f["from"],
                "to_front": f["to"],
                "to_back": k["to"],
                "signed_together": k["nonce"] == f["nonce"] + 1,
                "tip_signature": tip_signature,
                "victim_hype": sum(hype_side(v, tokens) for v in victims),
                "front_hype": hype_side(c["front"], tokens),
            }
        )
    # A public router is a contract that 5+ wallets other than attackers call as victims. A bot contract can have
    # many callers too, but they are all the operator's own wallets.
    attackers = {s["wallet"] for s in out}
    callers = defaultdict(set)
    for s in out:
        for v in s["victims"]:
            t = txs[v["tx"]]
            if t["to"] and t["from"] not in attackers:
                callers[t["to"]].add(t["from"])
    routers = {a for a, c in callers.items() if len(c) >= 5} | set(APPS)
    for s in out:
        s["bot"] = next((a for a in (s["to_front"], s["front"]["sender"]) if a not in routers), None)
    save("routers.json", sorted(routers))
    return out


def run():
    tokens = pool_tokens()
    cands = candidates(tokens)
    txs = lookup(h for c in cands for h in [c["front"]["tx"], c["back"]["tx"]] + [v["tx"] for v in c["victims"]])
    found = classify(cands, txs, tokens)
    save("sandwiches.json", found)
    tiers = defaultdict(int)
    for s in found:
        tiers[s["tier"]] += 1
    print(f"candidates {len(cands):,}, sandwiches {len(found):,} {dict(tiers)}")
