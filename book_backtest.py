#!/usr/bin/env python3
"""Backtest against recorded order books, not against a price line.

    python3 book_backtest.py                       # default bands, default tape
    python3 book_backtest.py --bands "300:240:0.50:0.60,..."
    python3 book_backtest.py --signal chainlink --stake 2.50
    python3 book_backtest.py --tape book_tape.jsonl --per-round

Gate 1
------
The usual backtest takes one number per window - a close, a mid, a best ask -
checks the entry against it, and calls the difference edge. It is not a
backtest. A fill on Polymarket is not struck against a number, it is struck
against a ladder: the $2.50 takes some shares at the best ask, the rest at
whatever is stacked behind it, and it takes nothing at all when the whole
ladder inside your price cap holds fewer than the venue's minimum shares.
Those three outcomes - filled cheap, filled worse than quoted, not filled -
are invisible to a price line, and two of them lose money.

So this replays against `book_tape.jsonl`: every recorded level on both
sides of both legs, at the second it existed, and the real resolution of
every window from `book_tape_winners.json`.

To make the difference impossible to ignore it runs BOTH engines on the
identical signals and prints them side by side:

  BOOK   walks the recorded asks. Fills partially-consumed levels at their
         own prices, pays the per-share fee at each, and refuses the trade
         outright when the depth inside the cap cannot reach the venue
         minimum. This is the number.
  QUOTE  the daydream. Assumes the whole stake fills at the best ask, every
         time, with no depth and no misses. This is what a close-price
         backtest reports.

The gap between them is the part of your edge that only ever existed on
paper. If BOOK is red while QUOTE is green, the strategy does not work and
the price line was hiding it.

Every trade is a taker buy: this venue's maker rebate is zero and the bot
crosses the spread, so there is no configuration in which a resting order
was what actually happened.
"""
from __future__ import annotations

import argparse
import pathlib
import statistics as st
import sys
from decimal import Decimal, ROUND_HALF_UP

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

import book_recorder                          # noqa: E402
import orderbook                              # noqa: E402
from accounting import fees                   # noqa: E402

WINDOW_SECONDS = 300
DEFAULT_BANDS = ("300:240:0.35:0.45,240:180:0.30:0.40,"
                 "180:120:0.40:0.50,120:60:0.55:0.75")
ZERO = Decimal("0")
CENT = Decimal("0.01")


# ------------------------------------------------------------------- bands ---
def parse_bands(raw: str) -> list[tuple[int, int, float, float]]:
    """"start:end:lo:hi" chunks, seconds-left descending.

    A trailing fifth field (the live cadence's per-band interval) is accepted
    and ignored, so a PHASE1_BANDS string can be pasted in unedited.
    """
    out = []
    for chunk in raw.split(","):
        parts = [p for p in chunk.strip().split(":") if p != ""]
        if len(parts) < 4:
            raise SystemExit(f"bad band {chunk!r}: need start:end:lo:hi")
        start, end = int(parts[0]), int(parts[1])
        lo, hi = float(parts[2]), float(parts[3])
        if end >= start:
            raise SystemExit(f"bad band {chunk!r}: end must be before start")
        if not 0 < lo <= hi < 1:
            raise SystemExit(f"bad band {chunk!r}: need 0 < lo <= hi < 1")
        out.append((start, end, lo, hi))
    return sorted(out, key=lambda b: -b[0])


def band_for(secs_left: float, bands) -> tuple | None:
    for band in bands:
        if band[1] <= secs_left < band[0]:
            return band
    return None


# ------------------------------------------------------------------ ladder ---
def ladder(book: dict, side: str) -> list[tuple[Decimal, Decimal]]:
    """Recorded levels as (price, size) decimals, best first.

    The tape stores prices as strings precisely so this stays exact. A level
    that is unparseable or non-positive is dropped rather than ending the
    walk: the rest of the ladder was really there.
    """
    out = []
    for row in (book or {}).get(side) or ():
        try:
            price, size = Decimal(str(row[0])), Decimal(str(row[1]))
        except Exception:
            continue
        if price.is_finite() and size.is_finite() and price > 0 and size > 0:
            out.append((price, size))
    return out


class NoFill(Exception):
    """The order could not have been filled against this book."""


