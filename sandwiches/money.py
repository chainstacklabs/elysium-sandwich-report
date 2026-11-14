"""What each sandwich moved: the bot's profit from traces, the victim's loss from simulation."""

import math
from collections import defaultdict

from . import rpc
from .chain import TRANSFER, USDC, WETH_DEPOSIT, WETH_WITHDRAWAL, WHYPE, is_hype, load, parse_swap, save
from .detect import MAX_BETWEEN


def flows(trace):
    """Net [HYPE, WHYPE, USDC] per address inside one transaction, from a callTracer trace with logs."""
    f = defaultdict(lambda: [0, 0, 0])

    def walk(frame):
        if frame.get("error"):
            return  # a reverted frame moved nothing
        value = rpc.num(frame.get("value"))
        if value and frame.get("to"):
            f[frame["from"].lower()][0] -= value
            f[frame["to"].lower()][0] += value
        for log in frame.get("logs", []):
            t, token = log.get("topics") or [], log["address"].lower()
            amount = rpc.num(log.get("data", "0x")[:66])
            if token == WHYPE and t and t[0] in (WETH_DEPOSIT, WETH_WITHDRAWAL) and len(t) > 1:
                f["0x" + t[1][-40:]][1] += amount if t[0] == WETH_DEPOSIT else -amount
            elif token in (WHYPE, USDC) and t and t[0] == TRANSFER and len(t) > 2:
                k = 1 if token == WHYPE else 2
                f["0x" + t[1][-40:]][k] -= amount
                f["0x" + t[2][-40:]][k] += amount
        for child in frame.get("calls", []):
            walk(child)

    walk(trace)
    return {a: x for a, x in f.items() if any(x)}


def trace(sandwiches):
    """Trace both legs of every sandwich and keep their money flows and gas cost."""
    cache = load("flows.json", {})
    need = sorted({h for s in sandwiches for h in (s["front"]["tx"], s["back"]["tx"])} - set(cache))
    for i in range(0, len(need), 20):
        chunk = need[i : i + 20]
        traces = rpc.batch(
            [
                ("debug_traceTransaction", [h, {"tracer": "callTracer", "tracerConfig": {"withLog": True}}])
                for h in chunk
            ]
        )
        receipts = rpc.batch([("eth_getTransactionReceipt", [h]) for h in chunk])
        for h, t, r in zip(chunk, traces, receipts, strict=True):
            if isinstance(t, dict) and "from" in t and isinstance(r, dict) and "gasUsed" in r:
                cache[h] = {
                    "flows": flows(t),
                    "from": r["from"].lower(),
                    "gas": rpc.num(r["gasUsed"]) * rpc.num(r.get("effectiveGasPrice")),
                }
        if i % 2000 == 0:
            save("flows.json", cache)
            print(f"traced {i + len(chunk):,}/{len(need):,}", flush=True)
    save("flows.json", cache)
    return cache


def in_hype(x, usd_per_hype):
    """[HYPE, WHYPE, USDC] -> HYPE wei."""
    return x[0] + x[1] + int(x[2] * 1e12 / usd_per_hype)


def profit(sandwiches, legs, operators, prices, clock):
    """Profit = what the operator's addresses gained over both legs, in HYPE, minus both legs' gas.
    Its addresses: the wallet, its bot, the contract both legs called (its bot, or an app holding its funds),
    and its profit recipients."""
    members = operators["members"]
    recipients = set(operators["recipients"])
    for s in sandwiches:
        op = operators["of"].get(s["wallet"], s["wallet"])
        side = {s["wallet"]} | ({s["bot"]} if s["bot"] else set()) | (set(members.get(op, [])) & recipients)
        if s["to_front"] == s["to_back"]:
            side.add(s["to_front"])
        price = prices[clock.day(s["back"]["b"])]
        total = 0
        for h in (s["front"]["tx"], s["back"]["tx"]):
            leg = legs.get(h)
            if leg is None:
                total = None
                break
            total += sum(in_hype(x, price) for a, x in leg["flows"].items() if a in side)
            total -= leg["gas"] if leg["from"] in side else 0
        s["operator"], s["profit"] = op, total


