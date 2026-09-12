"""Self-test for band_backtest.py's journal path.

A backtester that cannot say NO is worthless, and one that says YES for the
wrong reason is worse. This writes synthetic signal_journal.csv /
signal_winners.json files with a KNOWN answer and checks the harness returns
it - in both directions.

The market is simulated as a digital on a driftless underlying, so quotes are
calibrated by construction and settlement comes from the SAME path. Drawing a
winner independently of the price would make any schedule look mispriced for
free.

The single knob is delta, a CONSTANT price concession on the underdog leg,
applied so the two asks still sum to 1 + 2*half_spread:

    underdog ask = true_p - delta + half_spread
    favourite ask = (1 - true_p) + delta + half_spread

    delta > 0   UNDERDOGS are cheap by (delta - half_spread) everywhere
    delta = 0   nothing is cheap; every buy pays the spread
    delta < 0   FAVOURITES are cheap by the same amount

A constant offset matters: shading the quoted probability by a percentage
instead makes the planted edge vanish at one end of a band and reverse at the
other, so a band spanning that point scores near zero however good the
harness is. The first version of this file did exactly that and reported
three false failures.

Each case runs a single-purpose band schedule so the expected sign is
unambiguous. A mixed schedule buys favourites in one window and underdogs in
another, and the two cancel - which is itself worth knowing.

    python3 band_backtest_selftest.py
"""
from __future__ import annotations

import csv
import json
import math
import pathlib
import random
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).parent
ROUND_SECONDS = 300
SAMPLE_EVERY = 2.0
SIGMA_ANNUAL = 0.55
YEAR_SECONDS = 365 * 24 * 3600
HALF_SPREAD = 0.015          # ask sum lands near 1.03, as observed live

FAVOURITE_BAND = "300:30:0.62:0.80:10"      # clear of the inversion zone
STRADDLE_BAND = "300:30:0.55:0.75:10"       # floor inside it, on purpose
UNDERDOG_BAND = "300:30:0.25:0.45:10"


def phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def simulate_round(rng, window, delta):
    sigma_s = SIGMA_ANNUAL / math.sqrt(YEAR_SECONDS)
    spot = strike = 100_000.0
    rows = []
    elapsed = 0.0
    while elapsed + SAMPLE_EVERY < ROUND_SECONDS:
        spot *= math.exp(sigma_s * math.sqrt(SAMPLE_EVERY) * rng.gauss(0.0, 1.0)
                         - 0.5 * sigma_s ** 2 * SAMPLE_EVERY)
        elapsed += SAMPLE_EVERY
        remaining = ROUND_SECONDS - elapsed
        true_up = phi(math.log(spot / strike) / (sigma_s * math.sqrt(remaining)))
        true_up = min(max(true_up, 0.02), 0.98)
        if true_up < 0.5:                       # UP is the underdog
            up_ask = true_up - delta + HALF_SPREAD
            dn_ask = (1.0 - true_up) + delta + HALF_SPREAD
        else:                                   # DOWN is the underdog
            dn_ask = (1.0 - true_up) - delta + HALF_SPREAD
            up_ask = true_up + delta + HALF_SPREAD
        up_ask = min(max(up_ask, 0.01), 0.99)
        dn_ask = min(max(dn_ask, 0.01), 0.99)
        rows.append({
            "wall": window + elapsed,
            "window": window,
            "secs_left": remaining,
            "cl_strike": strike, "cl_now": round(spot, 2),
            "bn_strike": strike, "bn_now": round(spot, 2),
            "up_ask": round(up_ask, 4), "up_bid": round(up_ask - 0.01, 4),
            "dn_ask": round(dn_ask, 4), "dn_bid": round(dn_ask - 0.01, 4),
            "up_bid_vol": 100.0, "up_ask_vol": 100.0,
        })
    return rows, ("UP" if spot >= strike else "DOWN")


