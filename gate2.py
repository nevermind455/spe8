"""Gate 2: re-price real fills under deliberately hostile assumptions.

Gate 1 asks whether a strategy made money on the record it kept. It is
answered by the same book that chose the strategy, so it flatters. Gate 2
asks the questions live trading asks anyway:

  - you pay the WORST price your order touched, not the average
  - you arrive LATE, a tick or two after the move started
  - some orders NEVER FILL, and not at random: the ones you miss are
    disproportionately the ones that would have won

and then the question that decides everything:

  - does the band you picked still work on sessions you did not pick it on?

Read-only. It reprices fills that actually happened; nothing is invented
except the penalties, which are labelled. Run it on paper (default) or on
the live ledger (--live). A strategy that only looks good before this file
runs does not have an edge - it has a sample.

    python gate2.py                # pooled paper record + archived sessions
    python gate2.py --live         # the live ledger's own settled orders
    python gate2.py <dir>          # one archived session
"""
from __future__ import annotations

import collections
import json
import os
import pathlib
import random
import sys

import analyze_session as A

TICK = 0.01
ROOT = A.ROOT
# Live's unfilled attempts are not a random sample of its attempts. Pointing
# at the eventual winner more often than the filled ones is the signature of
# adverse selection, so the non-fill scenarios drop winners preferentially by
# this many points. Set it from what a session measures, not from taste.
ADVERSE_POINTS = 0.11


def _fee(shares: float, px: float, rate: float) -> float:
    """Polymarket taker fee: rate * shares * p * (1-p)."""
    return rate * shares * px * (1.0 - px)


def paper_orders(base: pathlib.Path) -> list:
    """Every settled paper fill under ``base``, plus any archived sessions."""
    out: list = []
    seen: set = set()
    roots = ([base] + sorted((base / "archive").glob("*"))
             if base == ROOT else [base])
    for d in roots:
        for audit in sorted(d.glob("*orders*.jsonl")):
            # Pair each audit file with its OWN profile's ledger. A directory
            # can hold several profiles (multi_, paper_, signal_flip_v1_) plus
            # index files that are not ledgers at all, and crossing them would
            # silently score one strategy's fills against another's outcomes.
            stem = audit.name.rsplit("orders", 1)[0].rstrip("_")
            led = d / f"{stem}_ledger.json" if stem else None
            if led is None or not led.is_file():
                led = next((c for c in sorted(d.glob("*ledger*.json"))
                            if c.is_file() and not c.name.startswith("_")), None)
            if led is None:
                continue
            try:
                doc = json.loads(led.read_text(encoding="utf-8"))
                pos = doc.get("positions", {}) if isinstance(doc, dict) else {}
                rows = [json.loads(x) for x in
                        audit.read_text(encoding="utf-8").splitlines() if x.strip()]
            except Exception as exc:
                print(f"  skipped {audit.name}: {type(exc).__name__}: {exc}")
                continue
            if not pos:
                continue
            for r in rows:
                oid = r.get("order_id")
                if r.get("status") != "FILLED" or not oid or oid in seen:
                    continue
                q = pos.get(str(r.get("token_id")))
                if not q or not q.get("settled") or q.get("payout_per_share") is None:
                    continue
                notional = float(r.get("requested_amount") or 0.0)
                avg = float(r.get("average_price") or 0.0)
                if not notional or not avg:
                    continue
                seen.add(oid)
                out.append({
                    "id": oid, "notional": notional, "avg": avg,
                    "worst": float(r.get("worst_price") or avg),
                    "fee": float(r.get("fee") or 0.0),
                    "rate": float(r.get("fee_rate") or 0.07),
                    "won": float(q["payout_per_share"]) > 0.9,
                    "src": f"{audit.parent.name}/{stem or 'paper'}",
                })
    return out