def walk(asks, stake: Decimal, cap: Decimal, min_shares: Decimal,
         theta: float) -> dict:
    """Spend `stake` walking real asks. The fill, or why there wasn't one.

    This is the same walk `paper_trade._fok_buy` performs against a live
    book, and the stake is raised to the venue minimum by the same
    `orderbook.venue_minimum_stake` the live and paper order paths share -
    so a fill reported here is a fill that code would have taken, not a
    parallel invention that happens to agree on the easy cases.
    """
    if not asks:
        raise NoFill("no asks on the recorded book")
    if asks[0][0] > cap:
        raise NoFill(f"best ask {asks[0][0]} is above the {cap} cap")

    stake = orderbook.venue_minimum_stake(stake, asks, min_shares, cap)
    remaining = stake
    shares = ZERO
    fee = ZERO
    worst = ZERO
    levels = 0
    for price, size in asks:
        if price > cap:
            break
        spend = min(remaining, price * size)
        if spend <= ZERO:
            continue
        took = spend / price
        shares += took
        fee += Decimal(str(fees.taker_fee(float(took), float(price), th=theta)))
        remaining -= spend
        worst = price
        levels += 1
        if remaining <= CENT / 2:
            remaining = ZERO
            break
    if remaining > ZERO:
        raise NoFill(f"only ${stake - remaining:.2f} of ${stake:.2f} available "
                     f"at or below {cap}")
    if shares < min_shares:
        raise NoFill(f"{shares:.2f} shares is under the {min_shares} venue minimum")
    return {
        "stake": stake,
        "shares": shares,
        "avg": stake / shares,
        "worst": worst,
        "fee": fee.quantize(CENT, rounding=ROUND_HALF_UP),
        "levels": levels,
    }


def quote_fill(asks, stake: Decimal, cap: Decimal, theta: float) -> dict:
    """The price-line fantasy: the whole stake at the best ask, no depth.

    Deliberately ignores size, the venue minimum and every level behind the
    first. That is the point - this is the backtest being argued against,
    reproduced honestly so the comparison is fair.
    """
    if not asks:
        raise NoFill("no asks on the recorded book")
    price = asks[0][0]
    if price > cap:
        raise NoFill(f"best ask {price} is above the {cap} cap")
    shares = stake / price
    return {
        "stake": stake,
        "shares": shares,
        "avg": price,
        "worst": price,
        "fee": Decimal(str(fees.taker_fee(float(shares), float(price), th=theta))),
        "levels": 1,
    }


# ----------------------------------------------------------------- signals ---
def side_for(snap: dict, rule: str) -> str | None:
    """Which leg the strategy buys, from the fields recorded at that tick."""
    if rule == "up":
        return "UP"
    if rule == "down":
        return "DOWN"
    if rule == "chainlink":
        strike, now = snap.get("cl_strike"), snap.get("cl_now")
        if strike is None or now is None:
            return None
        return "UP" if now >= strike else "DOWN"
    if rule == "binance":
        strike, now = snap.get("bn_strike"), snap.get("bn_now")
        if strike is None or now is None:
            return None
        return "UP" if now >= strike else "DOWN"
    if rule == "book":
        # Depth votes: the side carrying more resting size on the UP leg.
        bids = sum(float(s) for _p, s in ladder(snap.get("up"), "bids"))
        asks = sum(float(s) for _p, s in ladder(snap.get("up"), "asks"))
        if not bids or not asks:
            return None                       # abstain on a one-sided book
        return "UP" if bids >= asks else "DOWN"
    raise SystemExit(f"unknown signal rule {rule!r}")


LEG = {"UP": "up", "DOWN": "down"}


