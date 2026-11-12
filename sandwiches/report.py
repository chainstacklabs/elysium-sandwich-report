"""Build docs/index.html (the report) and data/sandwiches.csv from the cache."""

import csv
import datetime as dt
import os
import re
import statistics
from collections import Counter, defaultdict
from html import escape

from . import rpc
from .chain import APPS, NATIVE, PUBLIC_LAUNCH, USDC, WHYPE, load, save
from .charts import bars, stacked_area
from .clock import Clock
from .detect import WINDOW
from .money import CHANCE, MAX_WALLETS, ODDS, PROVEN_PER_OPERATOR, WIN_MATCHES, WIN_RATE

EXPLORER = "https://elysium.kinetiq.xyz/"
EXAMPLES = [  # bot buys of the walkthroughs: one in the private mainnet, one after the public launch
    ("Private mainnet", "0xd1933d28ca7c8d1760a3365b5a1389f8125ec390e4d41994b295e4798e056292"),
    ("Public mainnet", "0x613d9e38a89bbce0b88f716c77899809244a5295593050ca47256ea4f8837005"),
]
NAMED = 5  # operators shown by letter and color
COLORS = ["var(--o1)", "var(--o2)", "var(--o3)", "var(--o4)", "var(--o5)"]
OTHER = "var(--o0)"
LOSS_BUCKETS = [(i, i + 1) for i in range(10)] + [(10, None)]  # equal 1-point bins, then the tail
VENUE = {"v4": "Uniswap V4", "v2v3": "Uniswap V2/V3", "bin": "Bin AMM", "lb": "Liquidity Book"}
HYPEREVM_EXPLORER = "https://hyperevmscan.io/"
VENUE_SHORT = {"v4": "V4", "v2v3": "V2/V3", "bin": "Bin AMM", "lb": "LB"}


# ---------------------------------------------------------------- formatting
def usd(v):
    """Dollars in text: $153K, $6.2K, $699."""
    if abs(v) < 0.5:
        return "$0"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.1f}M"
    if abs(v) >= 1e4:
        return f"${v / 1e3:.0f}K"
    return f"${v / 1e3:.1f}K" if abs(v) >= 1e3 else f"${v:.0f}"


def usd_axis(v):
    return f"${v / 1e3:g}K" if v >= 1e3 else f"${v:g}"


def dollars(v):
    """Dollars in a table column; the header carries the unit."""
    return f"{v:,.0f}"


def hype(v):
    return f"{v:,.0f}" if abs(v) >= 100 else (f"{v:,.1f}" if abs(v) >= 1 else f"{v:,.3f}")


def n(v):
    return f"{v:,.0f}"


def pct(v):
    return f"{v:.1f}%" if v < 10 else f"{v:.0f}%"


def approx(x):
    """Round a rate for prose: 144 -> 140, 21 -> 20, 4.3 -> 4."""
    return int(round(x, -1)) if x >= 15 else round(x)


def whole_pct(v):
    return "0%" if v == 0 else ("<1%" if v < 1 else f"{v:.0f}%")


def when(t, fmt="%b %-d, %H:%M"):
    return dt.datetime.fromtimestamp(t, dt.UTC).strftime(fmt)


def at(t):
    """A moment in prose: Nov 9 at 13:00, never split across lines."""
    return when(t, "%b %-d at %H:%M").replace(" ", "\u00a0")


def stamp(t):
    """A moment in a list or table: date and time in their own aligned slots."""
    return (
        f'<span class="ts"><span class="d">{when(t, "%b %-d")}</span><span class="t">{when(t, "%H:%M")}</span></span>'
    )


def hour_start(key):
    return dt.datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=dt.UTC).timestamp()


def day(key):
    return dt.date.fromisoformat(key).strftime("%b %-d")


def short(a):
    return a[:6] + "…" + a[-4:]


def link(kind, value, text, cls=""):
    href = f"{EXPLORER}{kind}/{value}"
    return f'<a class="{cls}" href="{href}" target="_blank" rel="noopener" title="{value}">{escape(text)}</a>'


def addr(a, text=None):
    return link("address", a, text or short(a), "addr")


def tx(h, text):
    return link("tx", h, text, "addr" if text.startswith("0x") else "")


def block(b, prefix="Block"):
    return link("block", b, f"{prefix}\u00a0{b:,}")


def legs(s):
    return " · ".join((tx(s["front"]["tx"], "buy"), tx(s["victims"][0]["tx"], "victim"), tx(s["back"]["tx"], "sell")))


def swatch(color):
    return f'<span class="sw" style="background:{color}"></span>'


def tie(page):
    """Keep the last two words of each paragraph, list item and heading on one line."""
    end = r"(?=</p>|</dd>|</h[123]>|</span></li>|</li>)"
    return re.sub(r"\s([^\s<>]{1,12})\s([^\s<>]{1,14})" + end, "\u00a0\\1\u00a0\\2", page)


def figure(title, dek, body, legend=""):
    return f'<figure><h3>{title}</h3><p class="dek">{dek}</p>{legend}<div class="wrap">{body}</div></figure>'


def legend(items):
    return '<div class="legend">' + "".join(f"<span>{swatch(c)}{escape(t)}</span>" for t, c in items) + "</div>"


def table(head, rows, cls=""):
    """head: [(title, 'l' | 'r' | 'bar')]."""
    th = "".join(f'<th class="{a}">{h}</th>' for h, a in head)
    body = "".join(
        "<tr>" + "".join(f'<td class="{a}">{c}</td>' for c, (_, a) in zip(r, head, strict=True)) + "</tr>" for r in rows
    )
    return f'<div class="wrap"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def bar(v, top, color="var(--bar)"):
    return f'<i style="width:{100 * max(v, 0) / top if top else 0:.1f}%;background:{color}"></i>'


# ---------------------------------------------------------------- data
def symbols(token_addrs):
    cache = load("symbols.json", {NATIVE: "HYPE"})
    need = sorted(set(token_addrs) - set(cache))
    if need:
        for a, r in zip(
            need, rpc.batch([("eth_call", [{"to": a, "data": "0x95d89b41"}, "latest"]) for a in need]), strict=True
        ):
            cache[a] = _decode_string(r) or short(a)
        save("symbols.json", cache)
    return cache


def _decode_string(r):
    if not isinstance(r, str) or len(r) < 66:
        return None
    h = r[2:]
    try:
        if len(h) >= 128 and int(h[:64], 16) == 32:
            return bytes.fromhex(h[128 : 128 + 2 * int(h[64:128], 16)]).decode("utf-8", "replace")[:24]
        return bytes.fromhex(h[:64]).rstrip(b"\0").decode("utf-8", "replace")[:24]
    except ValueError:
        return None


