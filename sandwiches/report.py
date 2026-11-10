"""Build docs/index.html (the report) and data/sandwiches.csv from the cache."""
import csv
import datetime as dt
import os
import statistics
from collections import Counter, defaultdict
from html import escape

from . import rpc
from .chain import APPS, NATIVE, PUBLIC_LAUNCH, load, save
from .charts import bars, stacked_bars
from .clock import Clock

EXPLORER = "https://elysium.kinetiq.xyz/"
EXAMPLE = "0xd1933d28ca7c8d1760a3365b5a1389f8125ec390e4d41994b295e4798e056292"  # bot buy of the walkthrough
NAMED = 5  # operators shown by letter and color
COLORS = ["var(--o1)", "var(--o2)", "var(--o3)", "var(--o5)", "var(--o6)"]
LOSS_BUCKETS = [(0, 1), (1, 2), (2, 5), (5, 10), (10, 20), (20, None)]


# ---------------------------------------------------------------- formatting
def usd(v):
    if abs(v) < 0.5:
        return "$0"
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.2f}M"
    return f"${v / 1e3:,.1f}k" if abs(v) >= 1e4 else f"${v:,.0f}"


def hype(v):
    return f"{v:,.0f}" if abs(v) >= 100 else (f"{v:,.1f}" if abs(v) >= 1 else f"{v:,.3f}")


def n(v):
    return f"{v:,.0f}"


def pct(v):
    return f"{v:.1f}%" if v < 10 else f"{v:.0f}%"


def utc(t, fmt="%b %-d, %H:%M", suffix=" UTC"):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime(fmt) + suffix


def short(a):
    return a[:6] + "…" + a[-4:]


def link(kind, value, text):
    return (f'<a class="chip" href="{EXPLORER}{kind}/{value}" target="_blank" rel="noopener" title="{value}">'
            f'{escape(text)}<span class="arr">↗</span></a>')


def addr(a, text=None):
    return link("address", a, text or short(a))


def tx(h, text):
    return link("tx", h, text)


def block(b):
    return link("block", b, f"block {b:,}")


def figure(question, answer, chart, caption, legend=""):
    return (f'<figure><div class="q"><h3>{question}</h3><p class="answer">{answer}</p></div>{legend}'
            f'<div class="wrap">{chart}</div><figcaption>{caption}</figcaption></figure>')


def table(head, rows):
    th = "".join(f'<th class="{"l" if left else ""}">{escape(h)}</th>' for h, left in head)
    body = "".join("<tr>" + "".join(f'<td class="{"l" if left else ""}">{c}</td>' for c, (_, left) in zip(r, head)) + "</tr>"
                   for r in rows)
    return f'<div class="wrap"><table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></div>'


# ---------------------------------------------------------------- data
def symbols(token_addrs):
    cache = load("symbols.json", {NATIVE: "HYPE"})
    need = sorted(set(token_addrs) - set(cache))
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
        return {"v4": "Uniswap V4", "v2v3": "Uniswap V2/V3", "bin": "Bin AMM", "lb": "Liquidity Book"}[s["front"]["venue"]]


