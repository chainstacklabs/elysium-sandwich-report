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
from .charts import bars, stacked_bars
from .clock import Clock

EXPLORER = "https://elysium.kinetiq.xyz/"
EXAMPLE = "0xd1933d28ca7c8d1760a3365b5a1389f8125ec390e4d41994b295e4798e056292"  # bot buy of the walkthrough
NAMED = 5  # operators shown by letter and color
COLORS = ["var(--o1)", "var(--o2)", "var(--o3)", "var(--o4)", "var(--o5)"]
OTHER = "var(--o0)"
LOSS_BUCKETS = [(i, i + 1) for i in range(10)] + [(10, None)]  # equal 1-point bins, then the tail
VENUE = {"v4": "Uniswap V4", "v2v3": "Uniswap V2/V3", "bin": "Bin AMM", "lb": "Liquidity Book"}
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


def whole_pct(v):
    return "0%" if v == 0 else ("<1%" if v < 1 else f"{v:.0f}%")


def when(t, fmt="%b %-d, %H:%M"):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime(fmt)


def at(t):
    """A moment in prose: Nov 9 at 13:00, never split across lines."""
    return when(t, "%b %-d at %H:%M").replace(" ", "\u00a0")


def stamp(t):
    """A moment in a list or table: date and time in their own aligned slots."""
    return f'<span class="ts"><span class="d">{when(t, "%b %-d")}</span><span class="t">{when(t, "%H:%M")}</span></span>'


def hour_start(key):
    return dt.datetime.strptime(key, "%Y-%m-%dT%H").replace(tzinfo=dt.timezone.utc).timestamp()


def day(key):
    return dt.date.fromisoformat(key).strftime("%b %-d")


def short(a):
    return a[:6] + "…" + a[-4:]


def link(kind, value, text, cls=""):
    return f'<a class="{cls}" href="{EXPLORER}{kind}/{value}" target="_blank" rel="noopener" title="{value}">{escape(text)}</a>'


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
    return re.sub(r" ([^\s<>]{1,12}) ([^\s<>]{1,14})" + end, "\u00a0\\1\u00a0\\2", page)


def figure(title, dek, body, legend="", note=""):
    note = f'<p class="callout">{note}</p>' if note else ""
    return f'<figure><h3>{title}</h3><p class="dek">{dek}</p>{note}{legend}<div class="wrap">{body}</div></figure>'


def legend(items):
    return '<div class="legend">' + "".join(f"<span>{swatch(c)}{escape(t)}</span>" for t, c in items) + "</div>"


def table(head, rows, cls=""):
    """head: [(title, 'l' | 'r' | 'bar')]."""
    th = "".join(f'<th class="{a}">{h}</th>' for h, a in head)
    body = "".join("<tr>" + "".join(f'<td class="{a}">{c}</td>' for c, (_, a) in zip(r, head)) + "</tr>" for r in rows)
    return f'<div class="wrap"><table class="{cls}"><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


def bar(v, top, color="var(--bar)"):
    return f'<i style="width:{100 * max(v, 0) / top if top else 0:.1f}%;background:{color}"></i>'


# ---------------------------------------------------------------- data
def symbols(token_addrs):
    cache = load("symbols.json", {NATIVE: "HYPE"})
    need = sorted(set(token_addrs) - set(cache))
    if need:
        for a, r in zip(need, rpc.batch([("eth_call", [{"to": a, "data": "0x95d89b41"}, "latest"]) for a in need])):
            cache[a] = _decode_string(r) or short(a)
        save("symbols.json", cache)
    return cache


