#!/usr/bin/env python3
"""Session report for the taper/hedge profile. Read-only, safe while trading.

    python analyze_session.py                    # active PAPER profile from .env
    python analyze_session.py archive/paper-XXX  # a specific archived paper run
    python analyze_session.py --live             # the LIVE ledger from .env
    python analyze_session.py --live some/dir    # an archived live ledger

Answers the questions this profile raises, which analyze_pnl.py predates: is
the 2 signal : 1 opposite cadence holding, what is each leg type earning,
where is capital going, and which decision rule would have picked better.
Opens the ledger, order journal and fill log for reading only - it never
imports the trading loop and never writes.

Read EDGE first, and read it beside z/p. A strategy with no skill wins at
about the price it pays; winning BELOW the price paid means the signal is
worse than the venue's own quote, and no parameter setting fixes that. An
EDGE on a few dozen fills is noise however large it looks - this repo has
already retracted one "finding" that was 127 fills of luck.
"""
from __future__ import annotations

import collections
import csv
import json
import math
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

# Split at 0.80. A single ">=0.70" bucket once merged 0.70-0.80 (edge +0.041)
# with 0.80-0.90 (edge -0.056): above 0.70 the win rate plateaus near 78%
# whatever is paid, so the two halves have opposite economics and the merged
# row read as "no edge" while one half had one.
BAND_ORDER = ("<0.15", "0.15-0.30", "0.30-0.50", "0.50-0.70",
              "0.70-0.80", ">=0.80")


def band_of(px: float) -> str:
    return ("<0.15" if px < 0.15 else "0.15-0.30" if px < 0.30 else
            "0.30-0.50" if px < 0.50 else "0.50-0.70" if px < 0.70 else
            "0.70-0.80" if px < 0.80 else ">=0.80")


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


def sig(n: int, win: float, px: float) -> str:
    """z and two-sided p for EDGE = win - px, or a refusal to pretend.

    Normal approximation to the binomial. Below 30 fills it is not worth
    printing a number at all - that is where every retracted result came from.
    """
    if n < 30:
        return "  n<30  noise"
    var = max(win * (1.0 - win), 1e-9)
    z = (win - px) / math.sqrt(var / n)
    p = math.erfc(abs(z) / math.sqrt(2.0))
    return f"{z:+6.2f} p={p:.3f}"


def new_bucket() -> dict:
    return {"n": 0, "cost": 0.0, "pay": 0.0, "px": 0.0, "won": 0}


def print_bands(bands: dict) -> None:
    if not bands:
        return
    print(f"\n{'PRICE BAND':<12}{'fills':>6}{'avg px':>8}{'cost':>10}"
          f"{'P&L':>10}{'return':>9}{'won':>7}{'EDGE':>8}{'fee':>7}"
          f"{'z / p':>15}")
    for band in BAND_ORDER:
        b = bands.get(band)
        if not b or not b["cost"] or not b["n"]:
            continue
        pnl = b["pay"] - b["cost"]
        avg_px = b["px"] / b["n"]
        win = b["won"] / b["n"]
        # The taker fee is theta*(1-p) per dollar of notional, so a cheap
        # fill costs MORE in fees than an expensive one. Shown beside the
        # edge because it is what the edge has to clear.
        fee = 0.07 * (1 - avg_px)
        print(f"{band:<12}{b['n']:>6}{avg_px:>8.3f}{b['cost']:>10.2f}"
              f"{pnl:>+10.2f}{pnl / b['cost'] * 100:>8.1f}%"
              f"{win * 100:>6.0f}%{win - avg_px:>+8.3f}{fee * 100:>6.1f}%"
              f"  {sig(b['n'], win, avg_px)}")
    print("   EDGE is gross of fees; the fee column is what it must clear.")
    print("   Ignore any row that is not significant, however good it looks.")


