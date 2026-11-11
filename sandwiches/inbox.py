"""Prove that a bot saw its victim's transaction in Elysium's inbox on HyperEVM before it bought.

A transaction reaches Elysium either through the sequencer or as a signed message to Elysium's inbox contract on
HyperEVM, which the sequencer executes seconds later. The inbox is public. The Elysium block that executed a message
raised its header nonce (delayed messages read so far), so block b executed messages nonce(b-1) .. nonce(b)-1. Each
message's MessageDelivered log on Elysium's bridge carries its HyperEVM time. A sandwich is proven when its first
victim came through the inbox, the bot's buy came through the sequencer, and the buy's block is no earlier than the
victim's message (1-second timestamps; with several messages in the block, the latest one).

HyperEVM endpoint from HYPEREVM_RPC_URL (an endpoint with full log history). URLs never appear in output.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from .chain import load, save
from .detect import headers

URL = os.environ.get("HYPEREVM_RPC_URL")
GAP = 0.05  # seconds between requests
BRIDGE = "0x503d572157113faaad4dbed2647bb10b949216b6"  # Conduit chaininfo, elysium-mainnet (HyperEVM)
DELIVERED = "0x5e3c1311ea442664e8b1611bfabef659120ea7a0a2cfc0667700bebc69cbffe1"  # MessageDelivered(...)
LOOKBACK = 3000  # HyperEVM blocks searched before the parent block the Elysium block recorded
SPAN = 930  # blocks searched back per pass; with GROUP_SPREAD and +5 a query stays under 1,000 (common RPC cap)
GROUP_SIZE = 50  # message indices per query
GROUP_SPREAD = 60  # parent blocks of one query's indices lie this close together
_last = [0.0]


def _call(method, params):
    if not URL or not URL.startswith(("http://", "https://")):
        sys.exit("Set HYPEREVM_RPC_URL to an http(s) HyperEVM endpoint with full log history.")
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(URL, body, {"content-type": "application/json", "user-agent": "Mozilla/5.0"})
    err = None
    for attempt in range(8):
        wait = GAP - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                res = json.load(r)
            if "result" in res:
                return res["result"]
            err = str(res.get("error"))[:200]
            if "-32602" in err or "-32000" in err:  # bad params or a server-side timeout: retrying won't help
                break
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            err = type(e).__name__
        time.sleep(min(60, 5 * (attempt + 1)))
    raise RuntimeError(f"HyperEVM {method}: {err}")


def messages(wanted):
    """{index: {ts, kind, block, tx}} for delayed messages {index: parent block the executing Elysium block recorded},
    and the set of indices that could not be searched. The public RPC allows 1,000 blocks per query, so indices with
    nearby parent blocks share one query (their topics OR-ed), searched in steps of SPAN blocks back from the parent
    block. A query that still fails gives its indices up rather than retrying further back."""
    found, failed = {}, set()
    todo = sorted(wanted.items(), key=lambda kv: kv[1])
    for back in range(0, LOOKBACK, SPAN):
        groups, cur = [], []
        for idx, parent in todo:
            if cur and (parent - cur[0][1] > GROUP_SPREAD or len(cur) >= GROUP_SIZE):
                groups.append(cur)
                cur = []
            cur.append((idx, parent))
        if cur:
            groups.append(cur)
        for g in groups:
            lo = max(0, g[0][1] - back - SPAN + 1)
            hi = g[-1][1] - back + (5 if back == 0 else 0)
            query = {
                "address": BRIDGE,
                "topics": [DELIVERED, ["0x" + format(i, "064x") for i, _ in g]],
                "fromBlock": hex(lo),
                "toBlock": hex(hi),
            }
            try:
                logs = _call("eth_getLogs", [query])
            except RuntimeError:
                failed |= {i for i, _ in g}
                continue
            for lg in logs:
                w = lg["data"][2:]
                found[int(lg["topics"][1], 16)] = {
                    "ts": int(w[320:384], 16),
                    "kind": int(w[64:128], 16),
                    "block": int(lg["blockNumber"], 16),
                    "tx": lg["transactionHash"],
                }
        todo = [(i, p) for i, p in todo if i not in found and i not in failed]
        print(
            f"inbox messages: {len(found):,} found, {len(failed):,} could not be searched, {len(todo):,} left "
            f"after {back + SPAN:,} blocks back",
            flush=True,
        )
        if not todo:
            break
    return found, failed


def prove(sandwiches):
    """Set s["inbox"] = {proven, lead, message} for sandwiches whose first victim came through the inbox."""
    hdr = headers(b for s in sandwiches for b in (s["front"]["b"], s["front"]["b"] - 1))
    todo = [s for s in sandwiches if s["victims"][0].get("path") == "inbox"]
    print(f"inbox: {len(todo):,} sandwiches with an inbox victim", flush=True)
    cache = load("inbox.json", {})
    wanted = {}
    for s in todo:
        b = s["victims"][0]["b"]
        for idx in range(hdr[str(b - 1)][1], hdr[str(b)][1]):
            if str(idx) not in cache:
                wanted[idx] = hdr[str(b)][2]
    found, failed = messages(wanted)
    for idx, m in found.items():
        cache[str(idx)] = m
    for idx in wanted:
        if idx not in failed:
            cache.setdefault(str(idx), None)  # searched, not found
    save("inbox.json", cache)
    for s in todo:
        b = s["victims"][0]["b"]
        msgs = [cache.get(str(i)) for i in range(hdr[str(b - 1)][1], hdr[str(b)][1])]
        front_seq = hdr[str(s["front"]["b"])][1] == hdr[str(s["front"]["b"] - 1)][1]
        if any(str(i) not in cache for i in range(hdr[str(b - 1)][1], hdr[str(b)][1])):
            s["inbox"] = {"proven": False, "lead": None, "unknown": True}
            continue
        if not msgs or None in msgs or not front_seq:
            s["inbox"] = {"proven": False, "lead": None}
            continue
        lead = hdr[str(s["front"]["b"])][0] - max(m["ts"] for m in msgs)
        s["inbox"] = {"proven": lead >= 0, "lead": lead, "message": msgs[-1]["tx"]}
