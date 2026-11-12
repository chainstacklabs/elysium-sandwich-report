"""Find victims' transactions that were also posted to Elysium's inbox on HyperEVM.

A victim on the sequencer route had its buy executed in a sequencer block. The same signed transaction can also
appear in Elysium's inbox on HyperEVM, posted by any wallet: an L2 message (kind 3) whose payload is byte 0x04
followed by the raw signed transaction, so keccak-256 of the payload after that byte is the Elysium transaction hash.
A batch message (first byte 0x03) holds sub-messages, each an 8-byte length and a payload; those are opened too.

The step reads every InboxMessageDelivered log of the inbox over the HyperEVM blocks that the sandwiches' Elysium
blocks recorded as their parent blocks, with a margin, and keeps only messages whose hash is a victim's. For each
match it then reads the bridge's MessageDelivered log: the message's kind, its sender (aliased: the posting wallet
plus ALIAS) and its HyperEVM time.

cache/copies.json keeps the scanned block ranges and the matches, so a re-run fetches only new ranges. HyperEVM
endpoint from HYPEREVM_RPC_URL (full log history); URLs never appear in output.
"""

import gc
import hashlib
import re
import time

from . import inbox
from .chain import CACHE, load, save
from .detect import headers

INBOX = "0xce59f8b65ad41c9dae4c07a0571a39824bd92cff"  # Conduit chaininfo, elysium-mainnet (HyperEVM)
INBOX_DELIVERED = "0xff64905f73a67fb594e0f940a8075a860db489ad991e032f48c81123eb52d60b"  # InboxMessageDelivered
FROM_ORIGIN = "0xab532385be8f1005a4b6ba8fa20a2245facb346134ac739fe9a5198dc1580b9c"  # ...FromOrigin: payload in calldata
ALIAS = 0x1111000000000000000000000000000000001111  # Arbitrum's L1-to-L2 address alias offset
L2_MESSAGE = 3  # bridge message kind of a signed transaction or a batch of them
CHUNK = 2000  # HyperEVM blocks per eth_getLogs query (about 10,000 logs, 15 MB); halved on errors
MARGIN = 1000  # HyperEVM blocks scanned before the first and after the last victim's parent block (about 1 s each)
GAP = 0.2  # seconds between requests: at most 5 a second


