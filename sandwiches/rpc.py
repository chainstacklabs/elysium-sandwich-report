"""JSON-RPC client for an Elysium archive node with debug_* methods.

The endpoint comes from ELYSIUM_RPC_URL and never appears in output or errors.
"""
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

URL = os.environ.get("ELYSIUM_RPC_URL")
BATCH = 50
LOG_RANGE = 10_000  # most providers cap eth_getLogs at 10k blocks


def _post(body):
    if not URL:
        sys.exit("Set ELYSIUM_RPC_URL to an Elysium mainnet archive endpoint.")
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"content-type": "application/json"})
    for attempt in range(8):
        try:
            return json.load(urllib.request.urlopen(req, timeout=120))
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(f"RPC HTTP {e.code}") from None
        except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError):
            pass
        time.sleep(min(60, 2 ** attempt))  # back off on rate limits and transient errors
    raise RuntimeError("RPC unreachable after retries")


def batch(calls):
    """[(method, params)] -> results in order; a failed call returns {'error': message}."""
    out = []
    for i in range(0, len(calls), BATCH):
        chunk = calls[i:i + BATCH]
        res = _post([{"jsonrpc": "2.0", "id": j, "method": m, "params": p} for j, (m, p) in enumerate(chunk)])
        if isinstance(res, dict):
            raise RuntimeError(f"RPC batch error: {str(res.get('error'))[:200]}")
        res = sorted(res, key=lambda r: r["id"])
        out += [r["result"] if "error" not in r else {"error": str(r["error"].get("message"))[:200]} for r in res]
    return out


def call(method, params):
    r = batch([(method, params)])[0]
    if isinstance(r, dict) and "error" in r:
        raise RuntimeError(f"{method}: {r['error']}")
    return r


def logs(start, end, topics):
    return call("eth_getLogs", [{"fromBlock": hex(start), "toBlock": hex(end), "topics": topics}])


def head():
    return int(call("eth_blockNumber", []), 16)


def num(x):
    """Hex quantity -> int; empty or failed results count as 0."""
    return int(x, 16) if isinstance(x, str) and x not in ("0x", "") else 0