def build(path, rounds, delta, seed):
    rng = random.Random(seed)
    fields = ["wall", "window", "secs_left", "cl_strike", "cl_now", "bn_strike",
              "bn_now", "up_ask", "up_bid", "dn_ask", "dn_bid",
              "up_bid_vol", "up_ask_vol"]
    winners = {}
    with (path / "signal_journal.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for i in range(rounds):
            window = 1_700_000_000 + i * ROUND_SECONDS
            rows, winner = simulate_round(rng, window, delta)
            writer.writerows(rows)
            winners[str(window)] = winner
    (path / "signal_journal_winners.json").write_text(json.dumps(winners))


def write_paper_fixtures(path):
    """Give the harness a clean, EMPTY paper fill history.

    BUGFIX: this used to copy paper_orders.jsonl and paper_ledger.json out of
    the repo root. Both are gitignored runtime artifacts written by actually
    running the bot, so on a fresh clone run_case died with FileNotFoundError
    before a single case executed - the self-test only passed on a machine
    that had already traded.

    Empty is the correct fixture, not a convenience: every case here asserts
    on section 4, the JOURNAL backtest, which is built from the synthetic
    journal `build()` writes below. Section 3 replays real paper fills, and
    seeding it with invented ones would put fabricated trades into the same
    report the assertions read. An empty history makes section 3 render
    "NO DATA - untestable", which is exactly what it should say here.
    """
    (path / "paper_orders.jsonl").write_text("", encoding="utf-8")
    (path / "paper_ledger.json").write_text(
        json.dumps({"positions": {}}), encoding="utf-8")


def run_case(name, delta, bands, rounds, seed, expect):
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        shutil.copy(ROOT / "band_backtest.py", tmp / "band_backtest.py")
        write_paper_fixtures(tmp)
        build(tmp, rounds, delta, seed)
        proc = subprocess.run(
            [sys.executable, "band_backtest.py", "--bands", bands],
            cwd=tmp, capture_output=True, text=True, timeout=600)
        block = proc.stdout.split("4. JOURNAL BACKTEST")[-1]
        per100 = tval = None
        for line in block.splitlines():
            if "net PnL" in line:
                tval = float(line.split("t =")[1].strip())
            if "net per $100" in line:
                per100 = float(line.rsplit("$", 1)[1].strip())
        if per100 is None or tval is None:
            print(f"  FAIL  {name}: harness produced no result")
            print(proc.stdout[-800:], proc.stderr[-400:])
            return False
        ok = expect(per100, tval)
        print(f"  {'pass' if ok else 'FAIL'}  {name:<42} "
              f"net/$100 = {per100:+7.2f}   t = {tval:+6.2f}")
        return ok
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CASES = [
    # An efficient market must LOSE the spread plus the fee, not break even.
    ("A  efficient / favourite band  -> loss", 0.0, FAVOURITE_BAND, 2000, 1,
     lambda p, t: -9.0 < p < 0.0 and t < 0),
    ("B  efficient / underdog band   -> loss", 0.0, UNDERDOG_BAND, 2000, 2,
     lambda p, t: -16.0 < p < 0.0 and t < 0),
    # Underdogs cheap: the underdog band must find it, the favourite band
    # must lose by the same mechanism.
    ("C  underdogs cheap / dog band  -> EDGE", 0.05, UNDERDOG_BAND, 6000, 3,
     lambda p, t: p > 2.0 and t > 2.0),
    ("D  underdogs cheap / fav band  -> LOSS", 0.05, FAVOURITE_BAND, 2000, 4,
     lambda p, t: p < -2.0 and t < -2.0),
    # The mirror, so a harness biased toward one side gets caught.
    ("E  favourites cheap / fav band -> EDGE", -0.05, FAVOURITE_BAND, 6000, 5,
     lambda p, t: p > 2.0 and t > 2.0),
    # A band floor sitting inside [0.50, 0.50 + skew + half_spread] buys the
    # leg quoted highest, which under an asymmetric book is NOT always the
    # leg more likely to win. Here that inversion eats a real +3.0 edge and
    # turns it negative. This case exists to keep that documented.
    ("G  same edge, floor 0.55 -> eaten", -0.05, STRADDLE_BAND, 6000, 7,
     lambda p, t: p < 2.0),
    ("F  favourites cheap / dog band -> LOSS", -0.05, UNDERDOG_BAND, 2000, 6,
     lambda p, t: p < -2.0 and t < -2.0),
]


def main():
    print("band_backtest journal path - self test\n")
    results = [run_case(name, delta, bands, rounds, seed, expect)
               for name, delta, bands, rounds, seed, expect in CASES]
    print()
    passed = sum(results)
    print(f"{passed} passed, {len(results) - passed} failed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