# ---------------------------------------------------------------- page
def build():
    d = Data()
    rows, clock = d.rows, d.clock
    measured = [s for s in rows if s["loss"] is not None]
    loss = sum(s["loss_usd"] for s in rows)
    loss_hype = sum(s["loss"] or 0 for s in rows) / 1e18
    profit = sum(s["profit_usd"] for s in rows)
    profit_hype = sum(s["profit"] or 0 for s in rows) / 1e18
    volume = sum(s["volume_usd"] for s in rows)
    victims = {d.txs[v["tx"]]["from"] for s in rows for v in s["victims"]}
    loss_pcts = [100 * s["loss"] / s["victim_hype"] for s in measured]
    after = [s for s in rows if s["t"] >= PUBLIC_LAUNCH]
    after_loss = sum(s["loss_usd"] for s in after)
    first, last = min(s["t"] for s in rows), max(clock.time(s["back"]["b"]) for s in rows)

    # operators, ranked by the losses they caused
    op = defaultdict(lambda: defaultdict(float))
    op_rows = defaultdict(list)
    for s in rows:
        o = s["operator"]
        op_rows[o].append(s)
        for k, v in (("n", 1), ("same", s["tier"] != "C"), ("loss", s["loss_usd"]), ("profit", s["profit_usd"]),
                     ("profit_hype", (s["profit"] or 0) / 1e18)):
            op[o][k] += v
    ops = sorted(op, key=lambda o: -op[o]["loss"])
    name = {o: f"Operator {'ABCDE'[i]}" for i, o in enumerate(ops[:NAMED])}
    color = {o: COLORS[i] for i, o in enumerate(ops[:NAMED])}
    for o in ops:
        rs = op_rows[o]
        op[o]["first"] = min(s["t"] for s in rs)
        op[o]["last"] = max(clock.time(s["back"]["b"]) for s in rs)
        same = [s["t"] for s in rs if s["tier"] != "C"]
        op[o]["first_same"] = min(same) if same else None
    A = ops[0]

    # apps victims used
    apps = defaultdict(lambda: [0.0, 0, set()])
    for s in rows:
        for v in s["victims"]:
            t = d.txs[v["tx"]]
            key = "7702" if t["to"] == t["from"] else t["to"]
            apps[key][0] += s["loss_usd"] / len(s["victims"])
            apps[key][1] += 1
            apps[key][2].add(t["from"])
    apps = sorted(apps.items(), key=lambda kv: -kv[1][0])

    def app_name(a):
        return "Smart wallets (EIP-7702)" if a == "7702" else APPS.get(a, "Unnamed contract")

    top_app = apps[0]

    # ---- TL;DR
    def who(o):
        return (f'<div class="actor"><span class="dot" style="background:{color[o]}"></span><div><b>{name[o]}</b> {addr(o)} '
                f'caused {pct(100 * op[o]["loss"] / loss)} of all losses ({usd(op[o]["loss"])}) in {n(op[o]["n"])} sandwiches, '
                f'{utc(op[o]["first"])} → {utc(op[o]["last"])}.</div></div>')

    by_day = defaultdict(float)
    for s in rows:
        by_day[s["day"]] += s["loss_usd"]
    peak = max(by_day, key=by_day.get)
    tldr = f"""
<div class="tldr">
  <div class="kicker">TL;DR</div>
  <p class="lead">Between {utc(first, '%b %-d', '')} and {utc(last, '%b %-d', '')}, bots sandwiched <b>{n(len(rows))}</b> trades on
  Elysium, {pct(100 * (len(rows) - len(after)) / len(rows))} of them before the public launch. Victims lost <b class="loss">{usd(loss)}</b>
  ({hype(loss_hype)} HYPE) to worse prices; the bots kept <b class="prof">{usd(profit)}</b> ({hype(profit_hype)} HYPE) after gas, and the rest
  went to pool and launchpad fees. Three operators caused {pct(100 * sum(op[o]["loss"] for o in ops[:3]) / loss)} of the losses, and
  {pct(100 * top_app[1][0] / loss)} of the losses hit users of one trading-bot router. The large operators stopped within two hours
  of the public launch; small bots have taken {usd(after_loss)} since.</p>
  <div class="money">
    <div class="m"><span class="l">Victims lost</span><span class="v loss">{usd(loss)}</span><span class="h">{hype(loss_hype)} HYPE · median {pct(statistics.median(loss_pcts))} of a trade</span></div>
    <div class="m"><span class="l">Bots kept, after gas</span><span class="v prof">{usd(profit)}</span><span class="h">{hype(profit_hype)} HYPE · {pct(100 * profit / loss)} of the victims' loss</span></div>
    <div class="m"><span class="l">Trades sandwiched</span><span class="v">{usd(volume)}</span><span class="h">{n(len(rows))} trades by {n(len(victims))} wallets</span></div>
    <div class="m"><span class="l">After public launch</span><span class="v">{usd(after_loss)}</span><span class="h">{n(len(after))} sandwiches, last at {utc(last)}</span></div>
  </div>
  <div class="cols2">
    <div class="actors"><div class="kicker">Who</div>{''.join(who(o) for o in ops[:3])}
      <div class="kicker" style="margin-top:8px">Hit hardest</div>
      <div>Users of {addr(top_app[0])}, an unnamed trading-bot router: {usd(top_app[1][0])} lost across {n(top_app[1][1])} trades by
      {n(len(top_app[1][2]))} wallets.</div></div>
    <div><div class="kicker">What happened</div><ol class="tight">
      <li><b>{utc(first, '%b %-d', '')} – {utc(op[A]['first_same'] - 3600, '%b %-d', '') if op[A]['first_same'] else ''}:</b> sandwiching starts in early access. Bots mostly sell 1–2 blocks after the victim.</li>
      <li><b>{utc(op[A]['first'])}:</b> {name[A]} starts with its own bot contract and becomes the largest attacker.</li>
      <li><b>{utc(op[A]['first_same']) if op[A]['first_same'] else ''}:</b> its bot starts landing all three transactions in one block around each victim. Losses peak on {dt.date.fromisoformat(peak):%b %-d}.</li>
      <li><b>{utc(PUBLIC_LAUNCH)}:</b> public launch. {name[A]} stops at {utc(op[A]['last'], '%H:%M')}.</li>
    </ol></div>
  </div>
  <p class="small">Times are UTC. USD uses each day's median HYPE price in Elysium's own USDC pools. Data up to {utc(d.head['ts'])}, block {n(d.head['block'])}.</p>
</div>"""

    # ---- 1. walkthrough
    ex = next((s for s in rows if s["front"]["tx"] == EXAMPLE), None)
    walk = ""
    if ex:
        ex_loss = ex["loss"] / 1e18
        walk = f"""
<section id="what"><div class="q"><div class="kicker">1 · The mechanics</div><h2>What does a sandwich look like?</h2>
<p class="sub">One from {utc(ex['t'])}, all three transactions in {block(ex['front']['b'])}. The bot sees the victim's buy before it is
included, buys first to raise the price, lets the victim buy at that price, then sells into the price the victim pushed up.</p></div>
<div class="steps">
  <div class="step"><span class="n">Step 1 · bot buys</span><p>The bot buys with {hype(ex['front_hype'] / 1e18)} HYPE, paying a tip 1 wei above the victim's, so it is ordered first.</p>{tx(ex['front']['tx'], 'bot buy')}</div>
  <div class="step bad"><span class="n">Step 2 · victim buys</span><p>The victim's {hype(ex['victim_hype'] / 1e18)} HYPE buy fills at the higher price: <b class="loss">{pct(100 * ex_loss / (ex['victim_hype'] / 1e18))} fewer tokens</b>, a loss of {hype(ex_loss)} HYPE.</p>{tx(ex['victims'][0]['tx'], 'victim buy')}</div>
  <div class="step"><span class="n">Step 3 · bot sells</span><p>The bot sells everything with a zero tip, so it lands right after the victim, and keeps <b class="prof">{hype(ex['profit'] / 1e18)} HYPE</b>. The rest of the victim's loss went to pool and launchpad fees.</p>{tx(ex['back']['tx'], 'bot sell')}</div>
</div></section>"""

    # ---- 2. timeline
    days = sorted({s["day"] for s in rows} | {clock.day(d.head["block"])})
    days = [x for x in sorted({k[:10] for k in clock.names}) if days[0] <= x <= days[-1]]
    per_op = {o: defaultdict(float) for o in ops[:NAMED] + ["rest"]}
    for s in rows:
        per_op[s["operator"] if s["operator"] in name else "rest"][s["day"]] += s["loss_usd"]
    series = [(name[o], color[o], per_op[o]) for o in ops[:NAMED]] + [("Others", "var(--o4)", per_op["rest"])]
    daily = stacked_bars([(x, dt.date.fromisoformat(x).strftime("%b %-d")) for x in days], series, usd,
                         "Victims' losses per UTC day, by operator",
                         band=("2026-11-09", "Early access", "Public from Nov 9, 13:00 UTC") if "2026-11-09" in days else None)
    legend = '<div class="legend">' + "".join(f'<span><i style="background:{c}"></i>{escape(nm)}</span>' for nm, c, _ in series) + "</div>"
    traded_peak = d.volume.get(peak, 0)
    share_peak = 100 * sum(s["victim_hype"] for s in rows if s["day"] == peak) / 1e18 / traded_peak if traded_peak else 0

    hours = [h for h in clock.names if h[:10] >= days[0]]
    tiers = {"same": defaultdict(float), "next": defaultdict(float)}
    for s in rows:
        tiers["same" if s["tier"] != "C" else "next"][clock.hour(s["front"]["b"])] += s["loss_usd"]
    events = sorted([
        (first, "First sandwich", "mev"),
        (op[A]["first"], f"{name[A]} starts with its own bot contract", "mev"),
        *([(op[A]["first_same"], f"{name[A]} starts landing all three transactions in one block", "mev")] if op[A]["first_same"] else []),
        (PUBLIC_LAUNCH, "Public launch", "chain"),
        (op[A]["last"], f"{name[A]}'s last sandwich", "mev"),
    ])
    hour_index = {h: i for i, h in enumerate(hours)}
    marks = [(hour_index[utc(t, "%Y-%m-%dT%H", "")] + (t % 3600) / 3600, i + 1, k) for i, (t, _, k) in enumerate(events)
             if utc(t, "%Y-%m-%dT%H", "") in hour_index]
    hourly = stacked_bars([(h, dt.datetime.strptime(h[:10], "%Y-%m-%d").strftime("%b %-d") if h.endswith("T00") else "") for h in hours],
                          [("same block", "var(--same)", tiers["same"]), ("1–2 blocks later", "var(--next)", tiers["next"])],
                          usd, "Victims' losses per UTC hour, same-block and next-block sandwiches",
                          label_values=False, events=marks, height=300)
    timeline_items = "".join(f'<div class="ti {k}"><span class="t">{utc(t)}</span><span>{i}. {escape(text)}</span></div>'
                             for i, (t, text, k) in enumerate(events, 1))
    when = f"""
<section id="when"><div class="q"><div class="kicker">2 · Timeline</div><h2>When did it happen?</h2></div>
{figure("How much did victims lose each day, and to whom?",
        f"Almost all of it happened in early access. The worst day was {dt.date.fromisoformat(peak):%b %-d}: {usd(by_day[peak])}, when {pct(share_peak)} of all HYPE traded on Elysium was in sandwiched trades.",
        daily, "<span><b>Shows:</b> money victims lost to sandwiches per UTC day, by operator.</span><span><b>Measured as:</b> the victim's transaction re-run on the state just before the bot's buy, minus what the victim really got; USD at that day's HYPE price.</span>", legend)}
{figure("Did it stop when the chain opened to the public?",
        f"Mostly. Losses rose once {name[A]} put all three transactions in one block, and collapsed when it stopped two hours after the 13:00 UTC launch. Small bots continue at a low level ({usd(after_loss)} since launch).",
        hourly, "<span><b>Shows:</b> money victims lost per UTC hour. Dark: bot buy, victim and bot sell in one block. Light: the bot sold 1–2 blocks later. Numbers mark the events below.</span>",
        '<div class="legend"><span><i style="background:var(--same)"></i>Same block</span><span><i style="background:var(--next)"></i>Bot sold 1–2 blocks later</span></div>')}
<div class="timeline">{timeline_items}</div></section>"""

    # ---- 3. operators
    members, recipients = d.ops["members"], set(d.ops["recipients"])

    def card(o):
        rs = op_rows[o]
        wallets = sorted({s["wallet"] for s in rs})
        bots = sorted({s["bot"] for s in rs if s["bot"] and s["bot"] != s["wallet"]})
        app = Counter(s["to_front"] for s in rs if s["to_front"] == s["to_back"] and s["to_front"] not in bots)
        app = [a for a, k in app.most_common(1) if k >= len(rs) / 2 and not bots]
        gets = sorted(set(members.get(o, [])) & recipients)
        share = op[o]["same"] / op[o]["n"]
        later = [s for s in rs if op[o]["first_same"] and s["t"] >= op[o]["first_same"]]
        if share < 0.8 and len(later) >= 20 and sum(s["tier"] != "C" for s in later) >= 0.8 * len(later):
            how = f"Sold 1–2 blocks after the victim until {utc(op[o]['first_same'])}, then put all three transactions in one block"
        elif share >= 0.8:
            how = "Puts all three transactions in one block"
        else:
            how = f"Sells 1–2 blocks after the victim ({pct(100 * share)} in the same block)"
        worst = max(rs, key=lambda s: s["loss_usd"])
        chips = lambda xs: '<div class="chips">' + "".join(addr(x) for x in xs[:6]) + "</div>"  # noqa: E731
        return f"""<div class="op" style="--c:{color[o]}"><h3>{name[o]}</h3>
<div class="kv"><span>Victims lost</span><span><b class="loss">{usd(op[o]['loss'])}</b> · {n(op[o]['n'])} sandwiches</span>
<span>Kept after gas</span><span><b class="prof">{usd(op[o]['profit'])}</b> ({hype(op[o]['profit_hype'])} HYPE)</span>
<span>Active</span><span>{utc(op[o]['first'])} → {utc(op[o]['last'])}</span><span>How</span><span>{how}</span></div>
<div class="small">Wallets that sign its trades</div>{chips(wallets)}
{'<div class="small">Bot contracts</div>' + chips(bots) if bots else ''}
{'<div class="small">No bot contract: its funds sit in an app contract that ordinary users also trade through</div>' + chips(app) if app else ''}
{'<div class="small">Where its profit goes</div>' + chips(gets) if gets else ''}
<div class="small">Biggest hit</div><div class="chips">{tx(worst['front']['tx'], 'bot buy')}{tx(worst['victims'][0]['tx'], 'victim')}{tx(worst['back']['tx'], 'bot sell')}</div></div>"""

    rest = ops[NAMED:]
    op_loss = bars([(f"{name[o]} · {short(o)}", op[o]["loss"], color[o]) for o in ops[:NAMED]] +
                   [(f"{len(rest)} others", sum(op[o]["loss"] for o in rest), "var(--o4)")], usd, "Victims' losses by operator", 230)
    op_profit = bars([(f"{name[o]} · {short(o)}", max(0, op[o]["profit"]), color[o]) for o in ops[:NAMED]] +
                     [(f"{len(rest)} others", max(0, sum(op[o]["profit"] for o in rest)), "var(--o4)")], usd, "Profit kept by operator", 230)
    small = table([("Operator", True), ("Sandwiches", False), ("Victims lost", False), ("Kept", False), ("Active (UTC)", True)],
                  [[addr(o), n(op[o]["n"]), usd(op[o]["loss"]), usd(op[o]["profit"]),
                    f"{utc(op[o]['first'], '%b %-d %H:%M', '')} → {utc(op[o]['last'], '%b %-d %H:%M', '')}"] for o in rest])
    who_section = f"""
<section id="who"><div class="q"><div class="kicker">3 · Attackers</div><h2>Who did it?</h2>
<p class="sub">An operator is the set of wallets and contracts that its transactions tie together: wallets that sign one bot's trades,
bots with the same code, and the address its profit flows to.</p></div>
{figure("Whose sandwiches cost victims the most?", f"{name[A]} alone caused {pct(100 * op[A]['loss'] / loss)} of all losses. {n(len(ops))} operators sandwiched at least once.",
        op_loss, "<span><b>Shows:</b> money victims lost to each operator's sandwiches (USD).</span>")}
<div class="ops">{''.join(card(o) for o in ops[:NAMED])}</div>
{figure("How much did each operator keep?", f"The bots kept {usd(profit)}, {pct(100 * profit / loss)} of what victims lost. Each sandwich pays pool and launchpad fees on two trades, so much of the victims' loss never reached the bots.",
        op_profit, "<span><b>Shows:</b> what each operator's addresses gained over both legs of its sandwiches, after gas (USD).</span><span><b>Measured as:</b> every HYPE, WHYPE and USDC movement in and out of the operator's addresses inside its two transactions, from execution traces.</span>")}
<details><summary>Smaller operators</summary>{small}</details></section>"""

    # ---- 4. victims
    bucket_rows = []
    for lo, hi in LOSS_BUCKETS:
        k = sum(1 for p in loss_pcts if p >= lo and (hi is None or p < hi))
        bucket_rows.append((f"{lo}–{hi}% of the trade" if hi else f"{lo}% or more", k, None))
    app_chart = bars([(app_name(a)[:44], v[0], None) for a, v in apps[:6]] + [("Everything else", sum(v[0] for _, v in apps[6:]), "var(--o4)")],
                     usd, "Victims' losses by the app they traded through", 330)
    vict = defaultdict(lambda: [0.0, 0, 0.0])
    for s in rows:
        for v in s["victims"]:
            w = vict[d.txs[v["tx"]]["from"]]
            w[0] += s["loss_usd"] / len(s["victims"])
            w[1] += 1
            w[2] += s["volume_usd"] / len(s["victims"])
    victims_section = f"""
<section id="victims"><div class="q"><div class="kicker">4 · Victims</div><h2>Who got hurt, and how badly?</h2>
<p class="sub">{n(len(victims))} wallets were sandwiched. Losses are measured for {pct(100 * len(measured) / len(rows))} of the sandwiches; the rest could not be re-run and count as zero.</p></div>
{figure("How much does one sandwich take from a trade?",
        f"Usually a little, sometimes a lot: the median victim lost {pct(statistics.median(loss_pcts))} of the trade, and {n(sum(1 for p in loss_pcts if p >= 5))} trades lost 5% or more.",
        bars(bucket_rows, lambda v: f"{n(v)} trades", "Sandwiched trades by share of the trade lost", 200),
        "<span><b>Shows:</b> sandwiched trades by how much of the trade's value the victim lost.</span>")}
{figure("Which apps were the victims using?",
        f"Mostly one trading-bot router ({pct(100 * top_app[1][0] / loss)} of losses), but also Uniswap's own router and several unrelated apps, so the leak was not inside one app: it sat on a path they all share.",
        app_chart, "<span><b>Shows:</b> victims' losses by the contract their transaction called.</span>")}
<details><summary>Apps and largest victims</summary>
{table([("App", True), ("Contract", True), ("Trades hit", False), ("Wallets", False), ("Lost", False)],
       [[escape(app_name(a)), addr(a) if a != "7702" else "—", n(v[1]), n(len(v[2])), usd(v[0])] for a, v in apps[:8]])}
{table([("Wallet", True), ("Trades hit", False), ("Traded", False), ("Lost", False)],
       [[addr(w), n(v[1]), usd(v[2]), usd(v[0])] for w, v in sorted(vict.items(), key=lambda kv: -kv[1][0])[:10]])}
</details></section>"""

    # ---- 5. pools
    pools = defaultdict(lambda: {"loss": 0.0, "n": 0, "volume": 0.0, "ops": Counter(), "ex": None, "venue": ""})
    for s in rows:
        p = pools[s["pool"]]
        p["loss"] += s["loss_usd"]
        p["n"] += 1
        p["volume"] += s["volume_usd"]
        p["ops"][s["operator"]] += 1
        p["venue"] = d.venue(s)
        rank = (s["tier"] == "A", s["loss_usd"])
        if p["ex"] is None or rank > p["ex"][0]:
            p["ex"] = (rank, s)
    ranked = sorted(pools.items(), key=lambda kv: -kv[1]["loss"])
    top10 = 100 * sum(v["loss"] for _, v in ranked[:10]) / loss
    pool_rows = [[escape(d.pool_label(p)), v["venue"], n(v["n"]), usd(v["volume"]), usd(v["loss"]),
                  name.get(v["ops"].most_common(1)[0][0], short(v["ops"].most_common(1)[0][0])),
                  tx(v["ex"][1]["front"]["tx"], "buy") + tx(v["ex"][1]["victims"][0]["tx"], "victim") + tx(v["ex"][1]["back"]["tx"], "sell")]
                 for p, v in ranked[:15]]
    where = f"""
<section id="where"><div class="q"><div class="kicker">5 · Targets</div><h2>Where did it happen?</h2></div>
{figure("Which tokens were targeted?",
        f"Newly launched tokens paired with HYPE on Uniswap V4. {n(len(pools))} pools were hit; the top 10 account for {pct(top10)} of losses.",
        bars([(d.pool_label(p)[:34], v["loss"], None) for p, v in ranked[:12]], usd, "Victims' losses per pool, top 12"),
        "<span><b>Shows:</b> victims' losses per pool, top 12 (USD).</span>")}
<details><summary>Top 15 pools with an example sandwich each</summary>
{table([("Pool", True), ("Venue", True), ("Sandwiches", False), ("Traded", False), ("Lost", False), ("Main operator", True), ("Example", True)], pool_rows)}
</details></section>"""

    # ---- 6. method
    tiers_n = Counter(s["tier"] for s in d.all)
    by_venue = Counter(d.venue(s) for s in rows)
    venue_table = table(
        [("Venue", True), ("Swap events", False), ("Covered", True), ("Sandwiches", False)],
        [["Uniswap V4 pools, including launchpad hooks", n(d.venues.get("Uniswap V4", 0)), "yes", n(by_venue["Uniswap V4"])],
         ["Uniswap V2/V3 pools and forks", n(d.venues.get("Uniswap V2/V3", 0)), "yes", n(by_venue["Uniswap V2/V3"])],
         ["Bin AMM pools (USDC/WHYPE, from Nov 9)", n(d.venues.get("Bin AMM pools", 0)), "yes, from token transfers", n(by_venue["Bin AMM"])],
         ["Liquidity Book pools (USDC/WHYPE, from Nov 9)", n(d.venues.get("Liquidity Book pools", 0)), "yes, from token transfers", n(by_venue["Liquidity Book"])],
         ["Aggregator <i>Exchange</i> events", n(d.venues.get("Aggregator Exchange events", 0)), "trades land in the pools above", "—"],
         ["Bonding-curve Buy / Sell events", n(d.venues.get("Bonding-curve Buy / Sell events", 0)), "too few to matter", "—"],
         ["Algebra, PancakeSwap V3, Curve, Balancer", n(d.venues.get("Algebra, PancakeSwap V3, Curve, Balancer", 0)), "nothing to cover", "—"]])
    tip_ok = sum(1 for s in d.all if s["tier"] == "A" and s["tip_signature"])
    paired = sum(1 for s in d.all if s["tier"] != "C" and s["signed_together"])
    method = f"""
<section id="method"><div class="q"><div class="kicker">6 · Method</div><h2>How do we know?</h2></div><div class="method">
<div class="q"><h3>Which venues are covered?</h3><p>Every swap event of every common AMM format, over the whole chain:</p>{venue_table}</div>
<div class="q"><h3>How are sandwiches found?</h3><ol class="tight">
<li>In each pool: a buy, then other transactions buying in the same direction, then a sell by the first buyer of about what it bought (within 5%), all within 2 blocks.</li>
<li>The buy and the sell come from the same wallet; each victim from a different one.</li>
<li>Same block: {n(tiers_n['A'] + tiers_n['B'])} sandwiches, all counted. Bot sold 1–2 blocks later: {n(tiers_n['C'])}, counted only for operators that profit from the pattern overall ({n(sum(1 for s in rows if s['tier'] == 'C'))}).</li></ol></div>
<div class="q"><h3>How do we know the bot saw the victim's transaction first?</h3>
<p>Elysium has no public mempool, yet in {n(tip_ok)} of {n(tiers_n['A'])} back-to-back cases the bot's buy pays a higher tip than the victim and its
sell no more than the victim, usually the victim's tip plus 1 wei. In {n(paired)} of {n(tiers_n['A'] + tiers_n['B'])} same-block cases the sell was signed
together with the buy (consecutive nonces). The largest bot's contract refuses to sell unless it gets back more than it paid, which only works if it already knows a buy will land in between.</p></div>
<div class="q"><h3>How are losses and profits measured?</h3><ul class="tight">
<li><b>Victim's loss:</b> the victim's exact transaction re-run with <code>eth_simulateV1</code> on the state just before the bot's buy, compared with what the victim really got.</li>
<li><b>Bot's profit:</b> both bot transactions traced with <code>debug_traceTransaction</code>; every HYPE, WHYPE and USDC movement in and out of the operator's addresses, minus gas. Swap events alone overstate it, because launchpad hooks take fees outside the event.</li>
<li><b>USD:</b> each day's median HYPE price in Elysium's own USDC pools.</li></ul></div>
<div class="q"><h3>How are wallets grouped into operators?</h3>
<p>A wallet belongs with the bot contract it calls for its trades. Bot contracts with the same code once their hard-coded addresses are blanked are joined. An address that repeatedly receives most of an operator's sell proceeds is joined as its profit address. A contract that 5 or more other wallets use for their own trades is a public app, not a bot.</p></div>
<div class="q"><h3>Out of scope</h3><ul class="tight">
<li>Sandwiches whose legs are more than 2 blocks apart, launch sniping, arbitrage and back-running.</li>
<li>Profit moved to an address the transactions don't link to the operator, or kept in tokens other than HYPE, WHYPE and USDC.</li></ul></div>
</div></section>"""

    # ---- appendix
    per_day = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for s in rows:
        x = per_day[s["day"]]
        x[0] += 1
        x[1] += s["volume_usd"]
        x[2] += s["loss_usd"]
        x[3] += s["profit_usd"]
    appendix = "<details><summary>Day by day (UTC)</summary>" + table(
        [("Day", True), ("HYPE price", False), ("Sandwiches", False), ("Trades sandwiched", False), ("Victims lost", False), ("Bots kept", False)],
        [[dt.date.fromisoformat(k).strftime("%b %-d"), f"${d.prices[k]:,.2f}", n(v[0]), usd(v[1]), usd(v[2]), usd(v[3])]
         for k, v in sorted(per_day.items())]) + "</details>"

    css = open(os.path.join(os.path.dirname(__file__), "report.css")).read()
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sandwich attacks on Elysium</title><style>{css}</style></head><body><main>
<header class="q"><div class="kicker">Elysium mainnet · MEV report</div><h1>Sandwich attacks on Elysium</h1>
<p class="sub">From the first trades to {utc(d.head['ts'])}. Every address, transaction and block links to the Elysium explorer.</p>
<nav><a href="#what">What a sandwich is</a><a href="#when">When</a><a href="#who">Who</a><a href="#victims">Victims</a><a href="#where">Where</a><a href="#method">Method</a></nav></header>
{tldr}{walk}{when}{who_section}{victims_section}{where}{method}{appendix}
</main></body></html>"""
    os.makedirs("docs", exist_ok=True)
    open("docs/index.html", "w").write(page)
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
            w.writerow([utc(s["t"], "%Y-%m-%d %H:%M:%S", ""), s["front"]["b"], s["tier"], s["operator"], name.get(s["operator"], ""),
                        s["wallet"], s["bot"] or "", d.venue(s), s["pool"], d.pool_label(s["pool"]),
                        s["front"]["tx"], " ".join(v["tx"] for v in s["victims"]), s["back"]["tx"],
                        f"{s['victim_hype'] / 1e18:.6f}", "" if s["loss"] is None else f"{s['loss'] / 1e18:.6f}",
                        f"{s['loss_usd']:.2f}" if s["loss"] is not None else "", "" if s["profit"] is None else f"{s['profit'] / 1e18:.6f}",
                        f"{s['profit_usd']:.2f}"])
