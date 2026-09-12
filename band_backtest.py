"""Test a PHASE1_BANDS schedule before running it.

Three tests, in order of how much you should trust them.

  1. MECHANICS   deterministic. No data needed. Works out, from the ask sum
                 alone, which leg each band actually buys and which part of
                 the band can never fill. This is arithmetic, not statistics,
                 so it is the only part of this tool that cannot be noise.

  2. FEES        deterministic. Fee for a fixed notional is
                 notional * theta * (1 - p), so a band's price range fixes
                 its drag before a single trade happens.

  3. REPLAY      statistical, and CENSORED. Replays a schedule against the
                 fills in paper_orders.jsonl. Those fills were selected by
                 whatever bands were live at the time, so a proposed band
                 that sits outside the old one has NO observations and is
                 reported as untestable rather than as zero.

  4. JOURNAL     the real test. Needs signal_journal.csv + signal_winners.json
                 from `python signal_journal.py record` / `resolve`. That file
                 records both legs' asks every sample whether or not a trade
                 happened, so it is uncensored and any schedule can be tested
                 against it. Skipped if the journal is absent.

Usage:
    python3 band_backtest.py
    python3 band_backtest.py --bands "300:240:0.50:0.60,240:180:0.35:0.50,..."
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import pathlib
import statistics as st

ROOT = pathlib.Path(__file__).parent
THETA = 0.07
BET_SIZE = 2.50
VENUE_MIN_SHARES = 5.0
ROUND_SECONDS = 300
DEFAULT_INTERVAL = 12.0

OLD = "300:240:0.35:0.45,240:180:0.30:0.40,180:120:0.40:0.50,120:60:0.55:0.75:8"
NEW = "300:240:0.50:0.60,240:180:0.35:0.50,180:120:0.40:0.55,120:60:0.55:0.75:8"


def parse_bands(raw):
    out = []
    for chunk in raw.split(","):
        parts = chunk.strip().split(":")
        start, end = int(parts[0]), int(parts[1])
        lo, hi = float(parts[2]), float(parts[3])
        interval = float(parts[4]) if len(parts) == 5 else DEFAULT_INTERVAL
        out.append((start, end, lo, hi, interval))
    return sorted(out, key=lambda b: -b[0])


def band_for(bands, secs_left):
    for start, end, lo, hi, interval in bands:
        if end < secs_left <= start:
            return start, end, lo, hi, interval
    return None


# ------------------------------------------------------------- 1. mechanics

def mechanics(lo, hi, ask_sum):
    """What a band does, given both legs' asks sum to `ask_sum`.

    main_bot skips when BOTH legs sit inside the band ("priced as
    arbitrage"), so part of a band can be structurally dead. Both legs are
    in band when ask_up lies in [max(lo, S-hi), min(hi, S-lo)]; that interval
    is non-empty exactly when 2*lo < S < 2*hi.
    """
    dead_lo, dead_hi = max(lo, ask_sum - hi), min(hi, ask_sum - lo)
    has_dead = dead_hi > dead_lo
    live = []
    if has_dead:
        if lo < dead_lo:
            live.append((lo, dead_lo))
        if dead_hi < hi:
            live.append((dead_hi, hi))
    else:
        live.append((lo, hi))
    return (dead_lo, dead_hi) if has_dead else None, live


def stake_and_shares(price):
    """main_bot orders BET_SIZE dollars; the broker sizes UP to the 5-share
    venue minimum, so any price above BET_SIZE/5 quietly stakes more."""
    shares = max(BET_SIZE / price, VENUE_MIN_SHARES)
    return shares * price, shares


def report_mechanics(name, bands, sums=(1.01, 1.03, 1.05)):
    print("=" * 88)
    print(f"1. MECHANICS - {name}   (deterministic; S = ask_up + ask_down)")
    print("=" * 88)
    print(f"  {'window':<11}{'band':<13}{'S':>6}{'dead zone':>14}"
          f"{'actually buys':>22}{'stake/fill':>12}")
    for start, end, lo, hi, _ in bands:
        for ask_sum in sums:
            dead, live = mechanics(lo, hi, ask_sum)
            desc = " + ".join(f"{a:.2f}-{b:.2f}" for a, b in live) or "NOTHING"
            mid = ask_sum / 2
            tags = set()
            for a, b in live:
                tags.add("favourite" if (a + b) / 2 > mid else "underdog")
            side = "/".join(sorted(tags)) if tags else "-"
            stakes = [stake_and_shares(p)[0] for a, b in live for p in (a, b)]
            srange = (f"${min(stakes):.2f}-{max(stakes):.2f}" if stakes else "-")
            print(f"  {f'{start}-{end}s':<11}{f'{lo:.2f}-{hi:.2f}':<13}{ask_sum:>6.2f}"
                  f"{(f'{dead[0]:.2f}-{dead[1]:.2f}' if dead else '-'):>14}"
                  f"{desc + ' ' + side:>22}{srange:>12}")
        print()


# ------------------------------------------------------------------ 2. fees

def report_fees(name, bands, ask_sum=1.03):
    print("=" * 88)
    print(f"2. FEE ARITHMETIC - {name}   (deterministic, at S={ask_sum})")
    print("=" * 88)
    print(f"  {'window':<11}{'live range':<16}{'mid':>7}{'fee %stake':>12}"
          f"{'needs':>10}{'shares':>9}")
    for start, end, lo, hi, _ in bands:
        _, live = mechanics(lo, hi, ask_sum)
        if not live:
            print(f"  {f'{start}-{end}s':<11}{'NOTHING FILLS':<16}")
            continue
        for a, b in live:
            mid = (a + b) / 2
            _, shares = stake_and_shares(mid)
            print(f"  {f'{start}-{end}s':<11}{f'{a:.2f}-{b:.2f}':<16}{mid:>7.3f}"
                  f"{100 * THETA * (1 - mid):>11.2f}%"
                  f"{100 * THETA * mid * (1 - mid):>9.2f}pp{shares:>9.2f}")
    print("\n  'needs' is the extra win-rate accuracy, in percentage points,")
    print("  required just to cover the fee: break-even = p + theta*p*(1-p).\n")


# ---------------------------------------------------------------- 3. replay

def load_fills():
    """Settled paper fills for the censored replay, or [] when there are none.

    BUGFIX: this opened both paper runtime files unconditionally. They are
    gitignored (they are produced by running the bot), so on a fresh clone
    the whole tool died with FileNotFoundError before printing anything -
    including sections 1, 2 and 4, none of which need a fill history at all.
    Section 4 already degrades to an explicit SKIP when its journal is
    missing; this now does the same, and report_replay already renders an
    empty list as "NO DATA - untestable" per window.
    """
    audit = ROOT / "paper_orders.jsonl"
    ledger_path = ROOT / "paper_ledger.json"
    missing = [p.name for p in (audit, ledger_path) if not p.exists()]
    if missing:
        print(f"  (no paper fill history: {', '.join(missing)} not found; "
              f"the censored replay below has nothing to sample)\n")
        return []
    orders = [json.loads(line) for line in open(audit)]
    filled = [o for o in orders if o["status"] == "FILLED"]
    ledger = json.load(open(ledger_path))["positions"]
    payout = {t: (p["payout_per_share"] if p.get("settled") else None)
              for t, p in ledger.items()}
    out = []
    for o in filled:
        pps = payout.get(o["token_id"])
        if pps is None:
            continue
        o["payout_per_share"] = pps
        o["secs_left"] = ROUND_SECONDS - (o["wall"] % ROUND_SECONDS)
        o["stake"] = o["shares"] * o["average_price"]
        o["net"] = o["shares"] * pps - o["stake"] - o["fee"]
        out.append(o)
    return out


def tstat(values):
    if len(values) < 2:
        return float("nan")
    sd = st.stdev(values)
    return st.mean(values) / (sd / math.sqrt(len(values))) if sd else float("nan")


def report_replay(name, bands, fills):
    print("=" * 88)
    print(f"3. CENSORED REPLAY - {name}   (paper fills only; read the coverage column)")
    print("=" * 88)
    print(f"  {'window':<11}{'band':<13}{'seen':>6}{'in band':>9}{'rounds':>8}"
          f"{'stake':>9}{'net':>9}{'net/$100':>10}{'t':>7}  verdict")
    for start, end, lo, hi, _ in bands:
        window = [o for o in fills if end < o["secs_left"] <= start]
        hits = [o for o in window if lo <= o["average_price"] <= hi]
        if not hits:
            print(f"  {f'{start}-{end}s':<11}{f'{lo:.2f}-{hi:.2f}':<13}"
                  f"{len(window):>6}{0:>9}{'-':>8}{'-':>9}{'-':>9}{'-':>10}{'-':>7}"
                  f"  NO DATA - untestable")
            continue
        rounds = collections.defaultdict(lambda: [0.0, 0.0])
        for o in hits:
            r = rounds[o["condition_id"]]
            r[0] += o["stake"]
            r[1] += o["net"]
        stake = sum(r[0] for r in rounds.values())
        net = [r[1] for r in rounds.values()]
        t = tstat(net)
        if len(rounds) < 30:
            verdict = "too few rounds"
        elif abs(t) >= 2:
            verdict = "EDGE" if t > 0 else "LOSS"
        else:
            verdict = "NOISE"
        print(f"  {f'{start}-{end}s':<11}{f'{lo:.2f}-{hi:.2f}':<13}"
              f"{len(window):>6}{len(hits):>9}{len(rounds):>8}{stake:>9.0f}"
              f"{sum(net):>+9.2f}{100 * sum(net) / stake:>+10.2f}{t:>+7.2f}  {verdict}")
    print("\n  'seen' = settled fills recorded in that window at ANY price.")
    print("  'in band' = how many of them the proposed band would have taken.")
    print("  A band scoring on few of the fills it would want is not being")
    print("  tested; it is being sampled through the old band's keyhole.\n")


# --------------------------------------------------------------- 4. journal

def report_journal(name, bands):
    journal = ROOT / "signal_journal.csv"
    winners_path = ROOT / "signal_journal_winners.json"
    if not journal.exists() or not winners_path.exists():
        print("=" * 88)
        print("4. JOURNAL BACKTEST - SKIPPED")
        print("=" * 88)
        print(f"  {journal.name} not found.\n")
        print("  This is the test that would actually answer the question. The")
        print("  journal records BOTH legs' asks every sample whether or not a")
        print("  trade fires, so it is uncensored: any band schedule can be")
        print("  scored against it, including prices the live bands never")
        print("  bought. It costs nothing and samples every round.\n")
        print("      python3 signal_journal.py record        # leave running")
        print("      python3 journal_resolve.py              # then, later")
        print("      (NOT signal_journal.py resolve - see journal_resolve.py)")
        print("      python3 band_backtest.py                # picks it up\n")
        return

    winners = json.loads(winners_path.read_text())
    rows = list(csv.DictReader(journal.open(encoding="utf-8")))
    by_window = collections.defaultdict(list)
    for r in rows:
        by_window[int(r["window"])].append(r)

    per_round = []
    fills = skips_both = skips_none = 0
    for window, samples in sorted(by_window.items()):
        winner = winners.get(str(window))
        if winner not in ("UP", "DOWN"):
            continue
        samples.sort(key=lambda r: float(r["wall"]))
        last_fire = {}
        held_other = set()
        stake = net = 0.0
        for r in samples:
            secs = float(r["secs_left"])
            band = band_for(bands, secs)
            if band is None:
                continue
            start, end, lo, hi, interval = band
            wall = float(r["wall"])
            if wall - last_fire.get((start, end), -1e9) < interval:
                continue
            try:
                up_ask, dn_ask = float(r["up_ask"]), float(r["dn_ask"])
            except (TypeError, ValueError):
                continue
            in_band = [(s, a) for s, a in (("UP", up_ask), ("DOWN", dn_ask))
                       if lo <= a <= hi]
            if len(in_band) == 2:
                skips_both += 1
                continue
            if not in_band:
                skips_none += 1
                continue
            side, ask = in_band[0]
            if ("DOWN" if side == "UP" else "UP") in held_other:
                continue
            last_fire[(start, end)] = wall
            held_other.add(side)
            spend, shares = stake_and_shares(ask)
            fee = THETA * shares * ask * (1 - ask)
            stake += spend
            net += (shares if side == winner else 0.0) - spend - fee
            fills += 1
        if stake:
            per_round.append((stake, net))

    print("=" * 88)
    print(f"4. JOURNAL BACKTEST - {name}   (uncensored)")
    print("=" * 88)
    if not per_round:
        print("  journal present but no round produced a fill under this schedule\n")
        return
    stake = sum(s for s, _ in per_round)
    net = [n for _, n in per_round]
    t = tstat(net)
    print(f"  rounds traded          {len(per_round):>10}")
    print(f"  fills                  {fills:>10}")
    print(f"  skipped (both in band) {skips_both:>10}")
    print(f"  skipped (neither)      {skips_none:>10}")
    print(f"  stake                  ${stake:>9.2f}")
    print(f"  net PnL                ${sum(net):>+9.2f}   t = {t:+.2f}")
    print(f"  net per $100           ${100 * sum(net) / stake:>+9.2f}")
    print(f"  median round           ${st.median(net):>+9.2f}")
    if len(net) > 1 and st.mean(net):
        need = int((2 * st.stdev(net) / st.mean(net)) ** 2)
        print(f"  rounds for |t|=2       {need:>10,}")
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bands", default=NEW)
    ap.add_argument("--name", default="PROPOSED")
    ap.add_argument("--compare", default=OLD)
    args = ap.parse_args()

    proposed = parse_bands(args.bands)
    current = parse_bands(args.compare)
    fills = load_fills()

    report_mechanics(args.name, proposed)
    report_mechanics("CURRENT (for comparison)", current, sums=(1.03,))
    report_fees(args.name, proposed)
    report_fees("CURRENT", current)
    report_replay(args.name, proposed, fills)
    report_replay("CURRENT", current, fills)
    report_journal(args.name, proposed)


if __name__ == "__main__":
    main()