# ---------------------------------------------------------------- keccak-256
def _keccak_py(data):
    """Pure-Python keccak-256, used only where hashlib lacks it (OpenSSL older than 3.2). Slow but exact."""
    rc = [
        0x1, 0x8082, 0x800000000000808A, 0x8000000080008000, 0x808B, 0x80000001, 0x8000000080008081,
        0x8000000000008009, 0x8A, 0x88, 0x80008009, 0x8000000A, 0x8000808B, 0x800000000000008B, 0x8000000000008089,
        0x8000000000008003, 0x8000000000008002, 0x8000000000000080, 0x800A, 0x800000008000000A, 0x8000000080008081,
        0x8000000000008080, 0x80000001, 0x8000000080008008,
    ]  # fmt: skip
    rot = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
    mask, rate = (1 << 64) - 1, 136
    p = bytearray(data) + b"\x01"
    p += bytes(-len(p) % rate)
    p[-1] |= 0x80
    a = [[0] * 5 for _ in range(5)]
    for i in range(0, len(p), rate):
        for j in range(rate // 8):
            a[j % 5][j // 5] ^= int.from_bytes(p[i + 8 * j : i + 8 * j + 8], "little")
        for r in rc:
            c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
            d = [c[(x - 1) % 5] ^ (((c[(x + 1) % 5] << 1) | (c[(x + 1) % 5] >> 63)) & mask) for x in range(5)]
            a = [[a[x][y] ^ d[x] for y in range(5)] for x in range(5)]
            b = [[0] * 5 for _ in range(5)]
            for x in range(5):
                for y in range(5):
                    v, s = a[x][y], rot[x][y]
                    b[y][(2 * x + 3 * y) % 5] = ((v << s) | (v >> (64 - s))) & mask if s else v
            a = [[b[x][y] ^ (~b[(x + 1) % 5][y] & b[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
            a[0][0] ^= r
    return b"".join(a[j % 5][j // 5].to_bytes(8, "little") for j in range(4))


try:
    hashlib.new("keccak-256", b"")

    def keccak(data):
        return hashlib.new("keccak-256", data).digest()

except ValueError:
    keccak = _keccak_py


# ---------------------------------------------------------------- messages
def signed_txs(payload, depth=0):
    """Raw signed transactions in an L2 message payload: 0x04 + one transaction, or 0x03 + length-prefixed
    sub-messages (a batch)."""
    if payload[:1] == b"\x04":
        yield payload[1:], depth > 0
    elif payload[:1] == b"\x03" and depth < 2:
        i = 1
        while i + 8 <= len(payload):
            size = int.from_bytes(payload[i : i + 8], "big")
            yield from signed_txs(payload[i + 8 : i + 8 + size], depth + 1)
            i += 8 + size


def dealias(a):
    return "0x%040x" % ((int(a, 16) - ALIAS) % (1 << 160))


_calls = [0]


def _logs(query):
    time.sleep(GAP)
    _calls[0] += 1
    return inbox._call("eth_getLogs", [query])


def _scan(lo, hi, targets, stats):
    """Matches [(hash, message index, HyperEVM block, HyperEVM tx, in a batch)] in InboxMessageDelivered logs of
    blocks lo..hi. A failing query is split in two until it passes."""
    try:
        logs = _logs(
            {"address": INBOX, "topics": [[INBOX_DELIVERED, FROM_ORIGIN]], "fromBlock": hex(lo), "toBlock": hex(hi)}
        )
    except RuntimeError:
        if lo == hi:
            raise
        mid = (lo + hi) // 2
        print(f"copies: blocks {lo:,}-{hi:,} failed, splitting", flush=True)
        return _scan(lo, mid, targets, stats) + _scan(mid + 1, hi, targets, stats)
    found = []
    for lg in logs:
        stats["messages"] += 1
        if lg["topics"][0] == FROM_ORIGIN:
            stats["origin"] += 1
            continue
        raw = bytes.fromhex(lg["data"][2:])
        payload = raw[64 : 64 + int.from_bytes(raw[32:64], "big")]
        for signed, batched in signed_txs(payload):
            stats["batched" if batched else "signed"] += 1
            h = "0x" + keccak(signed).hex()
            if h in targets:
                idx = int(lg["topics"][1], 16)
                found.append((h, idx, int(lg["blockNumber"], 16), lg["transactionHash"], batched))
    return found


def _bridge(found, cache):
    """Kind, sender and time of each matched message, from the bridge's MessageDelivered logs."""
    want = sorted({idx for _, idx, *_ in found if str(idx) not in cache["messages"]})
    for i in range(0, len(want), 50):
        group = want[i : i + 50]
        blocks = [b for _, idx, b, *_ in found if idx in group]
        for lg in _logs(
            {
                "address": inbox.BRIDGE,
                "topics": [inbox.DELIVERED, ["0x" + format(idx, "064x") for idx in group]],
                "fromBlock": hex(min(blocks)),
                "toBlock": hex(max(blocks)),
            }
        ):
            w = lg["data"][2:]
            cache["messages"][str(int(lg["topics"][1], 16))] = {
                "kind": int(w[64:128], 16),
                "sender": dealias("0x" + w[152:192]),
                "ts": int(w[320:384], 16),
            }


def _senders(hashes):
    """Sender of each transaction, read from cache/txs.json as a stream: the file is too large to load here."""
    pat = re.compile(r'"(0x[0-9a-f]{64})": \{"from": "(0x[0-9a-f]{40})"')
    out, tail = {}, ""
    with open(f"{CACHE}/txs.json") as f:
        while block := f.read(1 << 23):
            text = tail + block
            for m in pat.finditer(text):
                if m.group(1) in hashes:
                    out[m.group(1)] = m.group(2)
            tail = text[-200:]
    return out


def _covered(done, lo, hi):
    """True when the ranges in done cover lo..hi."""
    at = lo
    for a, b, *_ in sorted(done, key=lambda c: c[0]):
        if a <= at <= b:
            at = b + 1
        if at > hi:
            return True
    return at > hi


def run():
    found = load("sandwiches.json")
    targets = {}  # victim tx -> block, for every sequencer-route victim of a counted sandwich
    for s in found:
        if s["counted"]:
            for v in s["victims"]:
                if v.get("path") == "sequencer":
                    targets[v["tx"]] = v["b"]
    del found
    gc.collect()
    hdr = headers(targets.values())
    parent = {h: hdr[str(b)][2] for h, b in targets.items()}
    cache = load("copies.json", {"victims": [], "done": [], "matches": {}, "messages": {}})
    new = set(targets) - set(cache["victims"])
    if new and cache["done"]:  # ranges scanned before these victims existed must be read again for them
        lo, hi = min(parent[h] for h in new) - MARGIN, max(parent[h] for h in new) + MARGIN
        cache["done"] = [c for c in cache["done"] if c[1] < lo or c[0] > hi]
    cache["victims"] = sorted(set(cache["victims"]) | set(targets))
    lo, hi = min(parent.values()) - MARGIN, max(parent.values()) + MARGIN
    chunks = [
        (max(a, lo), min(a + CHUNK - 1, hi))
        for a in range(lo - lo % CHUNK, hi + 1, CHUNK)
        if not _covered(cache["done"], max(a, lo), min(a + CHUNK - 1, hi))
    ]
    print(
        f"copies: {len(targets):,} victims, HyperEVM blocks {lo:,}-{hi:,}, {len(chunks):,} chunks to scan", flush=True
    )
    calls0, t0 = _calls[0], time.time()
    for i, (a, b) in enumerate(chunks):
        stats = {"messages": 0, "origin": 0, "signed": 0, "batched": 0}
        got = _scan(a, b, targets, stats)
        if got:
            _bridge(got, cache)
        for h, idx, blk, txh, batched in got:
            ms = cache["matches"].setdefault(h, [])
            if all(m[0] != idx for m in ms):
                ms.append([idx, blk, txh, batched])
        cache["done"].append([a, b, stats])
        save("copies.json", cache)
        if i % 10 == 0 or i == len(chunks) - 1:
            hits = sum(1 for h in cache["matches"] if h in targets)
            print(
                f"copies: {i + 1:,}/{len(chunks):,} chunks, {hits:,} victims found so far, "
                f"{_calls[0] - calls0:,} calls, {time.time() - t0:,.0f} s",
                flush=True,
            )
    _summarize(cache, targets, hdr)
    save("copies.json", cache)


def _summarize(cache, targets, hdr):
    """Per victim: its earliest copy (message index, HyperEVM tx, poster, seconds from the victim's block time,
    whether the victim posted it itself, whether it sat in a batch)."""
    hits = {h: ms for h, ms in cache["matches"].items() if h in targets}
    senders = _senders(set(hits))
    out = {}
    for h, ms in hits.items():
        ms = [m for m in ms if (cache["messages"].get(str(m[0])) or {}).get("kind") == L2_MESSAGE]
        if not ms:
            continue
        idx, _, txh, batched = min(ms)
        msg = cache["messages"][str(idx)]
        out[h] = {
            "index": idx,
            "tx": txh,
            "poster": msg["sender"],
            "seconds": msg["ts"] - hdr[str(targets[h])][0],
            "self": msg["sender"] == senders.get(h),
            "batch": batched,
            "copies": len(ms),
        }
    totals = {k: sum(c[2][k] for c in cache["done"]) for k in ("messages", "origin", "signed", "batched")}
    cache["result"] = {"checked": sorted(targets), "copies": out, "totals": totals}
    print(
        f"copies: {len(out):,} of {len(targets):,} sequencer-route victims also posted to the inbox; scanned "
        f"{totals['messages']:,} messages, {totals['signed']:,} signed and {totals['batched']:,} batched transactions, "
        f"{totals['origin']:,} sent from origin (payload not in the log, not decoded)",
        flush=True,
    )
