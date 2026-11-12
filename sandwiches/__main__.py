"""python -m sandwiches [step ...]   (no step = all, in order)

Each step reads and writes ./cache, so a re-run only fetches what is new.
"""

import sys

from . import clock, copies, detect, inbox, money, operators, report, rules, scan, venues
from .chain import load, save


def prices():
    clock.prices(scan.rows(), load("pool_tokens.json"))


def measure():
    """Trace the bots' transactions, group operators, then price both sides of every sandwich."""
    found = load("sandwiches.json")
    legs = money.trace(found)
    tokens, prices_, clk = load("pool_tokens.json"), load("prices.json"), clock.Clock()
    ops = operators.run(found, legs, prices_, clk)
    money.profit(found, legs, ops, prices_, clk)
    inbox.prove([s for s in found if s["tier"] == "C"])  # an inbox victim's block holds no sequencer transactions
    money.count(found)
    money.simulate([s for s in found if s["counted"]], tokens, prices_, clk)
    for s in found:
        s.setdefault("loss", None)
    save("sandwiches.json", found)


STEPS = {
    "scan": scan.run,  # swap logs -> cache/logs
    "clock": clock.build,  # first block of every UTC hour
    "detect": detect.run,  # pool tokens, candidates, transaction checks, tiers
    "prices": prices,  # daily HYPE/USD from Elysium's USDC pools
    "measure": measure,  # profit from traces, operators, victims' loss from simulation
    "copies": copies.run,  # victims' transactions also posted to the inbox on HyperEVM
    "rules": rules.run,  # how the count changes with the sell window
    "venues": venues.run,  # swap events of other AMM formats
    "report": report.build,  # docs/index.html and data/sandwiches.csv
}

if __name__ == "__main__":
    steps = sys.argv[1:] or list(STEPS)
    for step in steps:
        if step not in STEPS:
            sys.exit(f"unknown step {step!r}; steps: {', '.join(STEPS)}")
    for step in steps:
        print(f"== {step}", flush=True)
        STEPS[step]()
