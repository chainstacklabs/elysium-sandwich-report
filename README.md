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

[Elysium](https://elysium.kinetiq.xyz/docs/what-is-elysium) is an Arbitrum Nitro chain that settles to HyperEVM. It
opened as a private mainnet with early-access trading and went public on Nov 9, 2026. Bots
[sandwiched](https://ethereum.org/en/developers/docs/mev/#mev-examples-sandwich-trading) thousands of trades, almost
all before the public launch. This repository measures who did it and what the victims lost, from chain data alone.

**Read the report:** [chainstacklabs.github.io/elysium-sandwich-report](https://chainstacklabs.github.io/elysium-sandwich-report/)

## What's here

- [`docs/index.html`](docs/index.html) — the report: findings, operators, victims, targets and method
- [`data/sandwiches.csv`](data/sandwiches.csv) — every sandwich, one row each, with transactions, operator, loss and
  profit
- [`sandwiches/`](sandwiches) — the Python package that builds both from chain data

## Run

Needs Python 3.11+, [uv](https://docs.astral.sh/uv/) and an Elysium mainnet archive endpoint with `debug_*` methods.

```bash
export ELYSIUM_RPC_URL=https://...
uv run python -m sandwiches              # all steps; re-runs fetch only new blocks
uv run python -m sandwiches report       # rebuild docs/index.html and data/sandwiches.csv from the cache
```

A full run from block 1 makes about 7,000 batched requests and takes about 40 minutes. The cache in `cache/` holds the
swaps, transaction details, traces and simulations.

## License

[Apache 2.0](LICENSE)
