#!/usr/bin/env python3
"""Rank band schedules on recorded book depth, with the noise floor shown.

    python3 band_tuner.py --sweep
    python3 band_tuner.py --compare "live=300:240:0.35:0.45,...;reserveA=..."
    python3 band_tuner.py --sweep --nulls 500 --per-window

Why this is not just `book_backtest.py` in a loop
-------------------------------------------------
`book_backtest.py` answers "what did THIS schedule do". Tuning asks "which of
these many schedules is best", and that is a different question with a much
easier way to be wrong: search enough cells and one of them looks great
because you searched, not because it works. `STRATEGIES.md` already has a
variant in its ruled-out table for exactly this - "six parameters fitting 57
rounds" - so the tool that proposes parameters has to carry the guard.

Three things this does that a leaderboard does not:

  ROUND-CLUSTERED   fills inside one 5-minute round share one settlement, so
                    they are one observation, not several. The unit of
                    evidence is the round. Same clustering `edge_test.py`
                    applies to the paper ledger, reused from it directly.

  NOISE FLOOR       the same sweep is re-run against shuffled outcomes, many
                    times, to measure how good the BEST cell looks when
                    there is provably no edge at all. A cell has to beat that
                    floor, not beat zero. This is the number that kills most
                    tuning results, which is why it is printed first.

  DEPTH-HONEST      every fill is walked off the recorded ladder by
                    book_backtest.walk, so a cell that signals often and
                    fills rarely is reported as thin rather than as good.
                    Coverage is printed beside every score; a cell scoring on
                    a handful of the rounds it wanted is not a finding.

Shuffling outcomes keeps each round's real prices, real depth and real signal
reading, and breaks only the link between the signal and who won - which is
precisely the thing a real edge claims to have.
"""
from __future__ import annotations

import argparse
import math
import pathlib
import random
import statistics as st
import sys
from decimal import Decimal

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

import book_backtest as bb                    # noqa: E402
import book_recorder as br                    # noqa: E402
import edge_test                              # noqa: E402

# The cells the register's per-window tables were built on, at the cadence
# the live config actually runs.
DEFAULT_WINDOWS = [(300, 240), (240, 180), (180, 120), (120, 60)]
DEFAULT_GRID = [(0.25, 0.35), (0.30, 0.40), (0.35, 0.45), (0.40, 0.50),
                (0.45, 0.55), (0.50, 0.60), (0.55, 0.65), (0.60, 0.70),
                (0.30, 0.50), (0.40, 0.60), (0.55, 0.75)]


# ------------------------------------------------------------- entry making ---
def entries_for(band, by_window, *, stake, signal, theta, min_shares, cap):
    """Every entry one band would take, WITHOUT knowing who won.

    Separating the fill from the outcome is what makes the noise floor
    affordable: the ladder walk runs once per cell, and re-scoring it under
    a reshuffled set of winners is then pure arithmetic.

    Returns (fills, misses). A fill is one round's entry: the side taken, the
    stake the venue minimum forced, the shares the ladder actually gave, and
    the fee each parcel really cost.
    """
    stake_d, cap_d, min_d = Decimal(str(stake)), Decimal(str(cap)), Decimal(str(min_shares))
    lo, hi = Decimal(str(band[2])), Decimal(str(band[3]))
    price_cap = min(cap_d, hi)
    fills, misses = [], 0

    for window, snaps in by_window.items():
        for snap in snaps:
            left = float(snap["secs_left"])
            if not band[1] <= left < band[0]:
                continue
            side = bb.side_for(snap, signal)
            if side is None:
                continue
            asks = bb.ladder(snap.get(bb.LEG[side]), "asks")
            if not asks or not lo <= asks[0][0] <= hi:
                continue
            try:
                fill = bb.walk(asks, stake_d, price_cap, min_d, theta)
            except bb.NoFill:
                misses += 1
                break                          # the band wanted it; depth refused
            fills.append({"window": window, "side": side,
                          "stake": float(fill["stake"]),
                          "shares": float(fill["shares"]),
                          "fee": float(fill["fee"]),
                          "price": float(fill["avg"])})
            break                              # one entry per band per round
    return fills, misses


