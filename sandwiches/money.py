"""What each sandwich moved: the bot's profit from traces, the victim's loss from simulation."""
from collections import defaultdict

from . import rpc
from .chain import TRANSFER, USDC, WETH_DEPOSIT, WETH_WITHDRAWAL, WHYPE, is_hype, load, parse_swap, save


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
        chunk = need[i:i + 20]
        traces = rpc.batch([("debug_traceTransaction", [h, {"tracer": "callTracer", "tracerConfig": {"withLog": True}}])
                            for h in chunk])
        receipts = rpc.batch([("eth_getTransactionReceipt", [h]) for h in chunk])
        for h, t, r in zip(chunk, traces, receipts):
            if isinstance(t, dict) and "from" in t and isinstance(r, dict) and "gasUsed" in r:
                cache[h] = {"flows": flows(t), "from": r["from"].lower(),
                            "gas": rpc.num(r["gasUsed"]) * rpc.num(r.get("effectiveGasPrice"))}
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
    and its profit recipients. Next-block cases (tier C) count only for operators that profit from them overall."""
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
    by_op = defaultdict(lambda: [0, 0])
    for s in sandwiches:
        if s["tier"] == "C":
            by_op[s["operator"]][0] += 1
            by_op[s["operator"]][1] += s["profit"] or 0
    for s in sandwiches:
        n, total = by_op[s["operator"]]
        s["counted"] = s["tier"] != "C" or (n >= 20 and total > 0)


def simulate(sandwiches, tokens, prices, clock):
    """Re-run each sandwich's victim transactions, in order, on the state just before the bot's buy, and compare
    with what the victims really got. The loss is the extra HYPE paid, or HYPE not received, for the same trade."""
    full = load("victim_txs.json", {})
    need = sorted({v["tx"] for s in sandwiches for v in s["victims"]} - set(full))
    for i in range(0, len(need), 1000):
        chunk = need[i:i + 1000]
        for h, t in zip(chunk, rpc.batch([("eth_getTransactionByHash", [h]) for h in chunk])):
            if isinstance(t, dict) and "from" in t:
                full[h] = {"from": t["from"], "to": t["to"], "input": t["input"], "value": t["value"], "gas": t["gas"]}
    save("victim_txs.json", full)
    cache = load("simulations.json", {})
    todo = [s for s in sandwiches if s["front"]["tx"] + s["back"]["tx"] not in cache and all(v["tx"] in full for v in s["victims"])]
    for i in range(0, len(todo), 10):
        chunk = todo[i:i + 10]
        res = rpc.batch([("eth_simulateV1", [{"blockStateCalls": [{"calls": [full[v["tx"]] for v in s["victims"]]}],
                                              "validation": False}, hex(s["front"]["b"] - 1)]) for s in chunk])
        for s, r in zip(chunk, res):
            calls = r[0]["calls"] if isinstance(r, list) else []
            cache[s["front"]["tx"] + s["back"]["tx"]] = [
                _swap_in(c.get("logs", []), s["pool"], s["front"]["venue"]) if c.get("status") == "0x1" else None
                for c in calls]
        if i % 2000 == 0:
            save("simulations.json", cache)
            print(f"simulated {i + len(chunk):,}/{len(todo):,}", flush=True)
    save("simulations.json", cache)
    for s in sandwiches:
        s["loss"] = _loss(s, cache.get(s["front"]["tx"] + s["back"]["tx"]), tokens, prices[clock.day(s["front"]["b"])])


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
    for v, (sim_in, sim_out) in zip(s["victims"], simulated):
        if sim_in <= 0 or sim_out <= 0:
            return None
        t_in, t_out = (t[0], t[1]) if v["z"] else (t[1], t[0])
        paid_more = v["in"] - v["out"] * sim_in / sim_out    # in t_in units, for the same output
        got_less = v["in"] * sim_out / sim_in - v["out"]     # in t_out units, for the same input
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