def settlement_section(pos: dict) -> None:
    lat = []
    for q in pos.values():
        if q.get("settled") and q.get("settled_wall") and q.get("lots"):
            end = timer.window_start(min(l["wall"] for l in q["lots"])) + 300
            lat.append(q["settled_wall"] - end)
    if not lat:
        return
    # Percentiles, not min/max: a position whose first lot lands the far side
    # of a window boundary can attribute to the wrong round and read as
    # negative, and one abandoned round drags the maximum into days. Neither
    # says anything about how settlement is actually performing.
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


def pairs_section(held: dict) -> None:
    """held: condition -> token -> [shares, cost]. Two tokens = both legs."""
    rounds = under = 0
    locked = matched = skew = worst = 0.0
    for tokens in held.values():
        if len(tokens) != 2:
            continue
        (s1, c1), (s2, c2) = tokens.values()
        if s1 <= 0 or s2 <= 0:
            continue
        pair_cost = c1 / s1 + c2 / s2
        m = min(s1, s2)
        rounds += 1
        matched += m
        locked += (1.0 - pair_cost) * m
        skew += abs(s1 - s2)
        under += pair_cost < 1.0
        worst = max(worst, pair_cost)
    if not rounds:
        return
    # A matched UP+DOWN pair always pays exactly $1.00, so any pair that cost
    # more is a certain loss - no sample size question applies. On the taper
    # test this was 97% of a $149.55 loss.
    print(f"\nPAIRS       {rounds} rounds held both legs; "
          f"{under}/{rounds} cost under $1.00; worst ${worst:.4f}")
    print(f"            value locked into matched pairs ${locked:+,.2f}"
          f"   ({matched:,.0f} matched sh, {skew:,.0f} skew sh)")


# ===================================================================== PAPER
def paper_report(base: pathlib.Path) -> int:
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
    legs = collections.defaultdict(new_bucket)
    bands = collections.defaultdict(new_bucket)
    held = collections.defaultdict(dict)
    for o in filled:
        cell = held[o["condition_id"]].setdefault(str(o["token_id"]), [0.0, 0.0])
        cell[0] += o["shares"]
        cell[1] += o["total_cost"]
        q = pos.get(str(o["token_id"]))
        if not q or not q.get("settled"):
            continue
        leg = ("HEDGE" if phase_of.get((et(o["wall"]), o["side"]), "")
               .endswith("hedge") else "PRIMARY")
        pay = o["shares"] * (q.get("payout_per_share") or 0.0)
        px = o["average_price"]
        for bucket in (legs[leg], bands[band_of(px)]):
            bucket["n"] += 1
            bucket["cost"] += o["total_cost"]
            bucket["pay"] += pay
            bucket["px"] += px
            bucket["won"] += (pay > 0)

    if legs:
        print(f"\n{'LEG':<9}{'fills':>6}{'avg px':>8}{'cost':>10}{'P&L':>10}"
              f"{'return':>9}{'won':>7}{'EDGE':>8}{'z / p':>15}")
        for k in ("PRIMARY", "HEDGE"):
            d = legs.get(k)
            if not d or not d["n"] or not d["cost"]:
                continue
            avg_px = d["px"] / d["n"]
            win = d["won"] / d["n"]
            pnl = d["pay"] - d["cost"]
            print(f"{k:<9}{d['n']:>6}{avg_px:>8.3f}{d['cost']:>10.2f}"
                  f"{pnl:>+10.2f}{pnl / d['cost'] * 100:>8.1f}%"
                  f"{win * 100:>6.0f}%{win - avg_px:>+8.3f}"
                  f"  {sig(d['n'], win, avg_px)}")
        print("   EDGE = win rate minus average price paid. Negative means the")
        print("   signal is worse than the quote it is paying.")

    print_bands(bands)
    pairs_section(held)

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
        sig_of = {(r.get("time_et"), r.get("side")):
                  (r.get("price_side") or None, r.get("book_side") or None,
                   r.get("chainlink_side") or None) for r in rows}
        rules = {"price": lambda a, b, c: a,
                 "minority": strategy.minority_decision,
                 "final": strategy.final_decision}
        score = {k: [0, 0] for k in rules}
        for o in filled:
            win_side = winner.get(o["condition_id"])
            trip = sig_of.get((et(o["wall"]), o["side"]))
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

    settlement_section(pos)
    if open_pos:
        print(f"\nOPEN        {len(open_pos)} unsettled, ${frozen:,.2f} frozen")
    return 0


