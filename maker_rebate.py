#!/usr/bin/env python3
"""What does $X a day of volume actually pay - as maker, and as taker?

    python3 maker_rebate.py --volume 30000
    python3 maker_rebate.py --volume 30000 --price 0.50 --rebate-share 0.20
    python3 maker_rebate.py --volume 30000 --bet 2.50 --rounds-per-day 288

The one line that matters
------------------------
Fees on this venue are charged to the TAKER and rebated to the MAKER, so the
same $30,000 of daily volume is a large bill or a modest income depending
only on which side of the book produced it. Nothing else in this file is as
important as that sign.

For a fixed notional N filled at price p, the taker fee is

    fee = shares * theta * p * (1 - p) = (N/p) * theta * p * (1-p)
        = N * theta * (1 - p)

so the fee is a straight percentage of notional that shrinks as price rises.
A maker rebate returning `share` of the taker fees collected, allocated
pro-rata by each maker's own fee-equivalent volume, therefore pays that maker

    rebate = share * N * theta * (1 - p)

The pro-rata cancels: your slice of the pool is proportional to the fees your
own fills generated, so competition for the pool does not dilute the rate -
it only decides how much volume you can get filled at all.

What this file will NOT tell you
--------------------------------
Whether the rebate is worth collecting. The rebate is GROSS revenue on
volume. Every filled maker order is also a position that wins or loses, and a
resting order is filled precisely when someone acts on a price move it has
not repriced for. So the output below prints the rebate next to the
break-even adverse selection: the loss rate, in basis points of notional, at
which the rebate exactly cancels. That number is small. Treat it as the
hurdle, not as a footnote.

Rebate percentages are NOT hardcoded from a doc. Pass --rebate-share with a
figure you have verified for this market via rewards_check.py; the default is
a secondary-source figure for crypto and is flagged as such on every run.
"""
from __future__ import annotations

import argparse

# Secondary-source figure for crypto after the July 2026 fee update. Not a
# first-hand read - see rewards_check.py.
DEFAULT_REBATE_SHARE = 0.20
UNVERIFIED = True

# Polymarket US (QCX LLC, CFTC-regulated) and polymarket.com are DIFFERENT
# exchanges with different incentive programs. config.py pins CLOB_HOST to
# clob.polymarket.com and refuses any other host unless ALLOW_CUSTOM_CLOB_HOST
# is set, so a program documented only on docs.polymarket.us does not
# automatically apply to this bot. Check which entity a program belongs to
# before sizing anything on it.
ENTITY_NOTE = ("the Volume Incentive Program is documented on "
               "docs.polymarket.us (Polymarket US); this bot trades "
               "clob.polymarket.com")


def fee_on_notional(notional: float, price: float, theta: float) -> float:
    """Taker fee for `notional` dollars filled at `price`. Shares cancel."""
    return notional * theta * (1.0 - price)


def breakdown(*, volume: float, price: float, theta: float,
              rebate_share: float) -> dict:
    fee = fee_on_notional(volume, price, theta)
    rebate = rebate_share * fee
    return {
        "fee": fee,
        "fee_bps": theta * (1.0 - price) * 10_000,
        "rebate": rebate,
        "rebate_bps": rebate_share * theta * (1.0 - price) * 10_000,
        # The loss rate on notional at which the rebate is exactly cancelled.
        "breakeven_adverse_bps": rebate_share * theta * (1.0 - price) * 10_000,
    }


def report(volume: float, theta: float, rebate_share: float,
           prices: tuple[float, ...]) -> None:
    print(f"\n${volume:,.0f} of volume a day, theta = {theta:.3f}, "
          f"rebate share = {rebate_share:.0%}")
    print("=" * 74)
    print(f"{'avg price':>10}{'TAKER pays/day':>17}{'MAKER earns/day':>17}"
          f"{'rebate':>9}{'b/e adverse':>13}")
    print("-" * 74)
    for p in prices:
        b = breakdown(volume=volume, price=p, theta=theta,
                      rebate_share=rebate_share)
        print(f"{p:>10.2f}{-b['fee']:>16,.2f}{b['rebate']:>17,.2f}"
              f"{b['rebate_bps']:>8.0f}bps{b['breakeven_adverse_bps']:>10.0f}bps")
    print("\n  'b/e adverse' = the adverse-selection loss, in bps of notional,")
    print("  that exactly cancels the rebate. Lose more than that on the")
    print("  positions and the volume costs you money however large it is.")


