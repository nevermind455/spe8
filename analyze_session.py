#!/usr/bin/env python3
"""Session report for the taper/hedge profile. Read-only, safe while trading.

    python analyze_session.py                    # active profile from .env
    python analyze_session.py archive/paper-XXX  # a specific archived run

Answers the questions this profile raises, which analyze_pnl.py predates: is
the 2 signal : 1 opposite cadence holding, what is each leg type earning,
where is capital going, and which decision rule would have picked better.
Opens the ledger, order journal and fill log for reading only - it never
imports the trading loop and never writes.

Read EDGE first. A strategy with no skill wins at about the price it pays;
winning BELOW the price paid means the signal is worse than the venue's own
quote, and no parameter setting fixes that.
"""
from __future__ import annotations

import collections
import csv
import json
import os
import pathlib
import statistics
import sys

ROOT = pathlib.Path(__file__).parent
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False, encoding="utf-8")
except Exception:
    pass
sys.path.insert(0, str(ROOT))

import timer  # noqa: E402

try:
    import strategy  # noqa: E402
except Exception:
    strategy = None

DEFAULTS = {
    "ledger": ("PAPER_LEDGER_PATH", "paper_ledger.json"),
    "account": ("PAPER_ACCOUNT_PATH", "paper_account.json"),
    "audit": ("PAPER_AUDIT_PATH", "paper_orders.jsonl"),
    "fills": ("PAPER_TRADE_LOG_PATH", "paper_trade_log.csv"),
}


def resolve(base: pathlib.Path) -> dict:
    """Profile paths. An explicit directory overrides the .env profile."""
    out = {}
    for key, (env, default) in DEFAULTS.items():
        name = os.environ.get(env) or default
        out[key] = (base / pathlib.Path(name).name if base != ROOT
                    else ROOT / name)
    return out


def pct(n, d) -> str:
    return f"{n / d * 100:5.1f}%" if d else "   -- "


def et(wall) -> str:
    return timer.now_et(wall).strftime("%b %d %H:%M:%S ET")


