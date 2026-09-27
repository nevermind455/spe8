#!/usr/bin/env python3
"""What does the venue actually pay a maker in THIS market? Ask it.

    python3 rewards_check.py                 # the current BTC 5m round
    python3 rewards_check.py --window 1758931200
    python3 rewards_check.py --raw            # dump every field found

Why this exists
---------------
Polymarket runs two separate maker-side money flows, and both are configured
PER MARKET rather than venue-wide:

  MAKER REBATES     a share of the taker fees collected in that market, paid
                    back to makers whose limit orders filled, pro-rata by the
                    fee-equivalent volume their liquidity generated.
  LIQUIDITY REWARDS a daily pool paid for RESTING limit orders inside a
                    configured max spread from the adjusted midpoint, scored
                    by size and distance from best, subject to a minimum
                    qualifying size.

Whether the 5-minute BTC up/down markets carry either, and at what numbers, is
not something to assume from a blog post or from a fee schedule that was
rewritten twice in 2026. It is published per market and readable through the
API, so this reads it.

Deliberately does NOT hardcode a field schema. Reward config field names have
moved across API versions, and a checker that looks for one spelling and finds
nothing reports "no rewards" when it means "I looked in the wrong place" -
which is the expensive direction to be wrong in. So it probes several
plausible names, and prints every key it found whose name mentions a reward,
rebate or fee, verbatim. If the output is empty, that is evidence about the
schema as much as about the market, and `--raw` shows you everything.

Read-only. No wallet, no credentials, no orders.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

import market_discovery                       # noqa: E402
import timer                                  # noqa: E402

CLOB = "https://clob.polymarket.com"
# Names seen across API versions and docs. Presence, not absence, is the
# signal here - see the module docstring.
REWARD_HINTS = ("reward", "rebate", "incentive", "fee", "spread", "min_size",
                "minsize", "maker", "taker",
                # Volume caps how much maker flow you can possibly capture:
                # you cannot make $30k/day in a market that trades $3k.
                "volume", "liquidity", "openinterest", "open_interest")


def _interesting(obj, path="") -> list[tuple[str, object]]:
    """Every leaf whose key path mentions rewards, rebates or fees."""
    out = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else str(key)
            if isinstance(value, (dict, list)):
                out += _interesting(value, here)
            elif any(h in str(key).lower() for h in REWARD_HINTS):
                out.append((here, value))
    elif isinstance(obj, list):
        for i, value in enumerate(obj[:8]):
            out += _interesting(value, f"{path}[{i}]")
    return out


def _get(url, **params):
    import http_pool
    try:
        r = http_pool.get(url, params=params or None, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def check(window: int | None, raw: bool) -> int:
    now = timer.unix()
    window = timer.window_start(now) if window is None else int(window)
    slug = f"btc-updown-5m-{window}"
    print(f"market: {slug}")
    print(f"venue:  {CLOB}  (gamma: {market_discovery.GAMMA})")
    print("        Polymarket US (polymarket.us) is a SEPARATE exchange with")
    print("        its own incentive programs. Anything documented only there")
    print("        does not apply to this host.\n")

    event = market_discovery._fetch_slug(slug)
    if not isinstance(event, dict):
        print("gamma returned no event for that slug - is the window right?")
        return 1

    markets = market_discovery._as_list(event.get("markets"))
    if not markets:
        print("event carries no markets")
        return 1
    market = markets[0]

    print("--- GAMMA event/market: reward, rebate and fee fields ---")
    found = _interesting(market)
    if found:
        for key, value in found:
            print(f"  {key} = {value!r}")
    else:
        print("  (none found - either this market carries no reward config,")
        print("   or the field names moved. Re-run with --raw and look.)")

    condition = str(market.get("conditionId") or market.get("condition_id") or "")
    tokens = [str(t) for t in market_discovery._as_list(market.get("clobTokenIds"))]

    print("\n--- CLOB market record ---")
    if condition:
        clob = _get(f"{CLOB}/markets/{condition}")
        if "_error" in clob:
            print(f"  unavailable: {clob['_error']}")
        else:
            hits = _interesting(clob)
            for key, value in hits or ():
                print(f"  {key} = {value!r}")
            if not hits:
                print("  (no reward/fee fields in the CLOB record either)")
            for flag in ("enable_order_book", "accepting_orders", "active",
                         "closed", "minimum_order_size", "minimum_tick_size"):
                if flag in clob:
                    print(f"  {flag} = {clob[flag]!r}")
    else:
        print("  no condition id on the market; cannot look it up")

    print("\n--- is this market in the rewards-eligible sampling set? ---")
    sampling = _get(f"{CLOB}/sampling-simplified-markets")
    if "_error" in sampling:
        print(f"  unavailable: {sampling['_error']}")
    else:
        data = sampling.get("data") if isinstance(sampling, dict) else sampling
        ids = set()
        for entry in market_discovery._as_list(data):
            if isinstance(entry, dict):
                ids.add(str(entry.get("condition_id") or entry.get("conditionId") or ""))
        print(f"  sampling set holds {len(ids)} markets")
        if condition:
            inside = condition in ids
            print(f"  this market: {'ELIGIBLE (in the set)' if inside else 'NOT in the set'}")
            if not inside:
                print("  -> no liquidity-reward pool for this market. Maker")
                print("     rebates on taker fees may still apply; check the")
                print("     fields above rather than assuming either way.")

    if raw:
        print("\n--- RAW gamma market ---")
        print(json.dumps(market, indent=1, sort_keys=True)[:6000])
        for token in tokens[:1]:
            print(f"\n--- RAW CLOB book meta for {token} ---")
            print(json.dumps(_get(f"{CLOB}/book", token_id=token),
                             indent=1, sort_keys=True)[:2000])

    print("\n--- the ceiling on maker volume ---")
    for key in ("volume", "volume24hr", "volumeNum", "volume1wk",
                "liquidity", "liquidityNum", "openInterest"):
        if key in market:
            print(f"  {key} = {market[key]!r}")
    print("  Your maker volume cannot exceed the taker flow that arrives.")
    print("  A rebate rate means nothing if the market does not trade.")

    print("\nReminder: makers pay no trading fee, but a maker is filled when")
    print("the market moves against them. At a 0.39 fill the fee saved is")
    print("~428bps of notional, and 1.68pp of win rate lost to adverse")
    print("selection cancels exactly that. Measure it before believing it.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--window", type=int, default=None,
                    help="5-minute window start (default: the current round)")
    ap.add_argument("--raw", action="store_true",
                    help="dump the full records, not just matching fields")
    args = ap.parse_args(argv)
    return check(args.window, args.raw)


if __name__ == "__main__":
    raise SystemExit(main())