class Data:
    def __init__(self):
        self.clock = Clock()
        self.head = load("head.json")
        self.prices = load("prices.json")
        self.tokens = load("pool_tokens.json")
        self.txs = load("txs.json")
        self.ops = load("operators.json")
        self.volume = load("volume.json")
        self.venues = load("venues.json", {})
        rows = load("sandwiches.json")
        self.all = rows
        self.rows = [s for s in rows if s["counted"]]
        for s in self.rows:
            s["t"] = self.clock.time(s["front"]["b"])
            s["day"] = self.clock.day(s["front"]["b"])
            price = self.prices[s["day"]]
            s["loss_usd"] = (s["loss"] or 0) / 1e18 * price
            s["profit_usd"] = (s["profit"] or 0) / 1e18 * price
            s["volume_usd"] = s["victim_hype"] / 1e18 * price
        self.symbols = symbols({t for s in self.rows for t in self.tokens.get(s["pool"], [])})

    def pool_label(self, pool):
        t = self.tokens.get(pool)
        if not t:
            return short(pool.split(":")[-1])
        a, b = (self.symbols.get(x, short(x)) for x in t)
        return f"{b} / {a}" if a in ("HYPE", "WHYPE") else f"{a} / {b}"

    @staticmethod
    def venue(s):
        return VENUE[s["front"]["venue"]]