# ---------------------------------------------------------------- replaying ---
def replay(snaps, winners, bands, *, stake: float, signal: str, theta: float,
           min_shares: float, cap: float) -> dict:
    """One entry per band per round, both engines, on identical signals."""
    stake_d = Decimal(str(stake))
    cap_d = Decimal(str(cap))
    min_d = Decimal(str(min_shares))

    by_window: dict[int, list[dict]] = {}
    for snap in snaps:
        by_window.setdefault(int(snap["window"]), []).append(snap)

    trades: list[dict] = []
    misses: list[dict] = []
    skipped = {"unresolved": 0, "split": 0, "no_signal": 0, "no_band": 0}
    rounds = 0

    for window in sorted(by_window):
        outcome = winners.get(str(window))
        if outcome is None:
            skipped["unresolved"] += 1
            continue
        if outcome not in ("UP", "DOWN"):
            skipped["split"] += 1
            continue
        rounds += 1
        taken: set[tuple] = set()
        for snap in sorted(by_window[window], key=lambda s: -float(s["secs_left"])):
            band = band_for(float(snap["secs_left"]), bands)
            if band is None:
                skipped["no_band"] += 1
                continue
            if band in taken:
                continue
            side = side_for(snap, signal)
            if side is None:
                skipped["no_signal"] += 1
                continue
            asks = ladder(snap.get(LEG[side]), "asks")
            if not asks:
                continue
            best = asks[0][0]
            lo, hi = Decimal(str(band[2])), Decimal(str(band[3]))
            if not lo <= best <= hi:
                continue                      # band did not want this price
            taken.add(band)
            won = side == outcome
            price_cap = min(cap_d, hi)
            try:
                book_fill = walk(asks, stake_d, price_cap, min_d, theta)
            except NoFill as exc:
                book_fill = None
                reason = str(exc)
            try:
                quote = quote_fill(asks, stake_d, price_cap, theta)
            except NoFill:
                quote = None
            if quote is None:
                continue
            row = {
                "window": window, "secs_left": float(snap["secs_left"]),
                "band": band, "side": side, "won": won, "best": best,
                "quote": quote, "book": book_fill,
                "quote_pnl": _pnl(quote, won),
                "book_pnl": _pnl(book_fill, won) if book_fill else ZERO,
            }
            trades.append(row)
            if book_fill is None:
                misses.append({**row, "reason": reason})

    return {"trades": trades, "misses": misses, "rounds": rounds,
            "skipped": skipped}


def _pnl(fill: dict | None, won: bool) -> Decimal:
    """Settlement PnL of one taker buy. A winning share pays exactly $1."""
    if fill is None:
        return ZERO
    payout = fill["shares"] if won else ZERO
    return payout - fill["stake"] - fill["fee"]


# ----------------------------------------------------------------- reporting ---
def _money(value) -> str:
    return f"{float(value):+.2f}"


