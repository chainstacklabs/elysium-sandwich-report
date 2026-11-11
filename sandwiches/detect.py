"""Find sandwiches: a bot's buy, then other people's buys in the same pool, then the bot's sell.

1. From swap logs: in one pool, a front swap, then one or more swaps in the same direction by other
   transactions, then a back swap in the opposite direction that sells within 5% of what the front bought,
   all within 30 seconds of the front (block timestamps). Each front and back joins one sandwich at most.
2. From transactions: the front and back come from the same wallet, each victim from a different one.
3. Tier A: one block, only victims between the legs. B: one block, other transactions between. C: the back
   lands in a later block.
4. Each victim's path: sent to the sequencer, or through Elysium's inbox on HyperEVM (the block that executed it
   consumed a delayed message).
"""

from collections import defaultdict, deque

from . import rpc
from .chain import APPS, NATIVE, is_hype, load, save
from .clock import Clock
from .scan import rows

WINDOW = 30  # max seconds from the front's block to the back's block
MARGIN = 30  # candidates use interpolated block times; exact timestamps are checked afterwards
QUEUE = 2000  # swaps kept per pool while scanning
TOLERANCE = 0.05  # the back sells within 5% of what the front bought
MAX_VICTIMS = 20
MAX_BETWEEN = 40  # transactions re-run per sandwich when measuring the victims' loss
LIMIT = 0.0001  # a victim "got its minimum" when its output is above an amount in its calldata by at most 0.01%


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


def candidates(tokens, window=WINDOW, keep=True):
    """Sandwich-shaped triples from swap logs, plus HYPE volume per day (net HYPE moved per transaction).
    `between` lists every other transaction that swapped in the pool from the front to the last victim, in order:
    the loss re-run replays them without the front."""
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
        now = clock.time(x["b"])
        while q and now - clock.time(q[0]["b"]) > window + MARGIN:
            q.popleft()
        matched = False
        for i, front in enumerate(q):
            if front["z"] == x["z"] or front["tx"] == x["tx"] or abs(x["in"] - front["out"]) > TOLERANCE * front["out"]:
                continue
            later = list(q)[i + 1 :]
            victims = [v for v in later if v["z"] == front["z"] and v["tx"] not in (front["tx"], x["tx"])]
            if victims:
                last = victims[-1]
                between = []
                for v in later:
                    if v["tx"] not in (front["tx"], x["tx"]) and v["tx"] not in between:
                        between.append(v["tx"])
                    if v is last:
                        break
                found.append(
                    {"pool": x["pool"], "front": front, "victims": victims[:MAX_VICTIMS], "back": x, "between": between}
                )
                del q[i]  # a front joins one sandwich, and its back is not reused as a front
                matched = True
                break
        if not matched:
            q.append(x)
            if len(q) > QUEUE:
                q.popleft()
    volume[clock.day(x["b"])] += abs(net)
    if keep:
        save("volume.json", {d: v / 1e18 for d, v in sorted(volume.items())})
    return found


def headers(blocks):
    """Timestamp, delayed messages read so far (the header nonce) and parent-chain block, per block, cached."""
    cache = load("headers.json", {})
    need = sorted({b for b in blocks if str(b) not in cache})
    for i in range(0, len(need), 2000):
        chunk = need[i : i + 2000]
        for b, h in zip(chunk, rpc.batch([("eth_getBlockByNumber", [hex(b), False]) for b in chunk]), strict=True):
            cache[str(b)] = [int(h["timestamp"], 16), int(h["nonce"], 16), int(h["l1BlockNumber"], 16)]
        if i % 20000 == 0:
            save("headers.json", cache)
            print(f"headers {i + len(chunk):,}/{len(need):,}", flush=True)
    save("headers.json", cache)
    return cache


def in_window(cands, window=WINDOW):
    """Keep candidates whose back is at most `window` seconds after the front by real block timestamps."""
    hdr = headers(b for c in cands for b in (c["front"]["b"], c["back"]["b"]))
    kept = []
    for c in cands:
        seconds = hdr[str(c["back"]["b"])][0] - hdr[str(c["front"]["b"])][0]
        if seconds <= window:
            c["seconds"] = seconds
            kept.append(c)
    return kept


def victim_paths(found):
    """Each victim's path: the block that executed it read a new delayed message (the inbox) or not (the sequencer)."""
    hdr = headers(b for s in found for v in s["victims"] for b in (v["b"], v["b"] - 1))
    for s in found:
        for v in s["victims"]:
            v["path"] = "inbox" if hdr[str(v["b"])][1] > hdr[str(v["b"] - 1)][1] else "sequencer"


def limits(found):
    """Mark sandwiches where a victim received just above an amount written in its own transaction: its minimum
    output, which the bot's buy was sized to reach. An output exactly equal to such an amount is an exact-output trade
    and does not count. Ordinary buys in the same pools show this about 0.5% of the time; victims 40%."""
    cache = load("limits.json", {})
    outs = {v["tx"]: v["out"] for s in found for v in s["victims"]}
    need = sorted(set(outs) - set(cache))
    for i in range(0, len(need), 1000):
        chunk = need[i : i + 1000]
        for h, t in zip(chunk, rpc.batch([("eth_getTransactionByHash", [h]) for h in chunk]), strict=True):
            body = (t.get("input") or "")[10:] if isinstance(t, dict) else ""
            words = (int(body[j : j + 64], 16) for j in range(0, len(body) - 63, 64))
            cache[h] = any(0 < outs[h] - w <= LIMIT * w for w in words)
        if i % 20000 == 0:
            save("limits.json", cache)
            print(f"limit checks {i + len(chunk):,}/{len(need):,}", flush=True)
    save("limits.json", cache)
    for s in found:
        s["limit"] = any(cache.get(v["tx"]) for v in s["victims"])


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


def classify(cands, txs, tokens, keep=True):
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
                "seconds": c["seconds"],
                "buy_to_victim_blocks": victims[0]["b"] - c["front"]["b"],
                "victim_to_sell_blocks": c["back"]["b"] - victims[-1]["b"],
                "between": c["between"][: c["between"].index(victims[-1]["tx"]) + 1],
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
    if keep:
        save("routers.json", sorted(routers))
    return out


def run():
    tokens = pool_tokens()
    raw = candidates(tokens)
    cands = in_window(raw)
    print(f"candidates {len(raw):,}, within {WINDOW} s {len(cands):,}", flush=True)
    txs = lookup(h for c in cands for h in [c["front"]["tx"], c["back"]["tx"]] + [v["tx"] for v in c["victims"]])
    found = classify(cands, txs, tokens)
    victim_paths(found)
    limits(found)
    save("sandwiches.json", found)
    tiers = defaultdict(int)
    for s in found:
        tiers[s["tier"]] += 1
    print(f"candidates {len(cands):,}, sandwiches {len(found):,} {dict(tiers)}")
