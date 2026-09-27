#!/usr/bin/env python3
"""Will this bankroll survive long enough for the edge to pay?

    python3 survival.py                                 # the shipped config
    python3 survival.py --bankroll 250 --bet 2.50 --win-rate 0.463 --fills 123
    python3 survival.py --from-tape book_tape.jsonl     # measured, not assumed
    python3 survival.py --sweep-bet                     # what size survives

Volume does not create edge. It multiplies it, sign included.
-----------------------------------------------------------
"Survive and the volume will pay us" is two claims, and only one of them is
true as stated.

  Volume as EVIDENCE is free and it is the whole game. Every recorded round
  shrinks the error bar on the edge's sign. It costs nothing but wall-clock
  time, and it is what turns "46.3% over 123 fills" into a fact or a
  coincidence.

  Volume as STAKE is not free. It multiplies whatever the per-trade
  expectation already is. Positive, and size plus time compound it. Negative,
  and size plus time are precisely the mechanism that takes the account to
  zero. Volume is a lever on the sign, never a substitute for knowing it.

At a 0.39 average fill the taker fee alone is ~428 bps of notional, so the
break-even win rate sits about 1.7 points above the price paid. That hurdle is
charged on every single trade, win or lose, and it does not care how many
trades there are.

So this tool does not ask "what is the expected return". It asks the question
survival actually turns on: **given how little we know about the edge, what
fraction of futures end with the account still alive?** The edge is carried as
a posterior, not a point estimate, because a 46.3% win rate measured over 123
fills is consistent with a real edge AND with a coin - and those two futures
have very different ruin probabilities at the same bet size.

Nothing here places an order or reads a credential.
"""
from __future__ import annotations

import argparse
import math
import pathlib
import random
import statistics as st
import sys

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

from accounting import fees                   # noqa: E402

ROUNDS_PER_DAY = 288                          # 5-minute windows, 24h


# ------------------------------------------------------------- one trade ---
def trade_outcomes(price: float, bet: float, theta: float) -> tuple[float, float]:
    """(win payoff, loss payoff) in dollars for one taker buy.

    A winning share pays exactly $1. The fee is charged either way, because
    it is charged on the fill, not on the result.
    """
    shares = bet / price
    fee = fees.taker_fee(shares, price, th=theta)
    return shares - bet - fee, -(bet + fee)


def per_trade(price: float, bet: float, win_rate: float, theta: float) -> dict:
    up, down = trade_outcomes(price, bet, theta)
    ev = win_rate * up + (1 - win_rate) * down
    var = (win_rate * (up - ev) ** 2 + (1 - win_rate) * (down - ev) ** 2)
    sd = math.sqrt(max(0.0, var))
    be = fees.breakeven_win_rate(price, th=theta)
    # Kelly on a binary bet at decimal odds b = (1-p)/p.
    b = (1 - price) / price
    kelly = (win_rate * b - (1 - win_rate)) / b if b > 0 else 0.0
    return {
        "win": up, "loss": down, "ev": ev, "sd": sd,
        "breakeven": be, "edge_pp": (win_rate - be) * 100,
        "kelly": kelly, "sharpe": ev / sd if sd else float("nan"),
        "n_for_z2": int((2 * sd / ev) ** 2) if ev else None,
    }


# ------------------------------------------------------ the honest version ---
def posterior_draw(rng, wins: int, fills: int, prior: float = 1.0) -> float:
    """A plausible true win rate given what was actually observed.

    Beta(wins + prior, losses + prior). With `fills` small this is wide, and
    that width is the entire point: sizing off the point estimate assumes the
    thing you have not yet established.
    """
    return rng.betavariate(wins + prior, (fills - wins) + prior)