def _decode_string(r):
    if not isinstance(r, str) or len(r) < 66:
        return None
    h = r[2:]
    try:
        if len(h) >= 128 and int(h[:64], 16) == 32:
            return bytes.fromhex(h[128:128 + 2 * int(h[64:128], 16)]).decode("utf-8", "replace")[:24]
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
    loss_pcts = [100 * s["loss"] / s["victim_hype"] for s in measured]
    median_pct = statistics.median(loss_pcts)
    after = [s for s in rows if s["t"] >= PUBLIC_LAUNCH]
    after_loss = sum(s["loss_usd"] for s in after)
    first = min(s["t"] for s in rows)

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
    app_share = 100 * top_app[1][0] / loss

    by_day = defaultdict(float)
    for s in rows:
        by_day[s["day"]] += s["loss_usd"]
    peak = max(by_day, key=by_day.get)
    by_hour = defaultdict(float)
    for s in rows:
        by_hour[clock.hour(s["front"]["b"])] += s["loss_usd"]
    peak_hour = max(by_hour, key=by_hour.get)
    peak_value = by_hour[peak_hour]
    # the hour from which losses stay under a tenth of the peak until the launch
    before = [h for h in clock.names if peak_hour < h < when(PUBLIC_LAUNCH, "%Y-%m-%dT%H")]
    low = next((i for i in range(len(before)) if all(by_hour.get(k, 0) < peak_value / 10 for k in before[i:])), None)
    low_cap = -(-max(by_hour.get(k, 0) for k in before[low:]) // 100) * 100 if low is not None else None

    # ---- header and key
    pre_share = 100 * (len(rows) - len(after)) / len(rows)
    header = f"""
<header>
<h1>Sandwich attacks on Elysium</h1>
<p class="standfirst">Elysium opened as a private mainnet with early-access trading and went public on {at(PUBLIC_LAUNCH)}.
Since the private launch, bots have sandwiched more than {n(len(rows) // 1000 * 1000)} trades. {pct(pre_share)} of them came before
the public launch; since then, sandwiching has almost stopped.</p>
<dl class="key">
<div><dt>Data</dt><dd>Every Elysium mainnet block up to {block(d.head['block'], 'block')} ({at(d.head['ts'])})</dd></div>
<div><dt>Times</dt><dd>UTC</dd></div>
<div><dt>Dollars</dt><dd>HYPE converted at each day's median price in Elysium's HYPE/USDC pools. K is thousand, M is million</dd></div>
<div><dt>Sandwich</dt><dd>A bot buys just before a victim's buy and sells right after it, so the victim pays a higher price</dd></div>
<div><dt>Operator</dt><dd>One attacker: the wallets that send its transactions and the bot contracts they call.
<a href="#who">The five that caused the most loss</a> are labeled Operator A to E</dd></div>
<div><dt>Victims lost</dt><dd>What the victim's trade would have returned without the bot's buy, minus what it returned.
For {pct(100 * len(unmeasured) / len(rows))} of sandwiches it can't be measured, and they count as zero (<a href="#unmeasured">why</a>)</dd></div>
<div><dt>Bots kept</dt><dd>The operator's net gain in HYPE, WHYPE and USDC over its two transactions, after gas</dd></div>
<div><dt>Same block</dt><dd>The bot's buy, the victim's trade and the bot's sell are in one block. Otherwise the bot sold 1\u2060–\u20602\u00a0blocks later</dd></div>
</dl>
</header>"""

    # ---- summary
    def tile(label, value, note):
        return f'<div class="tile"><span class="l">{label}</span><span class="v">{value}</span><span class="h">{note}</span></div>'

    top3 = 100 * sum(op[o]["loss"] for o in ops[:3]) / loss

    def opref(o):
        return f'<a href="#op-{letter[o]}">{name[o]}</a>' 
    summary = f"""
<section class="summary">
<div class="tiles">
{tile("Victims lost", usd(loss), f"{hype(loss_hype)} HYPE<br>median {pct(median_pct)} of a trade")}
{tile("Bots kept, after gas", usd(profit), f"{hype(profit_hype)} HYPE<br>{pct(100 * profit / loss)} of the loss")}
{tile("Trades sandwiched", usd(volume), f"{n(len(rows))} trades<br>by {n(len(victims))} wallets")}
{tile("After public launch", usd(after_loss), f"lost in {n(len(after))} sandwiches<br>since {at(PUBLIC_LAUNCH)}")}
</div>
<p class="lead">Three <a href="#who">operators</a> caused {pct(top3)} of the losses. Users of one trading-bot router,
{addr(top_app[0])}, took {pct(app_share)}. The timeline follows {opref(A)}: it alone caused {pct(100 * op[A]['loss'] / loss)}
of the losses{', more than the next three operators together' if op[A]['loss'] > sum(op[o]['loss'] for o in ops[1:4]) else ''}.</p>
<ol class="events">
<li>{stamp(first)}<span>First sandwich. Until {when(a_same, '%b %-d') if a_same else 'the launch'}, bots mostly sell 1\u2060–\u20602\u00a0blocks after the victim.</span></li>
<li>{stamp(op[A]['first'])}<span>{opref(A)} starts and soon becomes the largest attacker.</span></li>
<li>{stamp(hour_start(peak_hour))}<span>Losses peak: {usd(peak_value)} in one hour.</span></li>
{f"<li>{stamp(a_same)}<span>{opref(A)} starts putting its buy, the victim and its sell in one block.</span></li>" if a_same else ''}
{f"<li>{stamp(hour_start(before[low]))}<span>Losses fall under {usd(low_cap)} an hour and stay there.</span></li>" if low is not None else ''}
<li>{stamp(PUBLIC_LAUNCH)}<span>Public launch. {opref(A)} stops at {when(op[A]['last'], '%H:%M')}.</span></li>
</ol>
</section>"""

    # ---- example
    ex = next((s for s in rows if s["front"]["tx"] == EXAMPLE), None)
    example = ""
    if ex:
        ex_loss, ex_in = ex["loss"] / 1e18, ex["victim_hype"] / 1e18
        example = f"""
<section id="example"><h2>What does a sandwich look like?</h2>
<p class="dek">{block(ex['front']['b'])} on {at(ex['t'])}. The bot saw the victim's buy before it reached a block.</p>
<ol class="steps">
<li><p><b>1. Bot buys</b> with {hype(ex['front_hype'] / 1e18)} HYPE. Its tip is 1 wei above the victim's, so it goes first.</p>{tx(ex['front']['tx'], short(ex['front']['tx']))}</li>
<li><p><b>2. Victim buys</b> with {hype(ex_in)} HYPE at the raised price and gets {pct(100 * ex_loss / ex_in)} fewer tokens, a loss of {hype(ex_loss)} HYPE.</p>{tx(ex['victims'][0]['tx'], short(ex['victims'][0]['tx']))}</li>
<li><p><b>3. Bot sells</b> everything with a zero tip, so it goes right after the victim. It keeps {hype(ex['profit'] / 1e18)} HYPE; pool and launchpad fees take the rest of the victim's loss.</p>{tx(ex['back']['tx'], short(ex['back']['tx']))}</li>
</ol></section>"""

    # ---- when
    days = sorted({s["day"] for s in rows} | {clock.day(d.head["block"])})
    days = [x for x in sorted({k[:10] for k in clock.names}) if days[0] <= x <= days[-1]]
    per_op = {o: defaultdict(float) for o in ops[:NAMED] + ["rest"]}
    for s in rows:
        per_op[s["operator"] if s["operator"] in name else "rest"][s["day"]] += s["loss_usd"]
    series = [(name[o], color[o], per_op[o]) for o in ops[:NAMED]] + [("Others", OTHER, per_op["rest"])]
    launch_day = when(PUBLIC_LAUNCH, "%Y-%m-%d")
    daily = stacked_bars([(x, day(x)) for x in days], series, "Victims' losses per day, by operator", total=usd, tip=usd,
                         band=(launch_day, "Private mainnet", "Public mainnet") if launch_day in days else None)
    a_leads = next((x for x in days if per_op[A][x] and per_op[A][x] == max(v[2].get(x, 0) for v in series)), days[0])
    traded_peak = d.volume.get(peak, 0)
    share_peak = 100 * sum(s["victim_hype"] for s in rows if s["day"] == peak) / 1e18 / traded_peak if traded_peak else 0

    hours = [h for h in clock.names if h[:10] >= days[0]]
    tiers = {"same": defaultdict(float), "next": defaultdict(float)}
    for s in rows:
        tiers["same" if s["tier"] != "C" else "next"][clock.hour(s["front"]["b"])] += s["loss_usd"]
    events = [(first, "First sandwich", "mev"), (op[A]["first"], "Operator A starts", "mev"),
              *([(a_same, "A goes same-block", "mev")] if a_same else []),
              (PUBLIC_LAUNCH, "Public launch", "chain"), (op[A]["last"], "A stops", "mev")]
    hour_index = {h: i for i, h in enumerate(hours)}
    marks = [(hour_index[when(t, "%Y-%m-%dT%H")] + (t % 3600) / 3600, text, k) for t, text, k in events
             if when(t, "%Y-%m-%dT%H") in hour_index]
    hourly = stacked_bars([(h, day(h[:10]) if h.endswith("T00") else "") for h in hours],
                          [("Same block", "var(--same)", tiers["same"]), ("1\u2060–\u20602\u00a0blocks later", "var(--next)", tiers["next"])],
                          "Victims' losses per hour, same block and 1\u2060–\u20602\u00a0blocks later", axis=usd_axis, events=marks,
                          centered=False, tip=usd, tip_key=lambda h: f"{day(h[:10])}, {h[11:]}:00")
    when_section = f"""
<section id="when"><h2>When did it happen?</h2>
{figure(f"Operators took turns, then {opref(A)} caused most losses from {day(a_leads)}",
        f"Victims' losses per day, by operator. On the worst day, {day(peak)}, sandwiched trades were {pct(share_peak)} of all HYPE traded on Elysium.",
        daily, legend([(nm, c) for nm, c, _ in series]))}
{figure(f"Losses peaked on {when(hour_start(peak_hour), '%b %-d')} and fell {round((PUBLIC_LAUNCH - hour_start(before[low])) / 3600)} hours before the public launch" if low is not None else "Victims' losses per hour",
        f"Victims' losses per hour. At the peak, {usd(peak_value)} at {when(hour_start(peak_hour), '%H:00')}, {opref(A)} was still selling 1\u2060–\u20602\u00a0blocks after the victim.", hourly, legend([("Same block", "var(--same)"), ("Bot sold 1\u2060–\u20602\u00a0blocks later", "var(--next)")]))}
</section>"""

    # ---- who
    top_loss = op[A]["loss"]

    def op_label(o):
        return f'<span id="op-{letter[o]}"></span>{swatch(color[o])}{name[o]}' if o in name else addr(o)

    ops_table = table(
        [("Operator", "l"), ("Sandwiches", "r"), ("Same block", "r"), ("Victims lost ($)", "r"), ("", "bar"), ("Kept ($)", "r"),
         ("First", "l"), ("Last", "l")],
        [[op_label(o), n(op[o]["n"]), whole_pct(100 * op[o]["same"] / op[o]["n"]), dollars(op[o]["loss"]),
          bar(op[o]["loss"], top_loss, color.get(o, OTHER)), dollars(op[o]["profit"]),
          stamp(op[o]["first"]), stamp(op[o]["last"])] for o in ops]
        + [["<b>Total</b>", f"<b>{n(len(rows))}</b>", "", f"<b>{dollars(loss)}</b>", "", f"<b>{dollars(profit)}</b>", "", ""]],
        "ops")

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
        other = ([f"funds held in app contract {addr(app[0])}"] if app else []) + ([f"profit sent to {addresses(gets)}"] if gets else [])
        worst = max(rs, key=lambda s: s["loss_usd"])
        return [f"{swatch(color[o])}{letter[o]}", addresses(wallets), addresses(bots) or "none", "; ".join(other) or "", legs(worst)]

    addr_rows = [addr_row(o) for o in ops[:NAMED]]
    botless = [opref(o) for o, r in zip(ops[:NAMED], addr_rows) if r[2] == "none"]
    botless_text = (f"{' and '.join(botless)} {'has' if len(botless) == 1 else 'have'} none: {'its wallet trades' if len(botless) == 1 else 'their wallets trade'}"
                    " through an app contract that also holds the funds. ") if botless else ""

    who_section = f"""
<section id="who"><h2>Who did it?</h2>
{figure(f"Operator A caused {pct(100 * op[A]['loss'] / loss)} of all losses",
        f"All {n(len(ops))} operators, by victims' loss. <i>Same block</i> is the share of an operator's sandwiches with its buy, the victim's trade and its sell in one block; in the rest it sold 1\u2060–\u20602\u00a0blocks later. Bots kept {pct(100 * profit / loss)} of what victims lost, because each sandwich pays pool and launchpad fees on two trades.",
        ops_table)}
<figure><h3>Addresses of the five largest operators</h3>
<p class="dek"><i>Wallets</i> are the accounts that sign and send an operator's transactions; one operator can run several,
for example to send many transactions at once. <i>Bot contracts</i> are the contracts those transactions call to trade.
{botless_text}<i>Largest sandwich</i> links the bot's buy, the victim's trade and the bot's sell.</p>
{table([("", "l"), ("Wallets", "l"), ("Bot contracts", "l"), ("Other addresses", "l"), ("Largest sandwich", "l")],
       addr_rows, "addrs")}</figure>
</section>"""

    # ---- victims
    bucket_rows = [(f"{lo}–{hi}%" if hi else f"{lo}% or more", sum(1 for p in loss_pcts if p >= lo and (hi is None or p < hi)))
                   for lo, hi in LOSS_BUCKETS]
    shown = apps[:8]
    rest_apps = apps[8:]
    app_rows = [[addr(a) if a != "7702" else "", escape(APPS.get(a, "Smart wallets (EIP-7702)" if a == "7702" else "")),
                 n(v[1]), n(len(v[2])), dollars(v[0]), bar(v[0], top_app[1][0])] for a, v in shown]
    if rest_apps:
        app_rows.append(["", f"{len(rest_apps)} other contracts", n(sum(v[1] for _, v in rest_apps)),
                         n(len(set().union(*(v[2] for _, v in rest_apps)))), dollars(sum(v[0] for _, v in rest_apps)),
                         bar(sum(v[0] for _, v in rest_apps), top_app[1][0])])
    vict = defaultdict(lambda: [0.0, 0, 0.0])
    for s in rows:
        for v in s["victims"]:
            w = vict[d.txs[v["tx"]]["from"]]
            w[0] += s["loss_usd"] / len(s["victims"])
            w[1] += 1
            w[2] += s["volume_usd"] / len(s["victims"])
    victims_section = f"""
<section id="victims"><h2>Who got hurt, and how badly?</h2>
{figure(f"The median victim lost {pct(median_pct)} of the trade",
        f"Sandwiched trades, by the share of the trade's value the victim lost. {n(sum(1 for p in loss_pcts if p >= 10))} trades lost 10% or more.",
        bars(bucket_rows, n, "Sandwiched trades by share of the trade lost", 110, 24))}
{figure(f"Users of one trading-bot router took {pct(app_share)} of the losses",
        "Victims' losses by the contract their transaction called. Users of Uniswap's router and other unrelated apps were hit too, so the leak was on a path these apps share, not inside one app.",
        table([("Contract", "l"), ("Name", "l"), ("Trades", "r"), ("Wallets", "r"), ("Lost ($)", "r"), ("", "bar")], app_rows))}
<details><summary>Ten largest victims</summary>
{table([("Wallet", "l"), ("Trades", "r"), ("Traded ($)", "r"), ("Lost ($)", "r")],
       [[addr(w), n(v[1]), dollars(v[2]), dollars(v[0])] for w, v in sorted(vict.items(), key=lambda kv: -kv[1][0])[:10]])}
</details></section>"""

    # ---- where
    pools = defaultdict(lambda: {"loss": 0.0, "n": 0, "volume": 0.0, "ops": Counter(), "ex": None, "venue": "", "venue_name": ""})
    for s in rows:
        p = pools[s["pool"]]
        p["loss"] += s["loss_usd"]
        p["n"] += 1
        p["volume"] += s["volume_usd"]
        p["ops"][s["operator"]] += 1
        p["venue"], p["venue_name"] = VENUE_SHORT[s["front"]["venue"]], d.venue(s)
        rank = (s["tier"] == "A", s["loss_usd"])
        if p["ex"] is None or rank > p["ex"][0]:
            p["ex"] = (rank, s)
    ranked = sorted(pools.items(), key=lambda kv: -kv[1]["loss"])
    top10 = ranked[:10]
    main_op = {p: v["ops"].most_common(1)[0][0] for p, v in top10}
    one_venue = len({v["venue"] for _, v in top10}) == 1  # a column with one value moves into the subtitle
    one_op = len(set(main_op.values())) == 1
    pool_head = [("Pool", "l"), ("Venue", "l"), ("Sandwiches", "r"), ("Traded ($)", "r"), ("Lost ($)", "r"), ("", "bar"),
                 ("Main operator", "l"), ("Example", "l")]
    pool_rows = [[escape(d.pool_label(p)), v["venue"], n(v["n"]), dollars(v["volume"]), dollars(v["loss"]), bar(v["loss"], top10[0][1]["loss"]),
                  op_label(main_op[p]), legs(v["ex"][1])] for p, v in top10]
    drop = {i for i, flag in ((1, one_venue), (6, one_op)) if flag}
    pool_head = [h for i, h in enumerate(pool_head) if i not in drop]
    pool_rows = [[c for i, c in enumerate(r) if i not in drop] for r in pool_rows]
    first_op = next(iter(main_op.values()))
    pool_note = f" {name.get(first_op, short(first_op))} was the main attacker in all ten." if one_op else ""
    where = f"""
<section id="where"><h2>Where did it happen?</h2>
{figure("Most targets were new tokens paired with HYPE on Uniswap V4",
        f"The ten pools with the largest losses. {n(len(pools))} pools were hit; these ten took {pct(100 * sum(v['loss'] for _, v in top10) / loss)} of all losses.{pool_note}",
        table(pool_head, pool_rows))}
</section>"""

    # ---- method
    tiers_n = Counter(s["tier"] for s in d.all)
    by_venue = Counter(d.venue(s) for s in rows)
    venue_table = table(
        [("Venue", "l"), ("Swap events", "r"), ("Sandwiches", "r"), ("Note", "l")],
        [["Uniswap V4 pools, including launchpad hooks", n(d.venues.get("Uniswap V4", 0)), n(by_venue["Uniswap V4"]), ""],
         ["Uniswap V2 and V3 pools and forks", n(d.venues.get("Uniswap V2/V3", 0)), n(by_venue["Uniswap V2/V3"]), ""],
         ["Bin AMM pools (USDC/WHYPE, from Nov 9)", n(d.venues.get("Bin AMM pools", 0)), n(by_venue["Bin AMM"]), "swaps rebuilt from token transfers"],
         ["Liquidity Book pools (USDC/WHYPE, from Nov 9)", n(d.venues.get("Liquidity Book pools", 0)), n(by_venue["Liquidity Book"]), "swaps rebuilt from token transfers"],
         ["Aggregator <i>Exchange</i> events", n(d.venues.get("Aggregator Exchange events", 0)), "", "trades land in the pools above"],
         ["Bonding-curve Buy and Sell events", n(d.venues.get("Bonding-curve Buy / Sell events", 0)), "", "too few to matter"],
         ["Algebra, PancakeSwap V3, Curve, Balancer", n(d.venues.get("Algebra, PancakeSwap V3, Curve, Balancer", 0)), "", ""]])
    tip_ok = sum(1 for s in d.all if s["tier"] == "A" and s["tip_signature"])
    same_n = tiers_n["A"] + tiers_n["B"]
    paired = sum(1 for s in d.all if s["tier"] != "C" and s["signed_together"])
    paired_text = f"In all {n(same_n)}" if paired == same_n else f"In {n(paired)} of {n(same_n)}"
    method = f"""
<section id="method"><h2>How do we know?</h2>
<div class="method">
<div><h3>Venues</h3><p>All swap events of the common AMM formats, from block 1.</p>{venue_table}</div>
<div><h3>Finding sandwiches</h3><ol>
<li>In one pool: a buy, then other wallets buying in the same direction, then a sell by the first buyer of about what it bought (within 5%), all within 2 blocks.</li>
<li>The buy and the sell come from the same wallet, each victim from a different one.</li>
<li>Same block: {n(same_n)} sandwiches, all counted. Sold 1\u2060–\u20602\u00a0blocks later: {n(tiers_n['C'])} candidates; {n(sum(1 for s in rows if s['tier'] == 'C'))} counted, from operators that profit from this pattern overall.</li></ol></div>
<div><h3>Evidence that bots saw pending transactions</h3>
<p>Elysium has no public mempool. Yet in {n(tip_ok)} of {n(tiers_n['A'])} sandwiches with the three transactions next to each other, the bot's buy pays a
higher tip than the victim and its sell no more than the victim; usually the buy pays the victim's tip plus 1 wei. {paired_text} same-block sandwiches,
the bot signed its sell together with its buy (consecutive nonces). Operator A's contract refuses to sell unless it gets back more than it paid,
which only works if it knows a buy will land in between.</p></div>
<div><h3>Losses and profits</h3><ul>
<li><b>Victims lost:</b> the victim's transaction is re-run with <code>eth_simulateV1</code> on the state just before the bot's buy and compared with what it really received.</li>
<li id="unmeasured"><b>Not measured:</b> {n(len(unmeasured))} sandwiches ({pct(100 * len(unmeasured) / len(rows))}).
{n(len(no_price))} are in pools of two tokens with neither HYPE nor USDC, so their loss has no price; for the other
{n(len(unmeasured) - len(no_price))} the re-run failed or its result could not be valued. They count as zero, so the totals are a floor.</li>
<li><b>Bots kept:</b> both bot transactions are traced with <code>debug_traceTransaction</code>, summing every HYPE, WHYPE and USDC movement in and out of the operator's addresses, minus gas. Swap events alone overstate it, because launchpad hooks take fees outside the event.</li></ul></div>
<div><h3>Grouping wallets into operators</h3>
<p>A wallet joins the bot contract it calls for its trades. Bot contracts with the same code, once hard-coded addresses are blanked, are joined.
An address that repeatedly receives most of an operator's sell proceeds joins as its profit address. A contract that 5 or more other wallets use for their own trades is a public app, not a bot.</p></div>
<div><h3>Out of scope</h3><ul>
<li>Sandwiches with more than 2 blocks between the bot's buy and sell; launch sniping, arbitrage and back-running.</li>
<li>Profit moved to an address that the transactions don't link to the operator, or kept in tokens other than HYPE, WHYPE and USDC.</li></ul></div>
</div></section>"""

    # ---- appendix
    per_day = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for s in rows:
        x = per_day[s["day"]]
        x[0] += 1
        x[1] += s["volume_usd"]
        x[2] += s["loss_usd"]
        x[3] += s["profit_usd"]
    appendix = "<details><summary>Day by day</summary>" + table(
        [("Day", "l"), ("HYPE price ($)", "r"), ("Sandwiches", "r"), ("Traded ($)", "r"), ("Victims lost ($)", "r"), ("Bots kept ($)", "r")],
        [[day(k), f"{d.prices[k]:,.2f}", n(v[0]), dollars(v[1]), dollars(v[2]), dollars(v[3])] for k, v in sorted(per_day.items())]) + "</details>"

    when_section = when_section.removesuffix("</section>") + appendix + "</section>"  # the daily table backs the timeline
    css = open(os.path.join(os.path.dirname(__file__), "report.css")).read()
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sandwich attacks on Elysium</title><style>{css}</style></head><body><div class="layout">
<nav class="side"><a href="#example">Example</a><a href="#when">When</a><a href="#who">Who</a><a href="#victims">Victims</a><a href="#where">Where</a><a href="#method">Method</a></nav>
<main>
{header}{summary}{example}{when_section}{who_section}{victims_section}{where}{method}
</main></div></body></html>"""
    os.makedirs("docs", exist_ok=True)
    open("docs/index.html", "w").write(tie(page))
    export_csv(d, name)
    print(f"report: {n(len(rows))} sandwiches, victims lost {usd(loss)}, bots kept {usd(profit)}")


def export_csv(d, name):
    os.makedirs("data", exist_ok=True)
    with open("data/sandwiches.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time_utc", "block", "tier", "operator", "operator_label", "wallet", "bot", "venue", "pool", "pool_label",
                    "bot_buy_tx", "victim_txs", "bot_sell_tx", "victim_hype", "victim_loss_hype", "victim_loss_usd",
                    "bot_profit_hype", "bot_profit_usd"])
        for s in sorted(d.rows, key=lambda s: (s["front"]["b"], s["front"]["li"])):
            w.writerow([when(s["t"], "%Y-%m-%d %H:%M:%S"), s["front"]["b"], s["tier"], s["operator"], name.get(s["operator"], ""),
                        s["wallet"], s["bot"] or "", d.venue(s), s["pool"], d.pool_label(s["pool"]),
                        s["front"]["tx"], " ".join(v["tx"] for v in s["victims"]), s["back"]["tx"],
                        f"{s['victim_hype'] / 1e18:.6f}", "" if s["loss"] is None else f"{s['loss'] / 1e18:.6f}",
                        f"{s['loss_usd']:.2f}" if s["loss"] is not None else "", "" if s["profit"] is None else f"{s['profit'] / 1e18:.6f}",
                        f"{s['profit_usd']:.2f}"])