def score(fills, winners) -> dict:
    """Round-clustered PnL of a cell under a given set of outcomes.

    One net number per round, because one round settles once. `edge_test`
    owns the statistics; this only decides what a round's net was.
    """
    per_round = {}
    for f in fills:
        won = winners.get(str(f["window"])) == f["side"]
        net = (f["shares"] if won else 0.0) - f["stake"] - f["fee"]
        per_round[f["window"]] = per_round.get(f["window"], 0.0) + net
    nets = list(per_round.values())
    if not nets:
        return {"rounds": 0, "net": 0.0, "t": float("nan"), "per100": 0.0,
                "stake": 0.0, "wins": 0}
    stake = sum(f["stake"] for f in fills)
    mean, t = edge_test.tstat(nets)
    return {
        "rounds": len(nets),
        "net": sum(nets),
        "t": t,
        "per100": sum(nets) / stake * 100 if stake else 0.0,
        "stake": stake,
        "wins": sum(1 for f in fills if winners.get(str(f["window"])) == f["side"]),
        "nets": nets,
    }


# -------------------------------------------------------------- noise floor ---
def noise_floor(cells, winners, *, nulls: int, seed: int = 0) -> dict:
    """How good the best cell looks when nothing predicts anything.

    Each draw reassigns the recorded outcomes across rounds at random, then
    re-scores EVERY cell and keeps the best |t| - the same maximum the sweep
    reports. Comparing a real best against this distribution is the whole
    point: a sweep of N cells has N chances to get lucky, and a single cell's
    p-value does not price that.
    """
    rng = random.Random(seed)
    windows = sorted(winners)
    outcomes = [winners[w] for w in windows]
    best_ts = []
    for _ in range(nulls):
        shuffled = outcomes[:]
        rng.shuffle(shuffled)
        fake = dict(zip(windows, shuffled))
        best = 0.0
        for _name, _band, fills, _misses in cells:
            if not fills:
                continue
            t = score(fills, fake)["t"]
            if math.isfinite(t):
                best = max(best, abs(t))
        if best:
            best_ts.append(best)
    if not best_ts:
        return {}
    best_ts.sort()
    return {
        "draws": len(best_ts),
        "median": best_ts[len(best_ts) // 2],
        "p95": best_ts[int(0.95 * len(best_ts))],
        "max": best_ts[-1],
        "values": best_ts,
    }


def beats_floor(t: float, floor: dict) -> float | None:
    """Share of null sweeps whose best cell beat this |t|. That is the p-value."""
    if not floor or not math.isfinite(t):
        return None
    hits = sum(1 for v in floor["values"] if v >= abs(t))
    return (hits + 1) / (floor["draws"] + 1)


# ------------------------------------------------------------------ driving ---
def build_cells(by_window, bands, **kw):
    cells = []
    for name, band in bands:
        fills, misses = entries_for(band, by_window, **kw)
        cells.append((name, band, fills, misses))
    return cells


def sweep_bands(windows, grid):
    for start, end in windows:
        for lo, hi in grid:
            yield (f"{start}-{end}s {lo:.2f}-{hi:.2f}", (start, end, lo, hi))


def schedule_cells(by_window, schedules, **kw):
    """A named multi-band schedule scored as one strategy, not cell by cell."""
    out = []
    for name, bands in schedules:
        fills, misses = [], 0
        for band in bands:
            f, m = entries_for(band, by_window, **kw)
            fills += f
            misses += m
        out.append((name, bands, fills, misses))
    return out


def report(cells, winners, floor, *, title, top=15):
    rows = []
    for name, band, fills, misses in cells:
        s = score(fills, winners)
        s.update(name=name, band=band, misses=misses,
                 signalled=len(fills) + misses)
        rows.append(s)
    rows.sort(key=lambda r: (-abs(r["t"]) if math.isfinite(r["t"]) else 0))

    print(f"\n{'=' * 78}")
    print(title)
    print("=" * 78)
    if floor:
        print(f"NOISE FLOOR ({floor['draws']} shuffled sweeps of the same "
              f"{len(cells)} cells)")
        print(f"  with NO edge at all, the best cell still reaches "
              f"|t| = {floor['median']:.2f} typically, "
              f"{floor['p95']:.2f} at the 95th pct, {floor['max']:.2f} at worst.")
        print(f"  a cell must clear {floor['p95']:.2f} to mean anything. "
              f"Beating zero is not the bar.")
    else:
        print("NOISE FLOOR: not computed (--nulls 0). Every |t| below is "
              "unadjusted for the search.")

    print(f"\n{'cell':<26}{'rnds':>5}{'fill':>6}{'miss':>6}{'won':>9}"
          f"{'/$100':>8}{'net':>9}{'|t|':>7}{'p':>7}")
    print("-" * 81)
    for r in rows[:top]:
        p = beats_floor(r["t"], floor)
        t = abs(r["t"]) if math.isfinite(r["t"]) else float("nan")
        hit = f"{r['wins']}/{len(r.get('nets') or ())}" if r["rounds"] else "-"
        print(f"{r['name']:<26}{r['rounds']:>5}"
              f"{r['signalled'] - r['misses']:>6}{r['misses']:>6}{hit:>9}"
              f"{r['per100']:>8.1f}{r['net']:>9.2f}"
              f"{(f'{t:.2f}' if math.isfinite(t) else '-'):>7}"
              f"{(f'{p:.3f}' if p is not None else '-'):>7}")

    print()
    live = [r for r in rows if r["rounds"] >= 2]
    if not live:
        print("  no cell filled in two or more rounds - nothing is measurable yet.")
        return rows
    best = live[0]
    need = edge_test.rounds_for_significance(best.get("nets") or [])
    print(f"  best cell: {best['name']} at {best['per100']:+.1f} per $100 "
          f"over {best['rounds']} rounds")
    if best["misses"]:
        pct = best["misses"] / max(1, best["signalled"])
        print(f"  it could not fill {best['misses']} of {best['signalled']} "
              f"entries it wanted ({pct:.0%}) - thin, not free")
    p = beats_floor(best["t"], floor)
    if p is not None and p > 0.05:
        print(f"  VERDICT: p = {p:.3f} against the noise floor. This is what "
              f"searching {len(cells)} cells looks like when nothing is there.")
        print("  Do not put it in .env.")
    elif p is not None and p > 0.01:
        print(f"  VERDICT: p = {p:.3f} - MARGINAL. It beat the noise floor by "
              f"about as much as one cell in twenty does by luck.")
        print("  Not actionable on this sample. Record more rounds and re-run; "
              "a real effect grows, a lucky one does not.")
    elif p is not None:
        print(f"  VERDICT: p = {p:.3f} against the noise floor - it clears the "
              f"search over {best['rounds']} rounds.")
    if need:
        print(f"  this effect size needs about {need} rounds to reach |t| = 2; "
              f"you have {best['rounds']}.")
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tape", default=str(br.TAPE))
    ap.add_argument("--winners", default=str(br.WINNERS))
    ap.add_argument("--sweep", action="store_true",
                    help="scan every price cell in every time window")
    ap.add_argument("--compare", default="",
                    help='"name=bands;name=bands" head to head, whole schedules')
    ap.add_argument("--signal", default="chainlink",
                    choices=["chainlink", "binance", "book", "up", "down"])
    ap.add_argument("--stake", type=float, default=2.50)
    ap.add_argument("--theta", type=float, default=0.07)
    ap.add_argument("--min-shares", type=float, default=5.0)
    ap.add_argument("--max-price", type=float, default=0.90)
    ap.add_argument("--nulls", type=int, default=300,
                    help="shuffled sweeps for the noise floor (0 to skip)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args(argv)

    snaps = br.snapshots(pathlib.Path(args.tape))
    winners = {k: v for k, v in br.winners(pathlib.Path(args.winners)).items()
               if v in ("UP", "DOWN")}
    if not winners:
        print("no resolved rounds - run `book_recorder.py resolve` first.")
        return 1

    by_window = {}
    for s in snaps:
        w = int(s["window"])
        if str(w) in winners:
            by_window.setdefault(w, []).append(s)
    for w in by_window:
        by_window[w].sort(key=lambda s: -float(s["secs_left"]))

    kw = dict(stake=args.stake, signal=args.signal, theta=args.theta,
              min_shares=args.min_shares, cap=args.max_price)
    print(f"tape: {len(snaps)} snapshots, {len(by_window)} resolved rounds, "
          f"signal {args.signal}, stake ${args.stake:.2f}")

    if args.compare:
        scheds = []
        for chunk in args.compare.split(";"):
            if not chunk.strip():
                continue
            name, _, raw = chunk.partition("=")
            scheds.append((name.strip(), bb.parse_bands(raw)))
        cells = schedule_cells(by_window, scheds, **kw)
        floor = noise_floor(cells, winners, nulls=args.nulls, seed=args.seed)
        report(cells, winners, floor, title="SCHEDULES, HEAD TO HEAD",
               top=args.top)

    if args.sweep or not args.compare:
        cells = build_cells(by_window, list(sweep_bands(DEFAULT_WINDOWS,
                                                        DEFAULT_GRID)), **kw)
        floor = noise_floor(cells, winners, nulls=args.nulls, seed=args.seed)
        report(cells, winners, floor,
               title=f"CELL SWEEP - {len(cells)} cells searched", top=args.top)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