def report(result: dict, *, per_round: bool = False) -> None:
    trades, misses = result["trades"], result["misses"]
    rounds, skipped = result["rounds"], result["skipped"]

    print(f"\n{'=' * 68}")
    print(f"{rounds} resolved rounds replayed, {len(trades)} entries signalled")
    if skipped["unresolved"] or skipped["split"]:
        print(f"  skipped: {skipped['unresolved']} rounds not yet resolved, "
              f"{skipped['split']} split/unclear "
              f"(run `book_recorder.py resolve`)")
    if not trades:
        print("no entry ever matched a band - nothing to measure")
        return

    filled = [t for t in trades if t["book"]]
    q_pnl = sum((t["quote_pnl"] for t in trades), ZERO)
    b_pnl = sum((t["book_pnl"] for t in trades), ZERO)
    q_stake = sum((t["quote"]["stake"] for t in trades), ZERO)
    b_stake = sum((t["book"]["stake"] for t in filled), ZERO)

    print(f"\n{'':<22}{'QUOTE (price line)':>22}{'BOOK (real depth)':>22}")
    print(f"{'-' * 68}")
    print(f"{'entries filled':<22}{len(trades):>22}{len(filled):>22}")
    print(f"{'staked':<22}{'$' + f'{float(q_stake):.2f}':>22}"
          f"{'$' + f'{float(b_stake):.2f}':>22}")
    print(f"{'net PnL':<22}{_money(q_pnl):>22}{_money(b_pnl):>22}")
    if q_stake > 0 and b_stake > 0:
        print(f"{'return on stake':<22}"
              f"{f'{float(q_pnl / q_stake) * 100:+.2f}%':>22}"
              f"{f'{float(b_pnl / b_stake) * 100:+.2f}%':>22}")
    q_wins = sum(1 for t in trades if t["won"])
    b_wins = sum(1 for t in filled if t["won"])
    q_hits = f"{q_wins}/{len(trades)} ({q_wins / len(trades):.0%})"
    b_hits = (f"{b_wins}/{len(filled)} ({b_wins / len(filled):.0%})"
              if filled else "n/a")
    print(f"{'hit rate':<22}{q_hits:>22}{b_hits:>22}")

    print("\nWhat the price line could not see")
    print(f"{'-' * 68}")
    if misses:
        pct = len(misses) / len(trades)
        print(f"  {len(misses)} of {len(trades)} signalled entries ({pct:.0%}) "
              f"COULD NOT FILL.")
        print("  The quote backtest counted every one of them as a trade.")
        shown = 0
        for m in misses:
            if shown >= 5:
                break
            print(f"    round {m['window']} @ {m['secs_left']:.0f}s left, "
                  f"{m['side']} best {float(m['best']):.3f}: {m['reason']}")
            shown += 1
        if len(misses) > shown:
            print(f"    ... and {len(misses) - shown} more")
        lost = sum((m["quote_pnl"] for m in misses), ZERO)
        print(f"  Phantom PnL booked on trades that never happened: {_money(lost)}")
    else:
        print("  every signalled entry had the depth to fill")

    if filled:
        slips = [float((t["book"]["avg"] - t["best"]) / t["best"]) * 10000
                 for t in filled]
        deeper = [t for t in filled if t["book"]["levels"] > 1]
        print(f"  {len(deeper)}/{len(filled)} fills walked past the best ask "
              f"(up to {max(t['book']['levels'] for t in filled)} levels).")
        print(f"  Slippage vs the quoted best ask: "
              f"mean {st.mean(slips):.0f} bps, median {st.median(slips):.0f} bps, "
              f"worst {max(slips):.0f} bps.")
        fee = sum((t["book"]["fee"] for t in filled), ZERO)
        print(f"  Taker fees paid: ${float(fee):.2f} "
              f"({float(fee / b_stake) * 100:.2f}% of stake).")

    gap = q_pnl - b_pnl
    print(f"\n  VERDICT: the price line overstated PnL by {_money(gap)} "
          f"on ${float(q_stake):.2f} staked.")
    if b_pnl <= 0 < q_pnl:
        print("  The edge does not survive the real book. It was never there.")
    elif b_pnl > 0:
        print("  The edge survives the real book. Gate 1 passed on this tape.")
    else:
        print("  Both engines are red - the price line was not the problem.")

    if per_round:
        print(f"\n{'round':<12}{'left':>6}{'side':>6}{'best':>7}"
              f"{'avg':>7}{'shares':>8}{'quote':>9}{'book':>9}")
        for t in trades:
            book = t["book"]
            avg = f"{float(book['avg']):.3f}" if book else "-"
            shares = f"{float(book['shares']):.1f}" if book else "-"
            pnl = _money(t["book_pnl"]) if book else "NOFILL"
            print(f"{t['window']:<12}{t['secs_left']:>6.0f}{t['side']:>6}"
                  f"{float(t['best']):>7.3f}{avg:>7}{shares:>8}"
                  f"{_money(t['quote_pnl']):>9}{pnl:>9}")
    print()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tape", default=str(book_recorder.TAPE))
    ap.add_argument("--winners", default=str(book_recorder.WINNERS))
    ap.add_argument("--bands", default=DEFAULT_BANDS)
    ap.add_argument("--signal", default="chainlink",
                    choices=["chainlink", "binance", "book", "up", "down"])
    ap.add_argument("--stake", type=float, default=2.50)
    ap.add_argument("--theta", type=float, default=0.07,
                    help="taker fee coefficient (default 0.07)")
    ap.add_argument("--min-shares", type=float, default=5.0,
                    help="venue minimum order size in shares")
    ap.add_argument("--max-price", type=float, default=0.90)
    ap.add_argument("--per-round", action="store_true",
                    help="print every entry, not just the summary")
    args = ap.parse_args(argv)

    tape = pathlib.Path(args.tape)
    snaps = book_recorder.snapshots(tape)
    wins = book_recorder.winners(pathlib.Path(args.winners))
    if not wins:
        print("no resolved rounds yet - run `book_recorder.py resolve` first.\n"
              "A backtest without the real outcomes is not a backtest.")
        return 1
    print(f"tape: {tape.name} - {len(snaps)} full-depth snapshots, "
          f"{len(wins)} resolved rounds")
    print(f"signal: {args.signal}   stake: ${args.stake:.2f}   "
          f"bands: {args.bands}")
    result = replay(snaps, wins, parse_bands(args.bands), stake=args.stake,
                    signal=args.signal, theta=args.theta,
                    min_shares=args.min_shares, cap=args.max_price)
    report(result, per_round=args.per_round)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
