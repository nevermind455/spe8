#!/usr/bin/env python3
"""Does a SIG PRICE reversal beat the price the market charges for it?

    python reversal_edge.py                 # the active journal
    python reversal_edge.py --min-bps 3     # only reversals this far past the open
    python reversal_edge.py some_journal.csv

Read-only. It replays signal_journal's recorded samples and asks one
question: at the moment SIG PRICE flips sides, is the NEW side right more
often than the ask you would have paid for it?

Why this and not a paper run. A reversal entry fires extra trades, and at
this venue a taker pays theta*(1-p) of notional - about 2.4 points at the
prices this bot trades. So a reversal must be better than the quote by MORE
than the fee before it is worth taking at all. Measuring that on recorded
samples costs nothing and risks nothing; finding out by trading costs the
fee every time the answer is no.

A note on what "past the open" means here. SIG PRICE is "is BTC above where
this round opened", so a reversal IS the price crossing the open: at the
crossing the distance from the open is zero by construction. --min-bps
therefore filters on how far past the open the price has ALREADY moved when
the flip is observed, which is what separates a real turn from a tick
sitting on the line.
"""
from __future__ import annotations

import collections
import csv
import json
import math
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).parent
JOURNAL = ROOT / "signal_journal.csv"
WINNERS = ROOT / "signal_journal_winners.json"
# The taker fee is theta * (1 - price) of notional. Any edge below this is
# not an edge, it is a slower way to pay the venue.
THETA = 0.07


def _f(row, key):
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _side(row):
    """SIG PRICE: is Binance above where the round opened?"""
    strike, now = _f(row, "bn_strike"), _f(row, "bn_now")
    if strike is None or now is None or strike <= 0 or now == strike:
        return None, None
    bps = abs(now - strike) / strike * 10_000.0
    return ("UP" if now > strike else "DOWN"), bps


def _ask(row, side):
    return _f(row, "up_ask" if side == "UP" else "dn_ask")


def load(path: pathlib.Path):
    """Samples grouped by round, in time order, with the round's winner."""
    if not path.is_file():
        print(f"no journal at {path}")
        return {}, {}
    winners = json.loads(WINNERS.read_text()) if WINNERS.is_file() else {}
    rounds = collections.defaultdict(list)
    with path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rounds[str(row.get("window"))].append(row)
    for window in rounds:
        rounds[window].sort(key=lambda r: _f(r, "wall") or 0.0)
    return rounds, winners


def stat_line(label, calls, wins, paid, rounds):
    """One row, with the fee it has to clear and the sample it rests on."""
    if not calls:
        print(f"{label:<26}{0:>7}")
        return
    acc = wins / calls * 100.0
    implied = statistics.mean(paid) * 100.0
    fee = THETA * (1.0 - statistics.mean(paid)) * 100.0
    print(f"{label:<26}{calls:>7}{len(rounds):>8}{acc:>10.1f}%{implied:>10.1f}%"
          f"{acc - implied:>+8.1f}{fee:>7.1f}{acc - implied - fee:>+9.1f}")