def simulate(*, bankroll: float, bet: float, price: float, win_rate: float,
             theta: float, rounds: int, trials: int, fills: int | None,
             ruin_floor: float, seed: int = 0) -> dict:
    """Monte Carlo the account forward. Returns survival, not just return.

    When `fills` is given, each trial draws its own true win rate from the
    posterior implied by that sample - so the answer includes the chance the
    edge was never there. That is the difference between "what happens if I
    am right" and "what happens given what I know".

    Ruin is ABSORBING: an account that hits its stop-out stops trading, which
    is what a stop-out means. Modelling it as a dip you trade back out of
    would flatter every number below.
    """
    rng = random.Random(seed)
    rand = rng.random
    wins_seen = int(round(win_rate * fills)) if fills else 0
    up, down = trade_outcomes(price, bet, theta)
    floor = bankroll * ruin_floor
    finals, ruined, drawdowns = [], 0, []

    for _ in range(trials):
        q = posterior_draw(rng, wins_seen, fills) if fills else win_rate
        cash = peak = bankroll
        worst_dd = 0.0
        dead = False
        for _r in range(rounds):
            if cash < bet:                    # cannot place the next bet
                dead = True
                break
            cash += up if rand() < q else down
            if cash > peak:
                peak = cash
            elif peak > 0:
                dd = (peak - cash) / peak
                if dd > worst_dd:
                    worst_dd = dd
            if cash <= floor:                 # stopped out; absorbing
                dead = True
                break
        finals.append(cash)
        drawdowns.append(worst_dd)
        if dead:
            ruined += 1

    finals.sort()
    return {
        "trials": trials, "rounds": rounds,
        "ruin": ruined / trials,
        "median_final": finals[len(finals) // 2],
        "p05": finals[int(0.05 * trials)],
        "p95": finals[int(0.95 * trials)],
        "mean_final": st.mean(finals),
        "p_lose_money": sum(1 for f in finals if f < bankroll) / trials,
        "median_dd": st.median(drawdowns),
        "worst_dd": max(drawdowns),
    }


# ----------------------------------------------------------------- reading ---
def measure_from_tape(tape: pathlib.Path, winners: pathlib.Path, **kw) -> dict:
    """Average fill price and win rate off the recorded book, via the tuner."""
    import band_backtest as bb
    import band_tuner as bt
    import book_recorder as br

    snaps = br.snapshots(tape)
    wins = {k: v for k, v in br.winners(winners).items() if v in ("UP", "DOWN")}
    if not wins:
        raise SystemExit("no resolved rounds - run `book_recorder.py resolve`")
    by_window = {}
    for s in snaps:
        w = int(s["window"])
        if str(w) in wins:
            by_window.setdefault(w, []).append(s)
    for w in by_window:
        by_window[w].sort(key=lambda s: -float(s["secs_left"]))

    fills = []
    for band in bb.parse_bands(kw.pop("bands")):
        f, _m = bt.entries_for(band, by_window, **kw)
        fills += f
    if not fills:
        raise SystemExit("the bands never filled on this tape - nothing to size")
    won = sum(1 for f in fills
              if wins.get(str(f["window"])) == f["side"])
    return {
        "price": sum(f["price"] for f in fills) / len(fills),
        "win_rate": won / len(fills),
        "fills": len(fills),
    }


# --------------------------------------------------------------- reporting ---
def report_hurdle(theta: float) -> None:
    print("\nTHE HURDLE - charged on every fill, win or lose")
    print("-" * 62)
    print(f"{'pay':>6}{'must win':>10}{'hurdle':>9}{'fee drag':>11}")
    for p in (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70):
        be = fees.breakeven_win_rate(p, th=theta)
        print(f"{p:>6.2f}{be * 100:>9.2f}%{(be - p) * 100:>8.2f}pp"
              f"{fees.fee_drag_bps(p, th=theta):>9.0f}bps")
    print("  Volume does not reduce this. It pays it more often.")


def report(cfg: dict, tr: dict, sims: dict) -> None:
    print("\nPER TRADE")
    print("-" * 62)
    print(f"  price {cfg['price']:.3f}   bet ${cfg['bet']:.2f}   "
          f"win rate {cfg['win_rate'] * 100:.1f}%")
    print(f"  win pays {tr['win']:+.2f}, loss costs {tr['loss']:+.2f}")
    print(f"  break-even win rate {tr['breakeven'] * 100:.2f}% -> "
          f"edge {tr['edge_pp']:+.2f}pp")
    print(f"  EV {tr['ev']:+.4f} per trade, sd {tr['sd']:.2f} "
          f"(EV/sd {tr['sharpe']:+.3f})")
    if tr["n_for_z2"]:
        print(f"  this edge needs ~{tr['n_for_z2']} fills to reach |z| = 2")
    if tr["ev"] > 0:
        print(f"  full Kelly would bet {tr['kelly'] * 100:.1f}% of bankroll "
              f"(${cfg['bankroll'] * tr['kelly']:.2f}); quarter Kelly "
              f"${cfg['bankroll'] * tr['kelly'] / 4:.2f}")
    else:
        print("  EV is negative: NO bet size survives. Kelly says stake zero.")

    print(f"\nSURVIVAL - ${cfg['bankroll']:.0f} bankroll, ${cfg['bet']:.2f} a "
          f"round, ruin = down {(1 - cfg['ruin_floor']) * 100:.0f}%")
    print("-" * 62)
    print(f"{'horizon':<14}{'ruin':>8}{'lose $':>9}{'p05':>9}"
          f"{'median':>9}{'p95':>9}{'max DD':>8}")
    for label, s in sims.items():
        print(f"{label:<14}{s['ruin'] * 100:>7.1f}%{s['p_lose_money'] * 100:>8.0f}%"
              f"{s['p05']:>9.0f}{s['median_final']:>9.0f}{s['p95']:>9.0f}"
              f"{s['median_dd'] * 100:>7.0f}%")
    if cfg.get("fills"):
        print(f"\n  Each future above draws its own true win rate from what "
              f"{cfg['fills']} fills")
        print(f"  actually support - so these include the futures where the "
              f"edge was luck.")
    else:
        print("\n  NOTE: --fills not given, so every future assumes the win "
              "rate is exactly right.")
        print("  That is the optimistic case, not the honest one.")


def report_bet_sweep(cfg: dict, theta: float, rounds: int, trials: int) -> None:
    print(f"\nWHAT SIZE SURVIVES - ${cfg['bankroll']:.0f} bankroll over "
          f"{rounds} rounds")
    print("-" * 62)
    print(f"{'bet':>7}{'% bank':>8}{'ruin':>8}{'lose $':>9}{'median':>9}"
          f"{'p05':>8}")
    print("  (ruin = stopped out; it is absorbing, you do not trade back out)")
    for bet in (0.50, 1.00, 2.50, 5.00, 10.00, 20.00):
        if bet > cfg["bankroll"] / 2:
            continue
        s = simulate(bankroll=cfg["bankroll"], bet=bet, price=cfg["price"],
                     win_rate=cfg["win_rate"], theta=theta, rounds=rounds,
                     trials=trials, fills=cfg.get("fills"),
                     ruin_floor=cfg["ruin_floor"], seed=1)
        print(f"{bet:>7.2f}{bet / cfg['bankroll'] * 100:>7.2f}%"
              f"{s['ruin'] * 100:>7.1f}%{s['p_lose_money'] * 100:>8.0f}%"
              f"{s['median_final']:>9.0f}{s['p05']:>8.0f}")
    print("  Bigger bets do not buy more edge. They buy more variance on the "
          "same edge.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bankroll", type=float, default=1000.0)
    ap.add_argument("--bet", type=float, default=2.50)
    ap.add_argument("--price", type=float, default=0.388,
                    help="average fill price (default: the register's phase 1)")
    ap.add_argument("--win-rate", type=float, default=0.463,
                    help="observed win rate (default: the register's phase 1)")
    ap.add_argument("--fills", type=int, default=123,
                    help="how many fills that win rate came from; 0 to assume "
                         "it is exact (optimistic)")
    ap.add_argument("--theta", type=float, default=0.07)
    ap.add_argument("--ruin-floor", type=float, default=0.5,
                    help="ruin = bankroll falling to this fraction (default 0.5)")
    ap.add_argument("--trials", type=int, default=2000)
    ap.add_argument("--from-tape", default="")
    ap.add_argument("--winners", default="book_tape_winners.json")
    ap.add_argument("--bands", default="300:240:0.35:0.45,240:180:0.30:0.40,"
                                       "180:120:0.40:0.50,120:60:0.55:0.75")
    ap.add_argument("--signal", default="chainlink")
    ap.add_argument("--sweep-bet", action="store_true")
    args = ap.parse_args(argv)

    price, win_rate, fills = args.price, args.win_rate, args.fills
    if args.from_tape:
        m = measure_from_tape(
            pathlib.Path(args.from_tape), pathlib.Path(args.winners),
            bands=args.bands, stake=args.bet, signal=args.signal,
            theta=args.theta, min_shares=5.0, cap=0.90)
        price, win_rate, fills = m["price"], m["win_rate"], m["fills"]
        print(f"measured off {args.from_tape}: {fills} fills, "
              f"avg price {price:.4f}, won {win_rate * 100:.1f}%")
    else:
        print(f"assuming {fills or 'an exact'} fills at {price:.3f} winning "
              f"{win_rate * 100:.1f}% (STRATEGIES.md phase 1 unless overridden)")

    cfg = {"bankroll": args.bankroll, "bet": args.bet, "price": price,
           "win_rate": win_rate, "fills": fills or None,
           "ruin_floor": args.ruin_floor}
    report_hurdle(args.theta)
    tr = per_trade(price, args.bet, win_rate, args.theta)

    sims = {}
    for label, days in (("1 day", 1), ("1 week", 7), ("1 month", 30)):
        sims[f"{label} ({days * ROUNDS_PER_DAY})"] = simulate(
            bankroll=args.bankroll, bet=args.bet, price=price,
            win_rate=win_rate, theta=args.theta,
            rounds=days * ROUNDS_PER_DAY, trials=args.trials,
            fills=fills or None, ruin_floor=args.ruin_floor, seed=0)
    report(cfg, tr, sims)
    if args.sweep_bet:
        report_bet_sweep(cfg, args.theta, 30 * ROUNDS_PER_DAY, args.trials)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
