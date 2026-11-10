<img width="1200" alt="Labs" src="https://user-images.githubusercontent.com/99700157/213291931-5a822628-5b8a-4768-980d-65f324985d32.png">

<p>
 <h3 align="center">Chainstack is the leading suite of services connecting developers with Web3 infrastructure</h3>
</p>

<p align="center">
  • <a target="_blank" href="https://chainstack.com/">Homepage</a> •
  <a target="_blank" href="https://chainstack.com/protocols/">Supported protocols</a> •
  <a target="_blank" href="https://chainstack.com/blog/">Chainstack blog</a> •
  <a target="_blank" href="https://docs.chainstack.com/quickstart/">Blockchain API reference</a> • <br>
  • <a target="_blank" href="https://console.chainstack.com/user/account/create">Start for free</a> •
</p>

# Sandwich attacks on Elysium

[Elysium](https://elysium.kinetiq.xyz/docs/what-is-elysium) is an Arbitrum Nitro chain settling to HyperEVM. It ran as a
private mainnet with early access to trading before opening to the public on 2026-11-09 13:00 UTC. Elysium has no
public mempool, yet bots [sandwiched](https://ethereum.org/en/developers/docs/mev/#mev-examples-sandwich-trading)
thousands of trades during that period. This repository measures who did it, what victims lost and how, from chain
data alone.

**Report:** [chainstacklabs.github.io/elysium-sandwich-report](https://chainstacklabs.github.io/elysium-sandwich-report/)
(source: [`docs/index.html`](docs/index.html)). Every sandwich is listed in [`data/sandwiches.csv`](data/sandwiches.csv).

## Findings

Data to 2026-11-10 10:37 UTC (block 3,943,687). USD at each day's HYPE price in Elysium's USDC pools.

| | |
|---|---|
| Trades sandwiched | 13,036 trades worth $4.0M, by 4,312 wallets |
| Victims lost | $153.4k (2,101 HYPE); median 3.3% of a trade |
| Bots kept, after gas | $86.0k (1,178 HYPE); the rest went to pool and launchpad fees |
| After the public launch | 135 sandwiches, $1.1k |
| Hit hardest | users of one trading-bot router, [`0x9b60…3333`](https://elysium.kinetiq.xyz/address/0x9b601cc44bb8405da290d70bdcad3a3767bd3333): 65% of all losses |

**Timeline (UTC)**

- **Nov 4 21:43:** first sandwich. Until Nov 8, bots mostly sell 1–2 blocks after the victim.
- **Nov 7 05:53:** operator A starts with its own bot contract and becomes the largest attacker.
- **Nov 8 23:04:** operator A starts placing its buy, the victim and its sell in one block, ordered by tip: its buy pays
  the victim's tip plus 1 wei and its sell pays zero. Losses peak.
- **Nov 9 13:00:** public launch. Operator A stops at 15:09. Sandwiching drops to a few small bots.

**Operators**

| | Wallets and bots | Sandwiches | Victims lost | Kept | Active (UTC) |
|---|---|---|---|---|---|
| A | wallets [`0x20e8…f3a9`](https://elysium.kinetiq.xyz/address/0x20e8c116c608d1de12bce6f8ac4dd566fe14f3a9), [`0xc64a…1ca0`](https://elysium.kinetiq.xyz/address/0xc64ae9e5122ff5e39d53bee2667ed68bd6471ca0); bots [`0x6761…94e9`](https://elysium.kinetiq.xyz/address/0x6761a8cd4f51721c7e183b11a1a0b8829fab94e9), [`0x39fd…4bf9`](https://elysium.kinetiq.xyz/address/0x39fdb8e2a6239dc2626650dc8768c1ac9bea4bf9) | 6,822 | $77.7k | $44.1k | Nov 7 05:53 – Nov 9 15:09 |
| B | wallet [`0x6666…6666`](https://elysium.kinetiq.xyz/address/0x6666626a7b6aa571d96303b4f75b0e6b43b66666), funds held in app contract [`0xaa6f…48ad`](https://elysium.kinetiq.xyz/address/0xaa6f64cabcdb3393b0665487f404709f750c48ad) | 1,997 | $29.3k | $15.6k | Nov 7 13:17 – Nov 9 02:47 |
| C | wallet [`0x8fc2…8275`](https://elysium.kinetiq.xyz/address/0x8fc298f28d69aaa4f60afdd12ddf13bc8abd8275); bot [`0xb85f…b8f2`](https://elysium.kinetiq.xyz/address/0xb85f7425638672f0147937a07b2c7a83ed42b8f2) | 2,248 | $25.4k | $13.7k | Nov 6 07:02 – Nov 7 06:27 |
| D | 11 wallets; bot [`0x55e2…caea`](https://elysium.kinetiq.xyz/address/0x55e2ce458e2db8560f457625c0937aeb7fbecaea) | 1,448 | $12.3k | $7.1k | Nov 7 05:37 – Nov 9 06:29 |
| E | wallet [`0x9999…9999`](https://elysium.kinetiq.xyz/address/0x9999751e808ef8675531adff1d0ced16dde59999); bot [`0x120d…ea05`](https://elysium.kinetiq.xyz/address/0x120d57688cf26dd421045342a94b219dab1cea05) | 182 | $6.2k | $4.2k | Nov 4 21:43 – Nov 6 23:15 |

**Example** in [block 2,784,446](https://elysium.kinetiq.xyz/block/2784446): operator A's
[buy](https://elysium.kinetiq.xyz/tx/0xd1933d28ca7c8d1760a3365b5a1389f8125ec390e4d41994b295e4798e056292),
the [victim's 3 HYPE buy](https://elysium.kinetiq.xyz/tx/0x08531d7f54860652f8ff1aa5d93a830ae83558bc8c0a9b6d61d6e3254e1f7edc)
and the bot's [sell](https://elysium.kinetiq.xyz/tx/0xbaaa063e79161b75f991278d89c01779bfd3ee7c92591ef4627ba768c0e584af).
The victim received 9.1% fewer tokens (0.268 HYPE) and the bot kept 0.083 HYPE.

Victims came through several unrelated apps, including Uniswap's Universal Router, so the transactions leaked on a
path they share rather than inside one app.

## Methodology

1. **Swaps.** Every swap log from block 1: Uniswap V2, V3 and V4 pools and forks that emit the same events
   (1.97M swaps), plus Bin AMM and Liquidity Book pools, whose swaps are rebuilt from token transfers in and out of
   the pool. A search for other AMM formats (Algebra, PancakeSwap V3, Curve, Balancer, bonding curves) found nothing
   else with meaningful volume.
2. **Candidates.** In one pool: a buy, then other transactions buying in the same direction, then a sell by the first
   buyer of about what it bought (within 5%), all within 2 blocks.
3. **Verification.** The buy and the sell come from the same wallet, the victims from other wallets. Same-block cases
   are counted. Cases where the sell lands 1–2 blocks later are counted only for operators that profit from that
   pattern overall.
4. **Victims' loss.** Each victim transaction is re-run with `eth_simulateV1` on the state just before the bot's
   buy. The loss is the difference from what the victim really received.
5. **Bots' profit.** Both bot transactions are traced with `debug_traceTransaction`. Profit is the net HYPE, WHYPE and
   USDC that reached the operator's addresses, minus gas. Swap events alone overstate it, because launchpad hooks
   take fees outside the event.
6. **Operators.** A wallet is grouped with the bot contract it calls, bots with identical code (hard-coded owner
   blanked) are joined, and so is the address that repeatedly receives an operator's sell proceeds. A contract that
   5 or more other wallets trade through is an app, not a bot.
7. **Prices and time.** Daily median HYPE/USD from swaps in Elysium's HYPE/USDC pools. Block times from the first
   block of each UTC hour.

## Out of scope

- Sandwiches with more than 2 blocks between the bot's buy and sell.
- Launch sniping, arbitrage and back-running.
- Profit an operator moves to an address its transactions don't link to, or keeps in tokens other than HYPE, WHYPE
  and USDC.
- Blocks 87,023–142,652 (2026-10-31), a team stress test with no trading.

## Run

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and an Elysium mainnet archive endpoint with `debug_*` methods.

```bash
export ELYSIUM_RPC_URL=https://...
uv run python -m sandwiches              # all steps; re-runs fetch only new blocks
uv run python -m sandwiches report       # rebuild docs/index.html and data/sandwiches.csv from the cache
```

A full run from block 1 makes about 7,000 batched requests and takes about 40 minutes. The cache in `cache/` holds
the swaps, transaction details, traces and simulations.

| Step | Module | Does |
|---|---|---|
| `scan` | `scan.py` | swap logs into `cache/logs/` |
| `clock` | `clock.py` | first block of every UTC hour |
| `detect` | `detect.py` | candidates, transaction checks, tiers, bots |
| `prices` | `clock.py` | daily HYPE/USD |
| `measure` | `money.py`, `operators.py` | traces, operators, profit, victims' loss |
| `venues` | `venues.py` | swap events of other AMM formats |
| `report` | `report.py`, `charts.py` | `docs/index.html`, `data/sandwiches.csv` |

## License

[Apache 2.0](LICENSE)