PROVEN_PER_OPERATOR = 10  # proven sandwiches an operator needs before its matches count
CHANCE = 0.005  # how often a proof fires on ordinary buys in the same pools
ODDS = 1e-5  # an operator's proven count must be this unlikely by chance (about 20 operators are tested)
MAX_WALLETS = 20  # more wallets than this is a crowd of app users, not one bot
WIN_RATE = 0.9  # an operator that profits this often needs no proofs:
WIN_MATCHES = 100  # over at least this many matches; other wallets with 30+ matches profit in 81% of them at most
FAST_BLOCKS = 3  # a fast match: the buy goes through a contract that is not a public router, at most this many
FAST_RATE = 0.75  # blocks before the victim, and makes a profit; an operator needs this share of fast matches, over
FAST_MATCHES = 15  # at least this many matches (bots that buy a block or two ahead: 78-97%; others: 44% at most)


def fast(s):
    """The buy went through a contract that is not a public router, at most FAST_BLOCKS blocks before the victim,
    and profited."""
    return bool(s["bot"]) and s["buy_to_victim_blocks"] <= FAST_BLOCKS and (s["profit"] or 0) > 0


def by_chance(k, n, q=CHANCE):
    """Probability of k or more proven matches out of n if each is proven by chance with probability q."""
    if k <= 0:
        return 1.0
    log_q, log_r = math.log(q), math.log1p(-q)
    terms = [
        math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_q + (n - i) * log_r
        for i in range(k, n + 1)
    ]
    top = max(terms)
    return math.exp(top) * sum(math.exp(x - top) for x in terms)


def count(sandwiches):
    """A match counts when its operator is proven. Proofs that the bot knew the victim's transaction: the victim's buy
    waited in the HyperEVM inbox before the bot bought (`inbox`), the bot's buy was ordered just ahead of the victim by
    tip in the same block (`order`), the victim got just above its minimum (`limit`). Proofs also fire by chance on
    about CHANCE of ordinary buys, so one proven match is not enough. An operator is proven by PROVEN_PER_OPERATOR or
    more proven matches that chance would give with odds below ODDS, by a profit in WIN_RATE of at least WIN_MATCHES
    matches, or by FAST_RATE fast matches out of at least FAST_MATCHES: a bot that leaves no proofs (it pays no tip, or
    sends its own buy through the inbox) still wins almost every time, usually buying just ahead of the victim through a
    contract that is not a public router, and a trader does not. In each case it also needs at most MAX_WALLETS
    wallets, a profit over all its matches and a profit in more than half of them, which separates a bot from a trader
    that buys and sells quickly in many pools; then all its matches count."""
    matches, proven, wallets, kept = defaultdict(int), defaultdict(int), defaultdict(set), defaultdict(int)
    won, quick = defaultdict(int), defaultdict(int)
    for s in sandwiches:
        s["proofs"] = [
            p
            for p, ok in (
                ("inbox", (s.get("inbox") or {}).get("proven")),
                ("order", s["tip_signature"]),
                ("limit", s.get("limit")),
            )
            if ok
        ]
        op = s["operator"]
        matches[op] += 1
        proven[op] += bool(s["proofs"])
        wallets[op].add(s["wallet"])
        kept[op] += s["profit"] or 0
        won[op] += (s["profit"] or 0) > 0
        quick[op] += fast(s)
    ops = {
        op
        for op in matches
        if (
            (proven[op] >= PROVEN_PER_OPERATOR and by_chance(proven[op], matches[op]) < ODDS)
            or (matches[op] >= WIN_MATCHES and won[op] >= WIN_RATE * matches[op])
            or (matches[op] >= FAST_MATCHES and quick[op] >= FAST_RATE * matches[op])
        )
        and len(wallets[op]) <= MAX_WALLETS
        and kept[op] > 0
        and 2 * won[op] > matches[op]
    }
    for s in sandwiches:
        s["counted"] = s["operator"] in ops
        s["evidence"] = "+".join(s["proofs"]) or ("operator" if s["counted"] else None)