def live_orders(path: pathlib.Path) -> list:
    """Settled live orders, lots regrouped by order id as live_report does."""
    try:
        pos = json.loads(path.read_text(encoding="utf-8")).get("positions", {})
    except Exception as exc:
        print(f"cannot read {path}: {type(exc).__name__}: {exc}")
        return []
    grouped: dict = {}
    for q in pos.values():
        if not q.get("settled") or q.get("payout_per_share") is None:
            continue
        for lot in q.get("lots") or []:
            if str(lot.get("side", "")).upper() != "BUY":
                continue
            oid = lot.get("order_id") or lot.get("trade_id")
            o = grouped.setdefault(oid, {
                "id": oid, "sh": 0.0, "notional": 0.0, "fee": 0.0,
                "won": float(q["payout_per_share"]) > 0.9, "src": "live"})
            o["sh"] += lot["shares"]
            o["notional"] += lot["shares"] * lot["price"]
            o["fee"] += lot.get("fee") or 0.0
    out = []
    for o in grouped.values():
        if o["sh"] <= 0 or o["notional"] <= 0:
            continue
        px = o["notional"] / o["sh"]
        # Live records no per-level detail, so the worst price it touched is
        # unknown; its average is the only honest starting point.
        out.append({**o, "avg": px, "worst": px, "rate": 0.07})
    return out


def evaluate(orders, *, mode="avg", late=0, drop=0.0, adverse=0.0, seed=0):
    """Whole-book result under one set of assumptions.

    Spending is held constant: a worse price buys fewer shares for the same
    dollars, which is what actually happens to a market order.
    """
    rng = random.Random(seed)
    cost = payout = pxsum = 0.0
    won = n = 0
    for o in orders:
        if drop:
            p = drop + (adverse if o["won"] else -adverse)
            if rng.random() < min(1.0, max(0.0, p)):
                continue
        px = (o["avg"] if mode == "avg" else o["worst"]) + late * TICK
        if px >= 1.0:          # priced out of the book entirely
            continue
        shares = o["notional"] / px
        cost += o["notional"] + _fee(shares, px, o["rate"])
        payout += shares if o["won"] else 0.0
        pxsum += px
        won += o["won"]
        n += 1
    if not n:
        return None
    win, avg_px = won / n, pxsum / n
    return {"n": n, "win": win, "px": avg_px, "edge": win - avg_px,
            "pnl": payout - cost, "cost": cost,
            "ret": (payout - cost) / cost * 100 if cost else 0.0}


def _row(label: str, r, spread=None) -> None:
    if not r:
        print(f"{label:<34}{'nothing survives':>24}")
        return
    tail = f"  [{spread[0]:+.0f} .. {spread[-1]:+.0f}]" if spread else ""
    print(f"{label:<34}{r['n']:>6}{r['win'] * 100:>7.1f}%{r['px']:>8.3f}"
          f"{r['edge']:>+8.3f}{r['pnl']:>+10.2f}{r['ret']:>8.1f}%  "
          f"{A.sig(r['n'], r['win'], r['px'])}{tail}")