def report_scale(volume: float, price: float, theta: float,
                 rebate_share: float, bet: float, rounds: float) -> None:
    b = breakdown(volume=volume, price=price, theta=theta,
                  rebate_share=rebate_share)
    per_round = volume / rounds if rounds else float("nan")
    fills = volume / bet if bet else float("nan")
    print(f"\nWHAT ${volume:,.0f}/DAY ACTUALLY REQUIRES (at {price:.2f})")
    print("-" * 74)
    print(f"  {rounds:,.0f} five-minute rounds a day -> "
          f"${per_round:,.2f} of filled volume per round")
    print(f"  at ${bet:.2f} a clip that is {fills:,.0f} fills a day, "
          f"{fills / rounds:,.1f} per round")
    print(f"  maker rebate:            ${b['rebate']:,.2f}/day   "
          f"${b['rebate'] * 30:,.0f}/month")
    print(f"  the same volume as taker: ${-b['fee']:,.2f}/day   "
          f"${-b['fee'] * 30:,.0f}/month")
    print(f"  swing between the two sides: ${b['fee'] + b['rebate']:,.2f}/day")
    print(f"\n  and the positions themselves must not lose more than "
          f"{b['breakeven_adverse_bps']:.0f}bps")
    print(f"  of notional, i.e. ${b['rebate']:,.2f}/day, or the rebate is gone.")


def report_liquidity_pool(pool: float, share: float, volume: float,
                          price: float, theta: float,
                          rebate_share: float) -> None:
    """Liquidity rewards do NOT scale with your volume. Different animal.

    A rebate is a percentage of the fees your fills generated, so it grows
    exactly in step with how much you trade. A liquidity-reward pool is a
    fixed daily number set per market and split between makers by a score on
    their RESTING size and distance from the midpoint - so it is capped no
    matter how much you do, and it is diluted by every other maker competing
    for the same pool. Confusing the two is how a $30k/day plan gets built on
    a $50/day pool.
    """
    earn = pool * share
    reb = breakdown(volume=volume, price=price, theta=theta,
                    rebate_share=rebate_share)["rebate"]
    print(f"\nLIQUIDITY REWARDS - a ${pool:,.2f}/day pool, your score share "
          f"{share:.0%}")
    print("-" * 74)
    print(f"  you earn ${earn:,.2f}/day - and that does NOT rise with volume.")
    print(f"  it is capped by the pool and diluted by other makers.")
    print(f"  for comparison the volume-proportional rebate on "
          f"${volume:,.0f}/day is ${reb:,.2f}/day")
    if earn < reb:
        print("  -> on these numbers the rebate is the bigger line. Size the")
        print("     business on rebate + trading PnL, treat the pool as a tip.")
    else:
        print("  -> the pool is the bigger line here, so resting size and")
        print("     staying inside max-spread matter more than turnover.")


# ------------------------------------------------- volume incentive pools ---
def volume_incentive(*, pool: float, your_contracts: float,
                     total_contracts: float, price: float,
                     theta: float) -> dict:
    """A FIXED pool per contract, split pro-rata by volume traded.

    This is a different shape from a rebate and the difference is the whole
    point. A rebate pays a fixed PERCENTAGE of the fees your own fills
    generated, so the rate is immune to how many others are trading. A fixed
    pool pays `pool * (yours / total)`, so every other participant's volume
    DILUTES your rate. "More volume earns more rewards" is true for you
    individually and false for the rate: in a crowded pool everyone trades
    more and everyone earns less per contract.

    Reported per SHARE (contract), not per dollar, because that is the unit
    these pools are denominated in and the unit the fee compares against:

        fee per share    = theta * p * (1 - p)
        reward per share = pool / total_contracts

    so the pool covers the taker fee only while `total_contracts` stays below
    `pool / (theta * p * (1-p))`. Past that, trading into it costs money.
    """
    if total_contracts <= 0 or pool <= 0:
        return {}
    share = your_contracts / total_contracts
    reward = pool * share
    per_share = pool / total_contracts
    fee_per_share = theta * price * (1.0 - price)
    notional = your_contracts * price
    return {
        "share": share,
        "reward": reward,
        "per_share": per_share,
        "fee_per_share": fee_per_share,
        "covers_fee": per_share / fee_per_share if fee_per_share else float("inf"),
        "reward_bps": (per_share / price) * 10_000 if price else 0.0,
        "net_per_share": per_share - fee_per_share,
        "net": (per_share - fee_per_share) * your_contracts,
        "notional": notional,
        # Total volume at which the pool exactly pays the taker fee.
        "breakeven_total": pool / fee_per_share if fee_per_share else float("inf"),
    }