def simulate(sandwiches, tokens, prices, clock):
    """Re-run, in order and without the bot's buy, every transaction that swapped in the pool from the buy to the last
    victim, on the state just before the buy's block, and compare the victims' results with what they really got.
    The loss is the extra HYPE paid, or HYPE not received, for the same trade."""
    full = load("victim_txs.json", {})
    need = sorted({h for s in sandwiches for h in s["between"][:MAX_BETWEEN]} - set(full))
    for i in range(0, len(need), 1000):
        chunk = need[i : i + 1000]
        for h, t in zip(chunk, rpc.batch([("eth_getTransactionByHash", [h]) for h in chunk]), strict=True):
            if isinstance(t, dict) and "from" in t:
                full[h] = {"from": t["from"], "to": t["to"], "input": t["input"], "value": t["value"], "gas": t["gas"]}
        if i % 20000 == 0:
            print(f"replay tx lookups {i + len(chunk):,}/{len(need):,}", flush=True)
    save("victim_txs.json", full)
    cache = load("simulations_v2.json", {})
    key = lambda s: s["front"]["tx"] + s["back"]["tx"]  # noqa: E731
    todo = [
        s
        for s in sandwiches
        if key(s) not in cache and len(s["between"]) <= MAX_BETWEEN and all(h in full for h in s["between"])
    ]
    for i in range(0, len(todo), 10):
        chunk = todo[i : i + 10]
        res = rpc.batch(
            [
                (
                    "eth_simulateV1",
                    [
                        {"blockStateCalls": [{"calls": [full[h] for h in s["between"]]}], "validation": False},
                        hex(s["front"]["b"] - 1),
                    ],
                )
                for s in chunk
            ]
        )
        for s, r in zip(chunk, res, strict=True):
            calls = r[0]["calls"] if isinstance(r, list) else []
            at = {h: i for i, h in enumerate(s["between"])}
            out = []
            for v in s["victims"]:
                c = calls[at[v["tx"]]] if len(calls) == len(s["between"]) else {}
                ok = c.get("status") == "0x1"
                out.append(_swap_in(c.get("logs", []), s["pool"], s["front"]["venue"]) if ok else None)
            cache[key(s)] = out
        if i % 2000 == 0:
            save("simulations_v2.json", cache)
            print(f"simulated {i + len(chunk):,}/{len(todo):,}", flush=True)
    save("simulations_v2.json", cache)
    for s in sandwiches:
        s["loss"] = _loss(s, cache.get(key(s)), tokens, prices[clock.day(s["front"]["b"])])


def _swap_in(logs, pool, venue):
    """[in, out] of the first swap in `pool` among simulated logs."""
    if venue in ("bin", "lb"):  # these pools are read from transfers, as in scan.py
        moved = {"in": defaultdict(int), "out": defaultdict(int)}
        for log in logs:
            t = log.get("topics") or []
            if t and t[0] == TRANSFER and len(t) > 2:
                if "0x" + t[2][-40:] == pool:
                    moved["in"][log["address"].lower()] += rpc.num(log["data"])
                if "0x" + t[1][-40:] == pool:
                    moved["out"][log["address"].lower()] += rpc.num(log["data"])
        if len(moved["in"]) == 1 and len(moved["out"]) == 1:
            return [next(iter(moved["in"].values())), next(iter(moved["out"].values()))]
        return None
    for log in logs:
        swap = parse_swap(log) if log.get("topics") else None
        if swap and swap["pool"] == pool:
            return [swap["in"], swap["out"]]
    return None


def _loss(s, simulated, tokens, usd_per_hype):
    """Victims' loss in HYPE wei, or None when it can't be measured."""
    t = tokens.get(s["pool"])
    if not simulated or not t or len(simulated) != len(s["victims"]) or None in simulated or s["victim_hype"] <= 0:
        return None
    total = 0
    for v, (sim_in, sim_out) in zip(s["victims"], simulated, strict=True):
        if sim_in <= 0 or sim_out <= 0:
            return None
        t_in, t_out = (t[0], t[1]) if v["z"] else (t[1], t[0])
        paid_more = v["in"] - v["out"] * sim_in / sim_out  # in t_in units, for the same output
        got_less = v["in"] * sim_out / sim_in - v["out"]  # in t_out units, for the same input
        if is_hype(t_in):
            total += paid_more
        elif is_hype(t_out):
            total += got_less
        elif t_in == USDC:
            total += paid_more * 1e12 / usd_per_hype
        elif t_out == USDC:
            total += got_less * 1e12 / usd_per_hype
        else:
            return None
    # a re-run that picked another leg of a multi-hop trade gives nonsense; treat as unmeasured
    return total if -0.05 * s["victim_hype"] <= total <= s["victim_hype"] else None
