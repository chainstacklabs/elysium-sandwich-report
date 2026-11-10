"""Group wallets and contracts into operators.

Joined: a wallet and the bot contract it calls for its legs; bots whose bytecode is identical once every embedded
address is zeroed (same code, different hard-coded owner); a wallet and the address that repeatedly receives most
of its sell proceeds (its profit recipient). Each operator is named by its first wallet.
"""

import hashlib
from collections import Counter, defaultdict

from . import rpc
from .chain import NATIVE, USDC, WHYPE, load, save
from .money import in_hype


def code_family(code):
    """Hash of the runtime code with every PUSH20 operand zeroed."""
    b = bytearray(bytes.fromhex(code[2:]))
    i = 0
    while i < len(b):
        if 0x60 <= b[i] <= 0x7F:
            n = b[i] - 0x5F
            if n == 20:
                b[i + 1 : i + 21] = bytes(20)
            i += n
        i += 1
    return hashlib.sha256(bytes(b)).hexdigest()


def _components(edges):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(a)] = find(b)
    groups = defaultdict(set)
    for x in list(parent):
        groups[find(x)].add(x)
    return groups.values()


def _group(sandwiches, extra_edges):
    edges = [(s["wallet"], s["bot"]) for s in sandwiches if s["bot"]] + extra_edges
    codes = load("codes.json", {})
    bots = sorted({b for _, b in edges if b not in codes})
    for b, code in zip(bots, rpc.batch([("eth_getCode", [b, "latest"]) for b in bots]), strict=True):
        codes[b] = code_family(code) if isinstance(code, str) and len(code) > 400 else None
    save("codes.json", codes)
    families = defaultdict(list)
    for b in {b for _, b in edges}:
        if codes.get(b):
            families[codes[b]].append(b)
    edges += [(members[0], m) for members in families.values() for m in members[1:]]
    edges += [(s["wallet"], s["wallet"]) for s in sandwiches]
    wallets = [s["wallet"] for s in sorted(sandwiches, key=lambda s: s["front"]["b"])]
    of, members = {}, {}
    for comp in _components(edges):
        name = next(w for w in wallets if w in comp)
        members[name] = sorted(comp)
        of.update({m: name for m in comp})
    return of, members


def _recipients(sandwiches, legs, of, prices, clock, infra):
    """Per operator, the address outside market infrastructure that gains most in its sell legs, if that is
    the same address in at least a quarter of them (and at least 3)."""
    top = defaultdict(Counter)
    count = Counter()
    for s in sandwiches:
        op = of.get(s["wallet"], s["wallet"])
        count[op] += 1
        leg = legs.get(s["back"]["tx"])
        if not leg:
            continue
        price = prices[clock.day(s["back"]["b"])]
        gains = {
            a: in_hype(x, price)
            for a, x in leg["flows"].items()
            if a not in infra and a not in (s["wallet"], s["bot"], s["to_back"])
        }
        gains = {a: g for a, g in gains.items() if g > 0}
        if gains:
            top[op][max(gains, key=gains.get)] += 1
    return [(op, a) for op, c in top.items() for a, n in c.items() if n >= max(3, count[op] // 4)]


def run(sandwiches, legs, prices, clock):
    routers = set(load("routers.json", []))
    pool_managers = {s["pool"].split(":")[0] for s in sandwiches if ":" in s["pool"]}
    pools = {s["pool"] for s in sandwiches if ":" not in s["pool"]}
    hooks = set(load("hooks.json", []))
    infra = routers | pool_managers | pools | hooks | {WHYPE, USDC, NATIVE}
    of, _ = _group(sandwiches, [])
    recipient_edges = _recipients(sandwiches, legs, of, prices, clock, infra)
    of, members = _group(sandwiches, recipient_edges)
    ops = {"of": of, "members": members, "recipients": sorted({a for _, a in recipient_edges})}
    save("operators.json", ops)
    return ops