def poison(orders, title: str) -> None:
    """The gate itself: the same book, assumed worse and worse."""
    print(f"\n{title}")
    print(f"{'scenario':<34}{'n':>6}{'won':>8}{'avg px':>8}{'EDGE':>8}"
          f"{'P&L $':>10}{'return':>8}  {'z / p':<14}")
    _row("1 as recorded", evaluate(orders))
    _row("2 worst price on every fill", evaluate(orders, mode="worst"))
    _row("3   + arrive 1 tick late", evaluate(orders, mode="worst", late=1))
    _row("4   + arrive 2 ticks late", evaluate(orders, mode="worst", late=2))
    for drop, adv, name in ((0.30, 0.0, "5   + 30% never fill (random)"),
                            (0.30, ADVERSE_POINTS, "6   + 30% never fill (adverse)"),
                            (0.50, ADVERSE_POINTS, "7   + 50% never fill (adverse)")):
        # Which orders vanish is random, so one draw proves nothing: 200 draws,
        # reported at the median with the full range beside it.
        trials = [t for t in (evaluate(orders, mode="worst", late=1, drop=drop,
                                       adverse=adv, seed=s) for s in range(200)) if t]
        if not trials:
            _row(name, None)
            continue
        trials.sort(key=lambda t: t["pnl"])
        _row(name, trials[len(trials) // 2], [t["pnl"] for t in trials])
    print("   Scenario 6 is the one that matters: it assumes the orders you")
    print("   miss are the ones that would have won. That is what latency buys.")


def by_session(orders) -> None:
    """Per session, so one lucky run cannot hide inside a pooled average."""
    groups = sorted({o["src"] for o in orders})
    if len(groups) < 2:
        return
    print("\nPER SESSION (0.70-0.80 band, unpoisoned)")
    print(f"{'session':<28}{'n':>6}{'won':>8}{'avg px':>8}{'EDGE':>8}"
          f"{'P&L $':>10}  {'z / p':<14}")
    edges = []
    for name in groups:
        r = evaluate([o for o in orders
                      if o["src"] == name and 0.70 <= o["avg"] < 0.80])
        if not r:
            continue
        edges.append(r["edge"])
        print(f"{name[:27]:<28}{r['n']:>6}{r['win'] * 100:>7.1f}%{r['px']:>8.3f}"
              f"{r['edge']:>+8.3f}{r['pnl']:>+10.2f}  "
              f"{A.sig(r['n'], r['win'], r['px'])}")
    if len(edges) >= 3:
        print(f"   Edge ranges {min(edges):+.3f} to {max(edges):+.3f} "
              f"(spread {max(edges) - min(edges):.3f}).")
        print("   A spread far wider than the z columns allow means these sessions")
        print("   are not one process - so pooling them, or keeping the good ones,")
        print("   both mislead.")


def walk_forward(orders) -> None:
    """Pick the band without seeing the test session, then trade it there.

    Choosing a price band on the same sessions used to judge it is how a
    losing strategy passes gate 1. This holds each session out in turn.
    """
    groups = sorted({o["src"] for o in orders})
    if len(groups) < 3:
        return
    bands = [(0.15, 0.30), (0.30, 0.50), (0.50, 0.70),
             (0.70, 0.80), (0.80, 0.90), (0.90, 1.0)]
    print("\nWALK-FORWARD: band chosen WITHOUT the held-out session, traded on it")
    print(f"{'held-out session':<28}{'band':>12}{'n':>6}{'won':>8}{'avg px':>8}"
          f"{'EDGE':>8}{'P&L $':>10}")
    kept: list = []
    for held in groups:
        train = [o for o in orders if o["src"] != held]
        scored = []
        for lo, hi in bands:
            r = evaluate([o for o in train if lo <= o["avg"] < hi])
            if r and r["n"] >= 100:
                scored.append((r["edge"], (lo, hi)))
        if not scored:
            continue
        lo, hi = max(scored)[1]
        sub = [o for o in orders if o["src"] == held and lo <= o["avg"] < hi]
        r = evaluate(sub)
        if not r:
            continue
        kept += sub
        print(f"{held[:27]:<28}{f'{lo:.2f}-{hi:.2f}':>12}{r['n']:>6}"
              f"{r['win'] * 100:>7.1f}%{r['px']:>8.3f}{r['edge']:>+8.3f}"
              f"{r['pnl']:>+10.2f}")
    total = evaluate(kept)
    if total:
        print(f"\n{'OUT-OF-SAMPLE TOTAL':<28}{'':>12}{total['n']:>6}"
              f"{total['win'] * 100:>7.1f}%{total['px']:>8.3f}{total['edge']:>+8.3f}"
              f"{total['pnl']:>+10.2f}   return {total['ret']:+.1f}%   "
              f"{A.sig(total['n'], total['win'], total['px'])}")
        print("   This is the number to believe. If it is not positive here,")
        print("   the edge in gate 1 was the choice of band, not the strategy.")


def main(argv) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    base = pathlib.Path(args[0]).resolve() if args else ROOT
    if "--live" in argv[1:]:
        name = os.environ.get("LEDGER_PATH") or "ledger.json"
        lp = base / pathlib.Path(name).name if base != ROOT else ROOT / name
        orders = live_orders(lp)
        label = f"LIVE {lp.name}"
    else:
        orders = paper_orders(base)
        label = f"PAPER {base.name}"
    if not orders:
        print(f"no settled orders found for {label}")
        return 1
    src = collections.Counter(o["src"] for o in orders)
    print(f"{label}: {len(orders)} settled orders from {len(src)} session(s)")
    for name, count in src.most_common():
        print(f"   {count:>6}  {name}")
    poison(orders, f"EVERYTHING GOING WRONG - {label}, all bands")
    band = [o for o in orders if 0.70 <= o["avg"] < 0.80]
    if band:
        poison(band, f"EVERYTHING GOING WRONG - {label}, 0.70-0.80")
    by_session(orders)
    walk_forward(orders)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