def main(argv) -> int:
    base = pathlib.Path(argv[1]) if len(argv) > 1 else ROOT
    p = resolve(base)
    if not p["ledger"].is_file():
        print(f"no ledger at {p['ledger']}")
        return 1

    led = json.loads(p["ledger"].read_text(encoding="utf-8"))
    acct = (json.loads(p["account"].read_text(encoding="utf-8"))
            if p["account"].is_file() else {})
    orders = [json.loads(line) for line
              in p["audit"].read_text(encoding="utf-8").splitlines()
              if line.strip()] if p["audit"].is_file() else []
    rows = (list(csv.DictReader(p["fills"].open(encoding="utf-8")))
            if p["fills"].is_file() else [])
    pos = led.get("positions", {})
    filled = [o for o in orders if o.get("status") == "FILLED"]

    # ------------------------------------------------------------ money --
    start = float(acct.get("starting_balance") or 0.0)
    spent = sum(x["cost"] for x in pos.values())
    payouts = sum(x["shares"] * float(x.get("payout_per_share") or 0.0)
                  for x in pos.values() if x.get("settled"))
    banked = sum(x.get("realized_from_sales") or 0.0 for x in pos.values())
    cash = start - spent + payouts + banked
    open_pos = [x for x in pos.values() if not x.get("settled")]
    frozen = sum(x["cost"] for x in open_pos)
    realized = sum(x.get("realized") or 0.0
                   for x in pos.values() if x.get("settled"))
    settled_cost = sum(x["cost"] for x in pos.values() if x.get("settled"))

    print("=" * 70)
    print(f"  start ${start:,.2f}    realized ${realized:+,.2f}"
          f"    frozen ${frozen:,.2f}    cash ${cash:,.2f}")
    line = f"  equity ${cash + frozen:,.2f}"
    if settled_cost:
        line += (f"    settled turnover ${settled_cost:,.2f}"
                 f"    return {realized / settled_cost * 100:+.2f}%")
    print(line)
    print("=" * 70)

    # --------------------------------------------------------- attempts --
    phase2 = [r for r in rows if str(r.get("phase", "")).startswith("phase2")]
    att = collections.Counter(r.get("result", "") for r in phase2)
    if att:
        n = sum(att.values())
        print(f"\nATTEMPTS  ({n} phase-2)")
        for k, v in att.most_common():
            print(f"   {k:<28}{v:>6} {pct(v, n)}")

    # ---------------------------------------------------------- cadence --
    cad = collections.Counter()
    for r in phase2:
        if r.get("result") == "paper_filled":
            cad["opposite" if r["phase"].endswith("hedge") else "signal"] += 1
    if cad["opposite"]:
        print(f"\nCADENCE   {cad['signal']} signal-side : {cad['opposite']} "
              f"opposite  =  {cad['signal'] / cad['opposite']:.2f} : 1"
              f"   (design 2.00 : 1)")
    elif cad["signal"]:
        print(f"\nCADENCE   {cad['signal']} signal-side, 0 opposite - the cycle "
              f"never reached the hedge slot")

    # ----------------------------------------------- per-leg economics ----
    phase_of = {(r.get("time_et"), r.get("side")): r.get("phase", "")
                for r in rows}
    legs = collections.defaultdict(
        lambda: {"n": 0, "cost": 0.0, "pay": 0.0, "px": 0.0, "won": 0})
    bands = collections.defaultdict(
        lambda: {"n": 0, "cost": 0.0, "pay": 0.0, "px": 0.0, "won": 0})
    for o in filled:
        q = pos.get(str(o["token_id"]))
        if not q or not q.get("settled"):
            continue
        leg = ("HEDGE" if phase_of.get((et(o["wall"]), o["side"]), "")
               .endswith("hedge") else "PRIMARY")
        pay = o["shares"] * (q.get("payout_per_share") or 0.0)
        d = legs[leg]
        d["n"] += 1
        d["cost"] += o["total_cost"]
        d["pay"] += pay
        d["px"] += o["average_price"]
        d["won"] += (pay > 0)
        px = o["average_price"]
        band = ("<0.15" if px < 0.15 else "0.15-0.30" if px < 0.30 else
                "0.30-0.50" if px < 0.50 else "0.50-0.70" if px < 0.70
                else ">=0.70")
        b = bands[band]
        b["n"] += 1
        b["cost"] += o["total_cost"]
        b["pay"] += pay
        b["px"] += px
        b["won"] += (pay > 0)

    if legs:
        print(f"\n{'LEG':<9}{'fills':>6}{'avg px':>8}{'cost':>10}{'P&L':>10}"
              f"{'return':>9}{'won':>7}{'EDGE':>8}")
        for k in ("PRIMARY", "HEDGE"):
            d = legs.get(k)
            if not d or not d["n"] or not d["cost"]:
                continue
            avg_px = d["px"] / d["n"]
            win = d["won"] / d["n"]
            pnl = d["pay"] - d["cost"]
            print(f"{k:<9}{d['n']:>6}{avg_px:>8.3f}{d['cost']:>10.2f}"
                  f"{pnl:>+10.2f}{pnl / d['cost'] * 100:>8.1f}%"
                  f"{win * 100:>6.0f}%{win - avg_px:>+8.3f}")
        print("   EDGE = win rate minus average price paid. Negative means the")
        print("   signal is worse than the quote it is paying.")

    if bands:
        print(f"\n{'PRICE BAND':<12}{'fills':>6}{'avg px':>8}{'cost':>10}"
              f"{'P&L':>10}{'return':>9}{'won':>7}{'EDGE':>8}{'fee':>7}")
        for band in ("<0.15", "0.15-0.30", "0.30-0.50", "0.50-0.70", ">=0.70"):
            b = bands.get(band)
            if not b or not b["cost"] or not b["n"]:
                continue
            pnl = b["pay"] - b["cost"]
            avg_px = b["px"] / b["n"]
            win = b["won"] / b["n"]
            # The taker fee is theta*(1-p) per dollar of notional, so a cheap
            # fill costs MORE in fees than an expensive one. Shown beside the
            # edge because it is what the edge has to clear, and it is a large
            # part of why the low bands read worse than the high ones.
            fee = 0.07 * (1 - avg_px)
            print(f"{band:<12}{b['n']:>6}{avg_px:>8.3f}{b['cost']:>10.2f}"
                  f"{pnl:>+10.2f}{pnl / b['cost'] * 100:>8.1f}%"
                  f"{win * 100:>6.0f}%{win - avg_px:>+8.3f}{fee * 100:>6.1f}%")
        print("   EDGE is gross of fees; the fee column is what it must clear.")

    # --------------------------------------------------- decision rules --
    if strategy is not None:
        side_of = {str(o["token_id"]): o["side"] for o in filled}
        # Derive the winner from a LOSING position too. Recording it only
        # where we happened to hold the winner drops every round the bot got
        # wrong, which is a 12% exclusion of exactly the wrong rounds and
        # biases every hit rate upward. In a binary market a token that paid
        # zero tells us the other side won, which is the same information.
        winner = {}
        for t, q in pos.items():
            if not q.get("settled"):
                continue
            side = side_of.get(str(t))
            pay = q.get("payout_per_share")
            if side is None or pay is None:
                continue
            if pay > 0.9:
                winner[q["condition_id"]] = side
            elif pay < 0.1:
                winner[q["condition_id"]] = "DOWN" if side == "UP" else "UP"
            # a 50/50 resolution has no winning side; leave it unscored
        sig = {(r.get("time_et"), r.get("side")):
               (r.get("price_side") or None, r.get("book_side") or None,
                r.get("chainlink_side") or None) for r in rows}
        rules = {"price": lambda a, b, c: a,
                 "minority": strategy.minority_decision,
                 "final": strategy.final_decision}
        score = {k: [0, 0] for k in rules}
        for o in filled:
            win_side = winner.get(o["condition_id"])
            trip = sig.get((et(o["wall"]), o["side"]))
            if not win_side or not trip:
                continue
            for k, fn in rules.items():
                pick = fn(*trip)
                if pick:
                    score[k][1] += 1
                    score[k][0] += (pick == win_side)
        if any(v[1] for v in score.values()):
            live = os.environ.get("SIGNAL_DECISION_RULE") or "price"
            print(f"\n{'RULE':<10}{'decided':>9}{'correct':>9}{'hit rate':>10}")
            for k, (c, n) in score.items():
                print(f"{k:<10}{n:>9}{c:>9}{pct(c, n):>10}"
                      f"{'  <- live' if k == live else ''}")
            print("   Hit rate is whether the RULE picked the winning side,")
            print("   not whether the position won - a hedge leg buys the")
            print("   complement of the pick on purpose.")

    # ------------------------------------------------------- settlement --
    lat = []
    for q in pos.values():
        if q.get("settled") and q.get("settled_wall") and q.get("lots"):
            end = timer.window_start(min(l["wall"] for l in q["lots"])) + 300
            lat.append(q["settled_wall"] - end)
    if lat:
        # Percentiles, not min/max: a position whose first lot lands the far
        # side of a window boundary can attribute to the wrong round and read
        # as negative, and one abandoned round drags the maximum into days.
        # Neither says anything about how settlement is actually performing.
        lat.sort()
        p90 = lat[min(len(lat) - 1, int(len(lat) * 0.9))]
        print(f"\nSETTLEMENT  {len(lat)} settled   "
              f"median {statistics.median(lat):.0f}s   p90 {p90:.0f}s")
        print(f"            under 120s: {sum(1 for x in lat if x < 120)}"
              f"/{len(lat)}  (on-chain path; the API mirrors lag ~10 min)")
        slow = [x for x in lat if x > 600]
        if slow:
            print(f"            {len(slow)} took over 10 min - fell back to "
                  f"the API mirrors, or the venue stalled")
    if open_pos:
        print(f"\nOPEN        {len(open_pos)} unsettled, ${frozen:,.2f} frozen")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