def report_volume_incentive(pool: float, your_contracts: float,
                            totals: tuple[float, ...], price: float,
                            theta: float) -> None:
    print(f"\n! ENTITY: {ENTITY_NOTE}.")
    print("! Confirm the program covers the venue you actually trade before "
          "using any of this.")
    print(f"\nVOLUME INCENTIVE POOL - ${pool:,.0f} split pro-rata by volume")
    print(f"you trade {your_contracts:,.0f} contracts at {price:.2f} "
          f"(${your_contracts * price:,.0f} notional)")
    print("-" * 74)
    fee_ps = theta * price * (1.0 - price)
    print(f"  your taker fee is {fee_ps * 100:.2f}c per contract "
          f"(${fee_ps * your_contracts:,.2f} total)")
    print(f"\n{'total volume':>14}{'your share':>12}{'you earn':>11}"
          f"{'per contract':>14}{'vs fee':>9}{'net':>11}")
    for total in totals:
        v = volume_incentive(pool=pool, your_contracts=your_contracts,
                             total_contracts=total, price=price, theta=theta)
        if not v:
            continue
        print(f"{total:>14,.0f}{v['share'] * 100:>11.1f}%{v['reward']:>11,.0f}"
              f"{v['per_share'] * 100:>12.2f}c{v['covers_fee'] * 100:>8.0f}%"
              f"{v['net']:>11,.0f}")
    be = pool / fee_ps if fee_ps else float("inf")
    print(f"\n  The pool pays the taker fee exactly at {be:,.0f} total "
          f"contracts.")
    print(f"  Below that, trading is fee-free or better. Above it, you are "
          f"paying to farm.")
    print("  Your own volume is IN the denominator, so farming harder moves")
    print("  the market toward the bad side of that line.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--volume", type=float, default=30_000.0,
                    help="filled notional per day, in dollars")
    ap.add_argument("--price", type=float, default=0.40,
                    help="average fill price (default 0.40)")
    ap.add_argument("--theta", type=float, default=0.07,
                    help="taker fee coefficient (crypto, July 2026)")
    ap.add_argument("--rebate-share", type=float, default=DEFAULT_REBATE_SHARE,
                    help="fraction of taker fees rebated to makers")
    ap.add_argument("--bet", type=float, default=2.50)
    ap.add_argument("--rounds-per-day", type=float, default=288.0)
    ap.add_argument("--pool", type=float, default=0.0,
                    help="this market's daily liquidity-reward pool, if any "
                         "(read it with rewards_check.py)")
    ap.add_argument("--pool-share", type=float, default=0.10,
                    help="your expected share of that pool by score")
    ap.add_argument("--volume-pool", type=float, default=0.0,
                    help="a VOLUME INCENTIVE pool for one contract, in dollars "
                         "(Polymarket US; see the entity warning)")
    ap.add_argument("--your-contracts", type=float, default=25_000.0,
                    help="contracts (shares) you trade into that pool")
    args = ap.parse_args(argv)

    if UNVERIFIED and args.rebate_share == DEFAULT_REBATE_SHARE:
        print(f"! rebate share {DEFAULT_REBATE_SHARE:.0%} is a SECONDARY-SOURCE "
              f"figure, not a first-hand read.")
        print("! verify it for this market with rewards_check.py before "
              "sizing anything on it.")
    report(args.volume, args.theta, args.rebate_share,
           (0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70))
    report_scale(args.volume, args.price, args.theta, args.rebate_share,
                 args.bet, args.rounds_per_day)
    if args.pool > 0:
        report_liquidity_pool(args.pool, args.pool_share, args.volume,
                              args.price, args.theta, args.rebate_share)
    if args.volume_pool > 0:
        report_volume_incentive(
            args.volume_pool, args.your_contracts,
            (100_000, 250_000, 500_000, 1_000_000, 2_500_000, 5_000_000),
            args.price, args.theta)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