# ====================================================================== LIVE
def live_report(base: pathlib.Path) -> int:
    """The same questions, answered from what LIVE actually records.

    LIVE keeps no paper account file and no order journal. Its evidence is
    the ledger: confirmed BUY lots (from the private fill stream or REST
    reconcile), the authorization written before each order was sent, and
    balance reads taken from the venue. A FOK that crosses several price
    levels arrives as several trades, so lots are regrouped by order id and
    each order is counted once.

    Resolution does NOT redeem. A winning position is settled in the ledger
    the moment the chain resolves, but its payout stays as outcome tokens
    until someone redeems it on polymarket.com - so realized P&L here can run
    well ahead of the wallet balance.
    """
    def path(env: str, default: str) -> pathlib.Path:
        name = os.environ.get(env) or default
        return (base / pathlib.Path(name).name if base != ROOT
                else ROOT / name)

    lp = path("LEDGER_PATH", "ledger.json")
    dp = path("BOT_TRADE_LOG_PATH", "trade_log.csv")
    if not lp.is_file():
        print(f"no live ledger at {lp}")
        return 1
    led = json.loads(lp.read_text(encoding="utf-8"))
    pos = led.get("positions", {})
    auth = led.get("authorized_orders") or {}
    marks = [m for m in (led.get("balance_marks") or [])
             if isinstance(m, (list, tuple)) and len(m) == 2]

    orders: dict = {}
    for token, q in pos.items():
        for lot in q.get("lots") or []:
            if str(lot.get("side", "")).upper() != "BUY":
                continue
            oid = lot.get("order_id") or lot.get("trade_id")
            o = orders.setdefault(oid, {
                "token": str(token), "cond": q.get("condition_id"),
                "sh": 0.0, "notional": 0.0, "fee": 0.0, "lots": 0,
                "wall": lot["wall"], "q": q})
            o["sh"] += lot["shares"]
            o["notional"] += lot["shares"] * lot["price"]
            o["fee"] += lot["fee"]
            o["lots"] += 1
            o["wall"] = min(o["wall"], lot["wall"])
    if not orders:
        print(f"live ledger at {lp} holds no confirmed fills yet")
        return 0

    settled = [q for q in pos.values() if q.get("settled")]
    open_pos = [q for q in pos.values()
                if not q.get("settled") and q.get("shares", 0) > 0]
    realized = sum(q.get("realized") or 0.0 for q in settled)
    settled_cost = sum(q["cost"] for q in settled)
    frozen = sum(q["cost"] for q in open_pos)
    fees = sum(o["fee"] for o in orders.values())
    turnover = sum(o["notional"] + o["fee"] for o in orders.values())
    winnings = sum(q["shares"] * float(q.get("payout_per_share") or 0.0)
                   for q in settled)
    first = min(o["wall"] for o in orders.values())
    last = max(o["wall"] for o in orders.values())

    print("=" * 70)
    print(f"  LIVE   {et(first)}  ->  {et(last)}")
    print(f"  {len(orders)} orders   turnover ${turnover:,.2f}   "
          f"fees ${fees:,.2f} ({fees / turnover * 100:.2f}%)")
    line = f"  realized ${realized:+,.2f}"
    if settled_cost:
        line += (f" on ${settled_cost:,.2f} settled"
                 f"   return {realized / settled_cost * 100:+.2f}%")
    print(line)
    print(f"  frozen ${frozen:,.2f} in {len(open_pos)} open position(s)")
    print("=" * 70)

    # ------------------------------------------------------------ wallet --
    if marks:
        (t0, b0), (t1, b1) = marks[0], marks[-1]
        print(f"\nWALLET      first read ${b0:,.2f} ({et(t0)})")
        print(f"            last read  ${b1:,.2f} ({et(t1)})")
        if len(marks) >= 2:
            bought = sum(o["notional"] + o["fee"] for o in orders.values()
                         if t0 <= o["wall"] <= t1)
            credits = (b1 - b0) + bought
            # The only wallet movement the ledger can predict is the cost of
            # confirmed buys. Whatever else moved it came from outside: a
            # redemption, a deposit, or a fill the ledger never saw.
            print(f"            buys in that window ${bought:,.2f}; "
                  f"credits from outside ${credits:+,.2f}")
            print("            (credits = redemptions + deposits - any fills "
                  "the ledger missed)")
        print(f"            settled winnings owed ${winnings:,.2f} - only in "
              f"the wallet once redeemed")

    # --------------------------------------------------------- integrity --
    counters = {k: led.get(k) for k in (
        "duplicates", "skipped_status", "skipped_side",
        "skipped_unauthorized", "skipped_authorization_mismatch")}
    unauthorized = [oid for oid in orders if oid not in auth]
    print(f"\nINTEGRITY   " + "  ".join(f"{k}={v}" for k, v in counters.items()))
    print(f"            fills with no authorization on record: "
          f"{len(unauthorized)}/{len(orders)}")

    # --------------------------------------------------------- execution --
    est = act = 0.0
    headroom, multi = [], 0
    for oid, o in orders.items():
        a = auth.get(oid)
        if o["lots"] > 1:
            multi += 1
        if not a or not o["sh"]:
            continue
        est += float(a.get("estimated_fee") or 0.0)
        act += o["fee"]
        if a.get("price_cap") is not None:
            headroom.append(float(a["price_cap"]) - o["notional"] / o["sh"])
    print(f"\nEXECUTION   {multi}/{len(orders)} orders filled across more "
          f"than one price level")
    if est:
        print(f"            fee: estimated ${est:,.2f}, charged ${act:,.2f} "
              f"({(act - est) / est * 100:+.1f}%)")
    if headroom:
        print(f"            fill price below its cap by median "
              f"{statistics.median(headroom):.3f} "
              f"(min {min(headroom):.3f})")

    # ---------------------------------------------------------- attempts --
    if dp.is_file():
        days = {timer.now_et(o["wall"]).strftime("%b %d") for o in orders.values()}
        rows = [r for r in csv.DictReader(dp.open(encoding="utf-8"))
                if str(r.get("time_et", ""))[:6] in days
                and str(r.get("phase", "")).startswith("phase2")]
        att = collections.Counter(r.get("result", "") for r in rows)
        if att:
            n = sum(att.values())
            print(f"\nATTEMPTS    ({n} phase-2 on the days that traded, "
                  f"from {dp.name})")
            for k, v in att.most_common():
                print(f"   {k:<28}{v:>6} {pct(v, n)}")

    # ----------------------------------------------------------- economics --
    bands = collections.defaultdict(new_bucket)
    total = new_bucket()
    held = collections.defaultdict(dict)
    for o in orders.values():
        cell = held[o["cond"]].setdefault(o["token"], [0.0, 0.0])
        cell[0] += o["sh"]
        cell[1] += o["notional"] + o["fee"]
        q = o["q"]
        if not q.get("settled") or not o["sh"]:
            continue
        px = o["notional"] / o["sh"]
        paid = float(q.get("payout_per_share") or 0.0)
        for bucket in (bands[band_of(px)], total):
            bucket["n"] += 1
            bucket["cost"] += o["notional"] + o["fee"]
            bucket["pay"] += o["sh"] * paid
            bucket["px"] += px
            bucket["won"] += paid > 0.9
    if total["n"]:
        avg_px = total["px"] / total["n"]
        win = total["won"] / total["n"]
        print(f"\nOVERALL     {total['n']} settled orders   avg px {avg_px:.3f}"
              f"   won {win * 100:.1f}%   EDGE {win - avg_px:+.3f}"
              f"   {sig(total['n'], win, avg_px)}")
    print_bands(bands)
    pairs_section(held)
    settlement_section(pos)
    return 0


def main(argv) -> int:
    args = list(argv[1:])
    live = "--live" in args
    args = [a for a in args if a != "--live"]
    base = pathlib.Path(args[0]) if args else ROOT
    return live_report(base) if live else paper_report(base)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