def report(rounds, winners, min_bps):
    base_calls = base_wins = 0
    base_paid, base_rounds = [], set()
    rev = collections.defaultdict(
        lambda: {"calls": 0, "wins": 0, "paid": [], "rounds": set()})
    by_time = collections.defaultdict(
        lambda: {"calls": 0, "wins": 0, "paid": [], "rounds": set()})

    for window, samples in rounds.items():
        winner = winners.get(window)
        if winner not in ("UP", "DOWN"):
            continue
        previous = None
        for row in samples:
            side, bps = _side(row)
            if side is None:
                continue
            ask = _ask(row, side)
            if ask is None or not 0.0 < ask < 1.0:
                previous = side
                continue

            # Baseline: every sample, traded or not. This is what the journal
            # already reports, repeated here so the reversal rows have
            # something to be better than.
            base_calls += 1
            base_wins += side == winner
            base_paid.append(ask)
            base_rounds.add(window)

            reversal = previous is not None and side != previous
            previous = side
            if not reversal or bps < min_bps:
                continue
            bucket = ("<2 bps" if bps < 2 else "2-5 bps" if bps < 5
                      else "5-10 bps" if bps < 10 else ">=10 bps")
            for cell in (rev[bucket], rev["ALL reversals"]):
                cell["calls"] += 1
                cell["wins"] += side == winner
                cell["paid"].append(ask)
                cell["rounds"].add(window)
            left = _f(row, "secs_left")
            if left is not None:
                label = ("300-240s" if left >= 240 else "240-180s" if left >= 180
                         else "180-120s" if left >= 120 else "120-60s"
                         if left >= 60 else "60-0s")
                cell = by_time[label]
                cell["calls"] += 1
                cell["wins"] += side == winner
                cell["paid"].append(ask)
                cell["rounds"].add(window)

    resolved = len(base_rounds)
    print(f"{base_calls} samples across {resolved} resolved rounds"
          f"   (min move {min_bps} bps)\n")
    header = (f"{'':<26}{'calls':>7}{'rnds':>8}{'accuracy':>11}{'implied':>11}"
              f"{'edge':>8}{'fee':>7}{'net':>9}")
    print("=" * 78)
    print("DOES A REVERSAL BEAT THE PRICE CHARGED FOR IT")
    print("=" * 78)
    print(header)
    stat_line("every sample (baseline)", base_calls, base_wins, base_paid,
              base_rounds)
    cell = rev.get("ALL reversals")
    if cell:
        stat_line("at a reversal", cell["calls"], cell["wins"], cell["paid"],
                  cell["rounds"])
    print("\n  edge = accuracy minus the price paid, in points.")
    print("  net  = edge minus the taker fee. Only net can make money.")

    if len(rev) > 1:
        print("\n" + "=" * 78)
        print("BY HOW FAR PAST THE OPEN THE PRICE HAD MOVED")
        print("=" * 78)
        print(header)
        for bucket in ("<2 bps", "2-5 bps", "5-10 bps", ">=10 bps"):
            cell = rev.get(bucket)
            if cell:
                stat_line(bucket, cell["calls"], cell["wins"], cell["paid"],
                          cell["rounds"])

    if by_time:
        print("\n" + "=" * 78)
        print("BY TIME REMAINING IN THE ROUND")
        print("=" * 78)
        print(header)
        for label in ("300-240s", "240-180s", "180-120s", "120-60s", "60-0s"):
            cell = by_time.get(label)
            if cell:
                stat_line(label, cell["calls"], cell["wins"], cell["paid"],
                          cell["rounds"])

    print("\n" + "=" * 78)
    print("HOW BIG A SAMPLE BEFORE ANY OF THIS MEANS ANYTHING")
    print("=" * 78)
    print(f"  resolved ROUNDS       {resolved}   <- this is the sample size")
    if resolved:
        se = math.sqrt(0.25 / resolved) * 100.0
        print(f"  1 SE on a win rate    +/-{se:.1f} points")
        print(f"  readable at ~2 SE     {2 * se:.1f} points")
        print(f"  for a 3-point edge    about {int(0.25 / (0.015 ** 2)):,} rounds")
    print("\n  Reversals inside one round share that round's outcome, so the")
    print("  rounds column - not calls - is what the error bar rests on.")


def main(argv) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    min_bps = 0.0
    for arg in argv[1:]:
        if arg.startswith("--min-bps"):
            try:
                min_bps = float(arg.split("=", 1)[1]) if "=" in arg else 0.0
            except ValueError:
                print("--min-bps needs a number, e.g. --min-bps=3")
                return 1
    path = pathlib.Path(args[0]) if args else JOURNAL
    rounds, winners = load(path)
    if not rounds:
        return 1
    if not winners:
        print("no resolved winners yet - run: signal_journal.py resolve")
        return 1
    report(rounds, winners, min_bps)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