# ---------------------------------------------------------------- page
def build():
    d = Data()
    rows, clock = d.rows, d.clock
    measured = [s for s in rows if s["loss"] is not None]
    unmeasured = [s for s in rows if s["loss"] is None]
    no_price = [s for s in unmeasured if not any(t in (NATIVE, WHYPE, USDC) for t in d.tokens.get(s["pool"], []))]
    loss = sum(s["loss_usd"] for s in rows)
    loss_hype = sum(s["loss"] or 0 for s in rows) / 1e18
    profit = sum(s["profit_usd"] for s in rows)
    profit_hype = sum(s["profit"] or 0 for s in rows) / 1e18
    volume = sum(s["volume_usd"] for s in rows)
    victims = {d.txs[v["tx"]]["from"] for s in rows for v in s["victims"]}
    victim_trades = sum(len(s["victims"]) for s in rows)
    loss_pcts = [100 * s["loss"] / s["victim_hype"] for s in measured]
    median_pct = statistics.median(loss_pcts)
    after = [s for s in rows if s["t"] >= PUBLIC_LAUNCH]
    after_loss = sum(s["loss_usd"] for s in after)
    first = min(s["t"] for s in rows)
    unmeasured_pct = pct(100 * len(unmeasured) / len(rows))
    kept_pct = pct(100 * profit / loss)

    # operators, ranked by the losses they caused
    op = defaultdict(lambda: defaultdict(float))
    op_rows = defaultdict(list)
    for s in rows:
        o = s["operator"]
        op_rows[o].append(s)
        for k, v in (("n", 1), ("same", s["tier"] != "C"), ("loss", s["loss_usd"]), ("profit", s["profit_usd"])):
            op[o][k] += v
    ops = sorted(op, key=lambda o: -op[o]["loss"])
    letter = {o: "ABCDE"[i] for i, o in enumerate(ops[:NAMED])}
    name = {o: f"Operator {letter[o]}" for o in letter}
    color = {o: COLORS[i] for i, o in enumerate(ops[:NAMED])}
    for o in ops:
        rs = op_rows[o]
        op[o]["first"] = min(s["t"] for s in rs)
        op[o]["last"] = max(clock.time(s["back"]["b"]) for s in rs)
        same = [s["t"] for s in rs if s["tier"] != "C"]
        op[o]["first_same"] = min(same) if same else None
    A = ops[0]
    a_same = op[A]["first_same"]
    a_share = pct(100 * op[A]["loss"] / loss)

    def opref(o):
        return f'<a href="#op-{letter[o]}">{name[o]}</a>'

    def op_label(o):
        return f'<span id="op-{letter[o]}"></span>{swatch(color[o])}{name[o]}' if o in name else addr(o)

    # apps the victims traded through
    apps = defaultdict(lambda: [0.0, 0, set()])
    for s in rows:
        for v in s["victims"]:
            t = d.txs[v["tx"]]
            key = "7702" if t["to"] == t["from"] else t["to"]
            apps[key][0] += s["loss_usd"] / len(s["victims"])
            apps[key][1] += 1
            apps[key][2].add(t["from"])
    apps = sorted(apps.items(), key=lambda kv: -kv[1][0])
    top_app = apps[0]
    app_share = pct(100 * top_app[1][0] / loss)

    # peaks
    by_day = defaultdict(float)
    by_hour = defaultdict(float)
    for s in rows:
        by_day[s["day"]] += s["loss_usd"]
        by_hour[clock.hour(s["front"]["b"])] += s["loss_usd"]
    peak = max(by_day, key=by_day.get)
    peak_hour = max(by_hour, key=by_hour.get)
    peak_value = by_hour[peak_hour]
    # the hour from which losses stay under a tenth of the peak until the launch
    before = [h for h in clock.names if peak_hour < h < when(PUBLIC_LAUNCH, "%Y-%m-%dT%H")]
    low = next((i for i in range(len(before)) if all(by_hour.get(k, 0) < peak_value / 10 for k in before[i:])), None)
    low_cap = -(-max(by_hour.get(k, 0) for k in before[low:]) // 100) * 100 if low is not None else None

    # ---- header and key
    hours_before = (PUBLIC_LAUNCH - first) / 3600
    hours_after = (d.head["ts"] - PUBLIC_LAUNCH) / 3600
    rate_before = (len(rows) - len(after)) / hours_before
    rate_after = len(after) / hours_after
    header = f"""
<header>
<h1>Sandwich attacks on Elysium</h1>
<p class="standfirst">Elysium opened as a private mainnet with early-access trading and went public on
{at(PUBLIC_LAUNCH)}. Bots have run more than {n(len(rows) // 1000 * 1000)} sandwiches since the private launch.
After the public launch, the rate fell from about {approx(rate_before)} sandwiches an hour to about {approx(rate_after)}.</p>
<dl class="key">
<div><dt>Data</dt><dd>Every Elysium mainnet block up to {block(d.head["block"], "block")}
({at(d.head["ts"])}). Times are UTC</dd></div>
<div><dt>USD values</dt><dd>HYPE amounts converted at each day's median price in Elysium's HYPE/USDC pools.
K is thousand, M is million</dd></div>
<div><dt>Sandwich</dt><dd>A bot buys just before a victim's buy and sells soon after it, at most {WINDOW} seconds
after its own buy, so the victim pays a higher price. Counts depend on this rule (<a href="#rules">how</a>)</dd></div>
<div><dt>Operator</dt><dd>One attacker: the wallets that send its transactions and the bot contracts they call.
<a href="#who">The five that caused the most loss</a> are labeled Operator A to E</dd></div>
<div><dt>Victims lost</dt><dd>What the victim's trade would have returned without the bot's buy, minus what it
returned. For {unmeasured_pct} of sandwiches it can't be measured, and they count as zero
(<a href="#unmeasured">why</a>)</dd></div>
<div><dt>Bots kept</dt><dd>The operator's net gain in HYPE, WHYPE and USDC over its two transactions,
after gas</dd></div>
</dl>
</header>"""

    # ---- summary
    def tile(label, value, note):
        return (
            f'<div class="tile"><span class="l">{label}</span><span class="v">{value}</span>'
            f'<span class="h">{note}</span></div>'
        )

    def event(t, text):
        return f"<li>{stamp(t)}<span>{text}</span></li>"

    tiles = "\n".join(
        [
            tile("Victims lost", usd(loss), f"{hype(loss_hype)} HYPE<br>median sandwich: {pct(median_pct)} of the trade"),
            tile("Bots kept, after gas", usd(profit), f"{hype(profit_hype)} HYPE<br>{kept_pct} of the loss"),
            tile(
                "Value of sandwiched trades",
                usd(volume),
                f"{n(victim_trades)} trades by {n(len(victims))} wallets<br>in {n(len(rows))} sandwiches",
            ),
            tile(
                "After public launch",
                usd(after_loss),
                f"lost in {n(len(after))} sandwiches<br>since {at(PUBLIC_LAUNCH)}",
            ),
        ]
    )
    top3 = pct(100 * sum(op[o]["loss"] for o in ops[:3]) / loss)
    beats_three = op[A]["loss"] > sum(op[o]["loss"] for o in ops[1:4])
    more_than_three = ", more than the next three operators together" if beats_three else ""
    until = when(a_same, "%b %-d") if a_same else "the launch"
    timeline = [
        (first, f"First sandwich. Until {until}, bots mostly sell a block or more after the victim."),
        (op[A]["first"], f"{opref(A)} starts and soon becomes the largest attacker."),
        (hour_start(peak_hour), f"Losses peak: {usd(peak_value)} in one hour."),
    ]
    if a_same:
        timeline.append((a_same, f"{opref(A)} starts putting its buy, the victim and its sell in one block."))
    if low is not None:
        timeline.append((hour_start(before[low]), f"Losses fall under {usd(low_cap)} an hour and stay there."))
    timeline.append((PUBLIC_LAUNCH, f"Public launch. {opref(A)} stops at {when(op[A]['last'], '%H:%M')}."))
    route_day = defaultdict(lambda: [0.0, 0.0])  # [inbox, sequencer] losses per day
    for s in rows:
        route_day[s["day"]][s["victims"][0].get("path") != "inbox"] += s["loss_usd"]
    days_seen = sorted(route_day)
    switch = next((x for i, x in enumerate(days_seen) if all(route_day[y][1] > route_day[y][0] for y in days_seen[i:])), None)
    if switch and switch != days_seen[0]:
        start = min(s["t"] for s in rows if s["day"] == switch and s["victims"][0].get("path") != "inbox")
        timeline.append(
            (start, "Most losses now come from buys sent to the sequencer, no longer through the inbox on HyperEVM.")
        )
    timeline = "\n".join(event(t, text) for t, text in sorted(timeline, key=lambda e: e[0]))
    summary = f"""
<section class="summary">
<div class="tiles">
{tiles}
</div>
<p class="lead">{", ".join(opref(o) for o in ops[:2])} and {opref(ops[2])} caused {top3} of the losses. Victims who traded through one trading-bot router, {addr(top_app[0])}, lost {app_share} of the
total. The timeline follows {opref(A)}: it alone caused {a_share}
of the losses{more_than_three}.</p>
<ol class="events">
{timeline}
</ol>
</section>"""

    # ---- examples
    def tip(h):
        t = d.txs[h].get("tip")
        return int(t, 16) if t else None

    def example_fig(label, ex):
        loss_h, paid = ex["loss"] / 1e18, ex["victim_hype"] / 1e18
        kept = ex["profit"] / 1e18
        buy, victim, sell = ex["front"]["tx"], ex["victims"][0]["tx"], ex["back"]["tx"]
        bt, vt = tip(buy), tip(victim)
        ahead = (
            "1 wei above the victim's" if bt is not None and vt is not None and bt - vt == 1 else "above the victim's"
        )
        later = ex["back"]["b"] - ex["victims"][0]["b"]
        if later == 0:
            sells = "everything with a zero tip, so it goes right after the victim."
        else:
            sells = f"everything {later} block{'s' if later > 1 else ''} later."
        rest = "; pool and launchpad fees take the rest of the victim's loss." if kept < loss_h else "."
        who = opref(ex["operator"]) if ex["operator"] in name else addr(ex["operator"])
        steps = f"""<ol class="steps">
<li><p><b>1. Bot buys</b> with {hype(ex["front_hype"] / 1e18)} HYPE. Its tip is {ahead}, so it goes
first.</p>{tx(buy, short(buy))}</li>
<li><p><b>2. Victim buys</b> with {hype(paid)} HYPE at the raised price and gets {pct(100 * loss_h / paid)} fewer
tokens, a loss of {hype(loss_h)} HYPE.</p>{tx(victim, short(victim))}</li>
<li><p><b>3. Bot sells</b> {sells} It keeps {hype(kept)} HYPE{rest}</p>{tx(sell, short(sell))}</li>
</ol>"""
        return figure(
            f"{label}, {day(ex['day'])}: the victim lost {pct(100 * loss_h / paid)} of the trade",
            f"{block(ex['front']['b'])} on {at(ex['t'])}, {d.pool_label(ex['pool'])} on {d.venue(ex)}, run by {who}.",
            steps,
        )

    by_front = {s["front"]["tx"]: s for s in rows}
    figs = "\n".join(example_fig(label, by_front[h]) for label, h in EXAMPLES if by_front.get(h, {}).get("loss"))

    # ---- how bots saw the trades
    hdr = load("headers.json")
    tiers_n = Counter(s["tier"] for s in rows)
    tip_ok = sum(1 for s in rows if s["tier"] == "A" and s["tip_signature"])
    same_n = tiers_n["A"] + tiers_n["B"]
    paired = sum(1 for s in rows if s["tier"] != "C" and s["signed_together"])
    paired_text = "In every same-block sandwich" if paired == same_n else f"In {pct(100 * paired / same_n)} of same-block sandwiches"
    by_path = {"inbox": [], "sequencer": []}
    for s in rows:
        by_path[s["victims"][0].get("path", "sequencer")].append(s)
    route_rows = []
    for key, label in (("sequencer", "Sent to the sequencer"), ("inbox", "Sent through the inbox on HyperEVM")):
        rs = by_path[key]
        if rs:
            lost = sum(s["loss_usd"] for s in rs)
            route_rows.append(
                [
                    label,
                    n(len(rs)),
                    dollars(lost),
                    bar(lost, loss),
                    stamp(min(s["t"] for s in rs)),
                    stamp(max(s["t"] for s in rs)),
                ]
            )
    seq_share = pct(100 * len(by_path["sequencer"]) / len(rows))
    route_fig = figure(
        f"In {seq_share} of sandwiches, the victim's buy was sent to the sequencer",
        "Sandwiches by how the victim's buy reached Elysium: sent to the sequencer through an RPC, or sent through the "
        "inbox on HyperEVM (below).",
        table(
            [
                ("Victim's buy", "l"),
                ("Sandwiches", "r"),
                ("Victims lost ($)", "r"),
                ("", "bar"),
                ("First", "l"),
                ("Last", "l"),
            ],
            route_rows,
        ),
    )

    proven = [s for s in by_path["inbox"] if (s.get("inbox") or {}).get("proven")]
    inbox_unknown = sum(1 for s in d.all if (s.get("inbox") or {}).get("unknown"))
    inbox_unknown_text = f" {n(inbox_unknown)} matches could not be checked." if inbox_unknown else ""
    inbox_fig = ""
    if proven:
        leads = [s["inbox"]["lead"] for s in proven]
        ex = max((s for s in proven if s["loss"]), key=lambda s: s["loss_usd"], default=proven[0])
        front_ts = hdr[str(ex["front"]["b"])][0]
        msg = ex["inbox"]["message"]
        msg_link = (
            f'<a class="addr" href="{HYPEREVM_EXPLORER}tx/{msg}" target="_blank" rel="noopener" title="{msg}">'
            f"{short(msg)}</a>"
        )
        v = ex["victims"][0]
        lost_text = (
            f": it gets {pct(100 * ex['loss'] / ex['victim_hype'])} fewer tokens, "
            f"a loss of {hype(ex['loss'] / 1e18)} HYPE"
            if ex["loss"]
            else ""
        )
        kept_text = f", keeping {hype(ex['profit'] / 1e18)} HYPE" if ex["profit"] is not None else ""
        msg_time = when(front_ts - ex["inbox"]["lead"], "%H:%M:%S")
        sell = tx(ex["back"]["tx"], short(ex["back"]["tx"]))
        steps = f"""<ol class="steps">
<li><p><b>1. Victim sends its buy</b> through the inbox on HyperEVM at {msg_time}.
Anyone can read it there.</p>{msg_link}</li>
<li><p><b>2. Bot buys on Elysium</b> {ex["inbox"]["lead"]} s later, through the sequencer, with
{hype(ex["front_hype"] / 1e18)} HYPE.</p>{tx(ex["front"]["tx"], short(ex["front"]["tx"]))}</li>
<li><p><b>3. Elysium executes the victim's buy</b> {ex["buy_to_victim_blocks"]} blocks later, at the raised
price{lost_text}.</p>{tx(v["tx"], short(v["tx"]))}</li>
<li><p><b>4. Bot sells</b> {ex["seconds"]} s after its buy{kept_text}.</p>{sell}</li>
</ol>"""
        inbox_fig = '<div id="inbox-example">' + figure(
            "Through the inbox: the bot bought as soon as the victim's buy appeared on HyperEVM",
            "This is how the chain works: Elysium is an Arbitrum Nitro chain, and instead of sending a transaction to "
            "the sequencer, anyone can send it through the inbox on HyperEVM, Elysium's parent chain. The sequencer "
            "executes it seconds later; until then the trade is public on HyperEVM. "
            f"In {pct(100 * len(proven) / len(by_path['inbox']))} of sandwiches with a victim's buy sent this way, the "
            "bot bought after the victim's message appeared on HyperEVM (or in the same second), a median of "
            f"{statistics.median(leads):g} s later.{inbox_unknown_text} "
            f"Example from {at(front_ts)}, {d.pool_label(ex['pool'])}:",
            steps,
        ) + "</div>"

    example = f"""
<section id="example"><h2>What does a sandwich look like?</h2>
<p class="dek">Both victims sent their buy to the sequencer, and in both the bot saw it before it reached a block.
In {pct(100 * len(by_path["inbox"]) / len(rows))} of sandwiches the victim sent its buy through the inbox on HyperEVM
instead (<a href="#inbox-example">example</a>).</p>
{figs}
</section>"""

    seq = by_path["sequencer"]
    early = sum(1 for s in seq if s["buy_to_victim_blocks"] >= 1)
    early_pct = pct(100 * early / len(seq)) if seq else "0%"
    limit_pct = pct(100 * sum(1 for s in seq if "limit" in s.get("proofs", [])) / len(seq)) if seq else "0%"
    seq_fig = figure(
        "Sent to the sequencer: bots knew the victim's exact transaction",
        "These trades were never public before they were in a block, yet bots acted on them:",
        f"""<ul class="facts">
<li><b>Victim pushed to its minimum.</b> In {limit_pct} of these sandwiches the victim received just above the
minimum output written in its own transaction, against about {CHANCE:.1%} of ordinary buys. The bot sized its buy to
that limit, which it could only read from the victim's transaction.</li>
<li><b>Tips.</b> When the bot's buy, the victim's trade and the bot's sell are back to back in one block, the bot's buy
paid a higher tip than the victim in {pct(100 * tip_ok / tiers_n["A"])} of cases, usually exactly 1 wei more, and its
sell paid no more than the victim.</li>
<li><b>Signed in advance.</b> {paired_text}, the bot sent its sell together with its buy (consecutive nonces): it
planned the whole sandwich before the victim's trade was in a block.</li>
<li><b>Ahead of the block.</b> In {early_pct} of these sandwiches, the bot's buy landed in an earlier block than the
victim's buy, so the bot acted before the victim's trade was in any block.</li>
</ul>""",
    )
    seen = f"""
<section id="seen"><h2>How did bots see the trades?</h2>
<p class="dek">Elysium has no public mempool, yet bots saw victims' buys before they reached a block, in two
ways.</p>
{route_fig}
{inbox_fig}
{seq_fig}
</section>"""

    # ---- when
    days = sorted({s["day"] for s in rows} | {clock.day(d.head["block"])})
    days = [x for x in sorted({k[:10] for k in clock.names}) if days[0] <= x <= days[-1]]
    per_op = {o: defaultdict(float) for o in [*ops[:NAMED], "rest"]}
    for s in rows:
        per_op[s["operator"] if s["operator"] in name else "rest"][s["day"]] += s["loss_usd"]
    series = [(name[o], color[o], per_op[o]) for o in ops[:NAMED]] + [("Others", OTHER, per_op["rest"])]
    hours = [h for h in clock.names if h[:10] >= days[0]]
    per_op_hour = {o: defaultdict(float) for o in per_op}
    for s in rows:
        per_op_hour[s["operator"] if s["operator"] in name else "rest"][clock.hour(s["front"]["b"])] += s["loss_usd"]
    events = [
        (first, "First sandwich", "mev"),
        (op[A]["first"], "Operator A starts", "mev"),
        *([(a_same, "Operator A: same block", "mev")] if a_same else []),
        (PUBLIC_LAUNCH, "Public launch", "chain"),
        (op[A]["last"], "Operator A stops", "mev"),
    ]
    hour_index = {h: i for i, h in enumerate(hours)}
    marks = [
        (hour_index[when(t, "%Y-%m-%dT%H")] + (t % 3600) / 3600, text, k)
        for t, text, k in events
        if when(t, "%Y-%m-%dT%H") in hour_index
    ]
    launch_hour = when(PUBLIC_LAUNCH, "%Y-%m-%dT%H")
    band = (launch_hour,) if launch_hour in hours else None
    shaded = " The shaded part is the private mainnet." if band else ""
    daily = stacked_area(
        [(h, day(h[:10]) if h.endswith("T00") else "") for h in hours],
        [(name[o], color[o], per_op_hour[o]) for o in ops[:NAMED]] + [("Others", OTHER, per_op_hour["rest"])],
        "Victims' losses per hour, by operator",
        axis=usd_axis,
        band=band,
        events=marks,
        tip=usd,
        tip_key=lambda h: f"{day(h[:10])}, {h[11:]}:00",
    )
    a_leads = next((x for x in days if per_op[A][x] and per_op[A][x] == max(v[2].get(x, 0) for v in series)), days[0])
    traded_peak = d.volume.get(peak, 0)
    peak_sandwiched = sum(s["victim_hype"] for s in rows if s["day"] == peak) / 1e18
    share_peak = 100 * peak_sandwiched / traded_peak if traded_peak else 0
    daily_fig = figure(
        f"Operators took turns, then {opref(A)} caused most losses from {day(a_leads)}",
        f"Victims' losses per hour, by operator.{shaded}",
        daily,
        legend([(nm, c) for nm, c, _ in series]),
    )

    tiers = {"same": defaultdict(float), "next": defaultdict(float), "inbox": defaultdict(float)}
    for s in rows:
        path = "same" if s["tier"] != "C" else ("inbox" if s["victims"][0].get("path") == "inbox" else "next")
        tiers[path][clock.hour(s["front"]["b"])] += s["loss_usd"]
    inbox_share = pct(100 * sum(tiers["inbox"].values()) / loss)
    hourly_series = [  # the inbox share is the point, so it sits on the baseline
        ("Victim sent through the inbox on HyperEVM", "var(--inbox)", tiers["inbox"]),
        ("Victim sent to the sequencer, bot sold in a later block", "var(--next)", tiers["next"]),
        ("Victim sent to the sequencer, same block", "var(--same)", tiers["same"]),
    ]
    hourly = stacked_area(
        [(h, day(h[:10]) if h.endswith("T00") else "") for h in hours],
        hourly_series,
        "Victims' losses per hour: same block, later block, and through the inbox",
        axis=usd_axis,
        band=band,
        events=marks,
        tip=usd,
        tip_key=lambda h: f"{day(h[:10])}, {h[11:]}:00",
    )
    if low is not None:
        hours_early = round((PUBLIC_LAUNCH - hour_start(before[low])) / 3600)
        hourly_title = f"Losses peaked on {day(peak_hour[:10])} and fell {hours_early} hours before the public launch"
    else:
        hourly_title = "Victims' losses per hour"
    hourly_fig = figure(
        hourly_title,
        f"Victims' losses per hour, by how the victim's buy reached Elysium.{shaded} Victims who sent their buy through the "
        f"inbox on HyperEVM lost {inbox_share} of the total. On the worst day, {day(peak)}, sandwiched trades were "
        f"{pct(share_peak)} of all HYPE traded on Elysium.",
        hourly,
        legend([(label, c) for label, c, _ in hourly_series]),
    )

    per_day = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for s in rows:
        x = per_day[s["day"]]
        x[0] += 1
        x[1] += s["volume_usd"]
        x[2] += s["loss_usd"]
        x[3] += s["profit_usd"]
    day_table = table(
        [
            ("Day", "l"),
            ("HYPE price ($)", "r"),
            ("Sandwiches", "r"),
            ("Sandwiched trades ($)", "r"),
            ("Victims lost ($)", "r"),
            ("Bots kept ($)", "r"),
        ],
        [
            [day(k), f"{d.prices[k]:,.2f}", n(v[0]), dollars(v[1]), dollars(v[2]), dollars(v[3])]
            for k, v in sorted(per_day.items())
        ],
    )
    when_section = f"""
<section id="when"><h2>When did it happen?</h2>
{daily_fig}
{hourly_fig}
<details><summary>Day by day</summary>{day_table}</details></section>"""

    # ---- who
    ops_table = table(
        [
            ("Operator", "l"),
            ("Sandwiches", "r"),
            ("Same block", "r"),
            ("Victims lost ($)", "r"),
            ("", "bar"),
            ("Kept ($)", "r"),
            ("First", "l"),
            ("Last", "l"),
        ],
        [
            [
                op_label(o),
                n(op[o]["n"]),
                whole_pct(100 * op[o]["same"] / op[o]["n"]),
                dollars(op[o]["loss"]),
                bar(op[o]["loss"], op[A]["loss"], color.get(o, OTHER)),
                dollars(op[o]["profit"]),
                stamp(op[o]["first"]),
                stamp(op[o]["last"]),
            ]
            for o in ops
        ]
        + [
            [
                "<b>Total</b>",
                f"<b>{n(len(rows))}</b>",
                "",
                f"<b>{dollars(loss)}</b>",
                "",
                f"<b>{dollars(profit)}</b>",
                "",
                "",
            ]
        ],
        "ops",
    )
    after_ops = Counter(s["operator"] for s in after)
    after_ops_text = (
        f'<p class="dek">After the public launch, {n(len(after_ops))} operators were still active. Their shares of '
        "the sandwiches since then: "
        + ", ".join(
            f"{opref(o) if o in name else addr(o)} {pct(100 * k / len(after))}" for o, k in after_ops.most_common()
        )
        + ".</p>"
        if after_ops
        else ""
    )
    ops_fig = figure(
        f"Operator A caused {a_share} of all losses",
        f"All {n(len(ops))} operators, by victims' loss. <i>Same block</i> is the share of an operator's sandwiches "
        "with its buy, the victim's trade and its sell in one block; in the rest it sold in a later block.",
        ops_table + after_ops_text,
    )

    members, recipients = d.ops["members"], set(d.ops["recipients"])

    def addresses(xs, limit=4):
        more = f' <span class="more">+{len(xs) - limit} more</span>' if len(xs) > limit else ""
        return " ".join(addr(x) for x in xs[:limit]) + more

    def addr_row(o):
        rs = op_rows[o]
        wallets = sorted({s["wallet"] for s in rs})
        bots = sorted({s["bot"] for s in rs if s["bot"] and s["bot"] != s["wallet"]})
        app = Counter(s["to_front"] for s in rs if s["to_front"] == s["to_back"] and s["to_front"] not in bots)
        app = [a for a, k in app.most_common(1) if k >= len(rs) / 2 and not bots]
        gets = sorted(set(members.get(o, [])) & recipients)
        other = []
        if app:
            other.append(f"funds held in app contract {addr(app[0])}")
        if gets:
            other.append(f"profit sent to {addresses(gets)}")
        worst = max(rs, key=lambda s: s["loss_usd"])
        return [
            f"{swatch(color[o])}{letter[o]}",
            addresses(wallets),
            addresses(bots) or "none",
            "; ".join(other),
            legs(worst),
        ]

    addr_rows = [addr_row(o) for o in ops[:NAMED]]
    botless = [opref(o) for o, r in zip(ops[:NAMED], addr_rows, strict=True) if r[2] == "none"]
    if len(botless) == 1:
        botless_text = f"{botless[0]} has none: its wallet trades through an app contract that also holds the funds. "
    elif botless:
        botless_text = (
            f"{' and '.join(botless)} have none: their wallets trade through an app contract holding the funds. "
        )
    else:
        botless_text = ""
    addr_table = table(
        [("", "l"), ("Wallets", "l"), ("Bot contracts", "l"), ("Other addresses", "l"), ("Largest sandwich", "l")],
        addr_rows,
        "addrs",
    )
    who_section = f"""
<section id="who"><h2>Who did it?</h2>
{ops_fig}
<figure><h3>Addresses of the five operators that caused the most loss</h3>
<p class="dek"><i>Wallets</i> are the accounts that sign and send an operator's transactions; one operator can run
several, for example to send many transactions at once. <i>Bot contracts</i> are the contracts those transactions call
to trade. {botless_text}<i>Largest sandwich</i> links the bot's buy, the victim's trade and the bot's sell.</p>
{addr_table}</figure>
</section>"""

    # ---- victims
    bucket_rows = [
        (f"{lo}–{hi}%" if hi else f"{lo}% or more", sum(1 for p in loss_pcts if p >= lo and (hi is None or p < hi)))
        for lo, hi in LOSS_BUCKETS
    ]
    loss_fig = figure(
        f"In the median sandwich, the victim lost {pct(median_pct)} of the trade",
        "Sandwiches, by the share of the trade's value the victims lost. In "
        f"{pct(100 * sum(1 for p in loss_pcts if p >= 10) / len(loss_pcts))} of sandwiches they lost 10% or more.",
        bars(bucket_rows, n, "Sandwiches by share of the trade lost", 110, 24),
    )
    app_rows = [
        [
            addr(a) if a != "7702" else "",
            escape(APPS.get(a, "Smart wallets (EIP-7702)" if a == "7702" else "")),
            n(v[1]),
            n(len(v[2])),
            dollars(v[0]),
            bar(v[0], top_app[1][0]),
        ]
        for a, v in apps[:8]
    ]
    rest_apps = apps[8:]
    if rest_apps:
        rest_loss = sum(v[0] for _, v in rest_apps)
        app_rows.append(
            [
                "",
                f"{len(rest_apps)} other contracts",
                n(sum(v[1] for _, v in rest_apps)),
                n(len(set().union(*(v[2] for _, v in rest_apps)))),
                dollars(rest_loss),
                bar(rest_loss, top_app[1][0]),
            ]
        )
    apps_fig = figure(
        f"Victims who traded through one trading-bot router lost {app_share} of the total",
        "Victims' losses by the contract their transaction called. Users of Uniswap's router and of other apps not "
        "linked to the bots were hit too.",
        table(
            [("Contract", "l"), ("Name", "l"), ("Trades", "r"), ("Wallets", "r"), ("Lost ($)", "r"), ("", "bar")],
            app_rows,
        ),
    )
    vict = defaultdict(lambda: [0.0, 0, 0.0])
    for s in rows:
        for v in s["victims"]:
            w = vict[d.txs[v["tx"]]["from"]]
            w[0] += s["loss_usd"] / len(s["victims"])
            w[1] += 1
            w[2] += s["volume_usd"] / len(s["victims"])
    largest = sorted(vict.items(), key=lambda kv: -kv[1][0])[:10]
    wallet_op = {s["wallet"]: s["operator"] for s in rows}

    def victim_label(w):
        o = wallet_op.get(w)
        if o is None:
            return addr(w)
        owner = f"{name[o]}'s" if o in name else "a bot"
        return f'{addr(w)} <span class="more">{owner} wallet</span>'

    bot_victims = [w for w, _ in largest if w in wallet_op]
    victims_note = (
        '<p class="dek">Bots were sandwiched too: a wallet marked with an operator also ran sandwiches of its own.</p>'
        if bot_victims
        else ""
    )
    victims_table = table(
        [("Wallet", "l"), ("Trades", "r"), ("Sandwiched trades ($)", "r"), ("Lost ($)", "r")],
        [[victim_label(w), n(v[1]), dollars(v[2]), dollars(v[0])] for w, v in largest],
    )
    victims_section = f"""
<section id="victims"><h2>Who got hurt, and how badly?</h2>
{loss_fig}
{apps_fig}
<details><summary>Ten victims who lost the most</summary>
{victims_table}{victims_note}
</details></section>"""

    # ---- where
    pools = defaultdict(lambda: {"loss": 0.0, "n": 0, "volume": 0.0, "ops": Counter(), "ex": None, "venue": ""})
    for s in rows:
        p = pools[s["pool"]]
        p["loss"] += s["loss_usd"]
        p["n"] += 1
        p["volume"] += s["volume_usd"]
        p["ops"][s["operator"]] += 1
        p["venue"] = VENUE_SHORT[s["front"]["venue"]]
        rank = (s["tier"] == "A", s["loss_usd"])
        if p["ex"] is None or rank > p["ex"][0]:
            p["ex"] = (rank, s)
    top10 = sorted(pools.items(), key=lambda kv: -kv[1]["loss"])[:10]
    main_op = {p: v["ops"].most_common(1)[0][0] for p, v in top10}
    pool_head = [
        ("Pool", "l"),
        ("Venue", "l"),
        ("Sandwiches", "r"),
        ("Sandwiched trades ($)", "r"),
        ("Lost ($)", "r"),
        ("", "bar"),
        ("Main operator", "l"),
        ("Example", "l"),
    ]
    pool_rows = [
        [
            escape(d.pool_label(p)),
            v["venue"],
            n(v["n"]),
            dollars(v["volume"]),
            dollars(v["loss"]),
            bar(v["loss"], top10[0][1]["loss"]),
            op_label(main_op[p]),
            legs(v["ex"][1]),
        ]
        for p, v in top10
    ]
    # a column that holds one value in every row moves into the subtitle
    one_venue = len({v["venue"] for _, v in top10}) == 1
    one_op = len(set(main_op.values())) == 1
    drop = {i for i, flag in ((1, one_venue), (6, one_op)) if flag}
    pool_head = [h for i, h in enumerate(pool_head) if i not in drop]
    pool_rows = [[c for i, c in enumerate(r) if i not in drop] for r in pool_rows]
    first_op = next(iter(main_op.values()))
    pool_note = f" {name.get(first_op, short(first_op))} was the main attacker in all ten." if one_op else ""
    top10_share = pct(100 * sum(v["loss"] for _, v in top10) / loss)
    pools_fig = figure(
        "Most targets were tokens paired with HYPE on Uniswap V4",
        f"The ten pools with the largest losses. {n(len(pools))} pools were hit; these ten took {top10_share} "
        f"of all losses.{pool_note}",
        table(pool_head, pool_rows),
    )
    where = f"""
<section id="where"><h2>Where did it happen?</h2>
{pools_fig}
</section>"""

    # ---- method
    by_venue = Counter(d.venue(s) for s in rows)

    def venue_row(label, events_key, venue, note=""):
        return [label, n(d.venues.get(events_key, 0)), n(by_venue[venue]) if venue else "", note]

    venue_table = table(
        [("Venue", "l"), ("Swap events", "r"), ("Sandwiches", "r"), ("Note", "l")],
        [
            venue_row("Uniswap V4 pools, including launchpad hooks", "Uniswap V4", "Uniswap V4"),
            venue_row("Uniswap V2 and V3 pools and forks", "Uniswap V2/V3", "Uniswap V2/V3"),
            venue_row(
                "Bin AMM pools (USDC/WHYPE, from Nov 9)",
                "Bin AMM pools",
                "Bin AMM",
                "swaps rebuilt from token transfers",
            ),
            venue_row(
                "Liquidity Book pools (USDC/WHYPE, from Nov 9)",
                "Liquidity Book pools",
                "Liquidity Book",
                "swaps rebuilt from token transfers",
            ),
            venue_row(
                "Aggregator <i>Exchange</i> events",
                "Aggregator Exchange events",
                None,
                "trades land in the pools above",
            ),
            venue_row(
                "Bonding-curve Buy and Sell events", "Bonding-curve Buy / Sell events", None, "too few to matter"
            ),
            venue_row("Algebra, PancakeSwap V3, Curve, Balancer", "Algebra, PancakeSwap V3, Curve, Balancer", None),
        ],
    )
    by_proof = Counter(p for s in d.all for p in s.get("proofs", []))
    proven_n = sum(1 for s in d.all if s.get("proofs"))
    chance = [s for s in d.all if s.get("proofs") and not s["counted"]]
    pattern_n = sum(1 for s in rows if not s.get("proofs"))
    rules_table = table(
        [
            ("Rule", "l"),
            ("Matches", "r"),
            ("Matching wallets", "r"),
            ("Proven", "r"),
            ("Counted", "r"),
            ("Operators", "r"),
            ("Counted after launch", "r"),
        ],
        [
            [
                r["rule"],
                n(r["matches"]),
                n(r["wallets"]),
                n(r["proven"]),
                n(r["counted"]),
                n(r["counted_operators"]),
                n(r["after_launch"]),
            ]
            for r in load("rules.json", [])
        ],
    )
    check = load("handcheck.json", None)
    check_text = (
        f" In a random sample of {n(check['checked'])} sandwiches counted without a proof of their own, the victim "
        f"lost in {'every one' if check['victim_lost'] == check['checked'] else pct(100 * check['victim_lost'] / check['checked'])} and the bot also profited in "
        f"{pct(100 * check['bot_profited'] / check['checked'])}."
        if check
        else ""
    )
    method = f"""
<section id="method"><h2>How do we know?</h2>
<div class="method">
<div><h3>Venues</h3><p>All swap events of the common AMM formats, from block 1.</p>{venue_table}</div>
<div><h3>Finding sandwiches</h3><ol>
<li>In one pool: a buy, then other wallets buying in the same direction, then a sell by the first buyer of about what
it bought (within 5%), with the sell's block at most {WINDOW} seconds after the buy's block. We call this pattern a
<i>match</i>. Each buy and each sell joins one match at most: a sell takes the bot's earliest buy that is still
free.</li>
<li>The buy and the sell come from the same wallet, each victim from a different one.</li>
<li>A buy, then others buying, then a sell is also what an ordinary trader does. What makes it a sandwich is that the
bot knew the victim's transaction before it landed. {pct(100 * proven_n / len(d.all))} of matches carry their own proof
of that (a match can carry more than one):<ul>
<li><b>Minimum hit:</b> {pct(100 * by_proof["limit"] / len(d.all))} of matches. The victim received just above an amount written in its own
transaction (by at most 0.01%): its minimum output, which the bot's buy was sized to reach. Ordinary buys in the same
pools show this about {CHANCE:.1%} of the time. An output exactly equal to such an amount is an exact-output trade and does
not count.</li>
<li><b>Ordered by tip:</b> {pct(100 * by_proof["order"] / len(d.all))}. In one block, the bot's buy pays a higher tip than the victim and
its sell no more than the victim.</li>
<li><b>Inbox:</b> {pct(100 * by_proof["inbox"] / len(d.all))}. The victim's buy was waiting in the inbox on HyperEVM before the bot
bought.</li></ul></li>
<li>Proofs also fire by chance, on about {CHANCE:.1%} of ordinary buys, so one proven match proves nothing. A match
counts when its operator is proven. An operator is proven in one of two ways:<ul>
<li>by proofs: {PROVEN_PER_OPERATOR} or more proven matches, and more of them than chance would give, with odds below
1 in {round(1 / ODDS):,};</li>
<li>by results: a profit in {WIN_RATE:.0%} or more of at least {WIN_MATCHES} matches. A bot that pays no tip, or sends
its own buy through the inbox, leaves almost no proofs but still wins nearly every time. This shows what the wallet
does, not that it knew the victim's transaction; no ordinary trader with 30 or more matches comes close.</li></ul>
Either way it also needs:<ul>
<li>at most {MAX_WALLETS} wallets, since a larger group is app users rather than one bot;</li>
<li>a profit over all its matches;</li>
<li>a profit in more than half of its matches. A trader that buys and sells quickly in many pools also fits the
pattern, but loses more often than it wins.</li></ul>
Then all of its matches count; {pct(100 * pattern_n / len(rows))} of counted sandwiches have no proof
of their own. The remaining proven matches, {pct(100 * len(chance) / len(d.all))} of all matches and spread over
{n(len({s["operator"] for s in chance}))} operators, do not count.{check_text}
</li></ol>
<p>The first version of this report (Nov 10) required the sell within 2 blocks. Blocks came 1 to 9 a second, so that
meant 0.2 to 2 seconds and missed bots that sold later.</p></div>
<div id="rules"><h3>How the count changes with the rule</h3>
<p>Each row keeps only the matches where the bot sold within that time, and counts them the same way. Longer windows
find many more matches, mostly ordinary traders who bought and sold while others were buying; the proof keeps them out
of the count.</p>
{rules_table}</div>
<div><h3>Detecting the inbox route</h3>
<p>A victim's buy came through the inbox on HyperEVM when the block that executed it read a new message from the
inbox (the block header counts messages read). The message's time comes from its <code>MessageDelivered</code>
log on Elysium's bridge contract on HyperEVM.</p></div>
<div><h3>Losses and profits</h3><ul>
<li><b>Victims lost:</b> every transaction that swapped in the pool from the bot's buy to the last victim is re-run
in order, without the bot's buy, with <code>eth_simulateV1</code> on the state just before the buy's block. Each
victim's result is compared with what it really received.</li>
<li id="unmeasured"><b>Not measured:</b> {unmeasured_pct} of sandwiches.
{pct(100 * len(no_price) / len(rows))} are in pools of two tokens with neither HYPE nor USDC, so their loss has no
price; for the other {pct(100 * (len(unmeasured) - len(no_price)) / len(rows))} the re-run failed, had more than 40 transactions to replay, or its result could
not be valued. They count as zero, so the
totals are a floor.</li>
<li><b>Bots kept:</b> both bot transactions are traced with <code>debug_traceTransaction</code>, summing every HYPE,
WHYPE and USDC movement in and out of the operator's addresses, minus gas. Swap events alone overstate it, because
launchpad hooks take fees outside the event.</li></ul></div>
<div><h3>Grouping wallets into operators</h3>
<p>A wallet joins the bot contract it calls for its trades. Bot contracts with the same code, once hard-coded addresses
are blanked, are joined. An address that repeatedly receives most of an operator's sell proceeds joins as its profit
address. A contract that 5 or more other wallets use for their own trades is a public app, not a bot.</p></div>
<div><h3>Out of scope</h3><ul>
<li>Sandwiches where the bot sold more than {WINDOW} seconds after its buy; launch sniping, arbitrage and
back-running.</li>
<li>Profit moved to an address that the transactions don't link to the operator, or kept in tokens other than HYPE,
WHYPE and USDC.</li></ul></div>
</div></section>"""

    with open(os.path.join(os.path.dirname(__file__), "report.css")) as f:
        css = f.read()
    nav = "".join(
        f'<a href="#{anchor}">{text}</a>'
        for anchor, text in (
            ("example", "Example"),
            ("seen", "How bots saw"),
            ("when", "When"),
            ("who", "Who"),
            ("victims", "Victims"),
            ("where", "Where"),
            ("method", "Method"),
        )
    )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sandwich attacks on Elysium</title><style>{css}</style></head><body><div class="layout">
<nav class="side">{nav}</nav>
<main>
{header}{summary}{example}{seen}{when_section}{who_section}{victims_section}{where}{method}
</main></div></body></html>"""
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w") as f:
        f.write(tie(page))
    export_csv(d, name)
    print(f"report: {n(len(rows))} sandwiches, victims lost {usd(loss)}, bots kept {usd(profit)}")


def cell(text):
    """A CSV cell for on-chain text such as token symbols: never read as a formula by spreadsheet apps."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export_csv(d, name):
    os.makedirs("data", exist_ok=True)
    with open("data/sandwiches.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "time_utc",
                "block",
                "tier",
                "seconds",
                "buy_to_victim_blocks",
                "victim_to_sell_blocks",
                "victim_path",
                "evidence",
                "operator",
                "operator_label",
                "wallet",
                "bot",
                "venue",
                "pool",
                "pool_label",
                "bot_buy_tx",
                "victim_txs",
                "bot_sell_tx",
                "victim_hype",
                "victim_loss_hype",
                "victim_loss_usd",
                "bot_profit_hype",
                "bot_profit_usd",
            ]
        )
        for s in sorted(d.rows, key=lambda s: (s["front"]["b"], s["front"]["li"])):
            w.writerow(
                [
                    when(s["t"], "%Y-%m-%d %H:%M:%S"),
                    s["front"]["b"],
                    s["tier"],
                    s["seconds"],
                    s["buy_to_victim_blocks"],
                    s["victim_to_sell_blocks"],
                    s["victims"][0].get("path", ""),
                    s.get("evidence") or "",
                    s["operator"],
                    name.get(s["operator"], ""),
                    s["wallet"],
                    s["bot"] or "",
                    d.venue(s),
                    s["pool"],
                    cell(d.pool_label(s["pool"])),
                    s["front"]["tx"],
                    " ".join(v["tx"] for v in s["victims"]),
                    s["back"]["tx"],
                    f"{s['victim_hype'] / 1e18:.6f}",
                    "" if s["loss"] is None else f"{s['loss'] / 1e18:.6f}",
                    f"{s['loss_usd']:.2f}" if s["loss"] is not None else "",
                    "" if s["profit"] is None else f"{s['profit'] / 1e18:.6f}",
                    f"{s['profit_usd']:.2f}",
                ]
            )
