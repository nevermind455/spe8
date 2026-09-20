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
import time

# An accepted order is not a zero fill until late private-stream fills can no
# longer arrive. Rounds resolve at window_end; this is the grace period after
# it before UNRESOLVED becomes ACCEPTED_ZERO_FILL.
FINALISE_AFTER_S = 900.0

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


def fill_delay_section(journal: pathlib.Path, pos: dict) -> None:
    """How paper's edge changes when the same fills happen later.

    Reads rows written by PaperBroker's fill-delay probes. Each order's first
    row is the fill paper actually made; later rows re-quote the same FOK
    against the book seconds afterwards. An order that no longer fills at a
    delay is one a slower live order would likely have missed, and "missed
    won" says whether those misses were winners.
    """
    if not journal.is_file():
        return
    rows = []
    for line in journal.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    paid = {r.get("order_id"): r.get("average_price") for r in rows
            if r.get("reason") == "actual paper fill"}
    probes = [r for r in rows if r.get("reason") != "actual paper fill"]
    delays = {float(r.get("delay_s") or 0.0) for r in probes}
    # Compare the same orders at every delay. A probe that measured nothing
    # (round already closed, book unreadable, bot stopped first) removes its
    # order from every row - otherwise the longer delays would silently drop
    # late-round fills the paper row keeps, and the rows would compare
    # different trades.
    measured = collections.defaultdict(set)
    for r in probes:
        if r.get("fillable") is not None:
            measured[r.get("order_id")].add(float(r.get("delay_s") or 0.0))
    complete = {oid for oid in paid if delays and measured.get(oid) == delays}
    PAPER = -1.0   # sort key for the fill paper actually made
    groups = (("all fills", lambda px: True),
              ("0.70-0.80", lambda px: 0.70 <= px < 0.80))
    for title, keep in groups:
        stats = collections.defaultdict(lambda: {
            "orders": 0, "fill": 0, "won": 0, "px": 0.0,
            "miss": 0, "miss_won": 0})
        dropped = set()
        for r in rows:
            oid = r.get("order_id")
            base_px = paid.get(oid)
            if base_px is None or not keep(base_px):
                continue
            q = pos.get(str(r.get("token_id")))
            if not q or not q.get("settled"):
                continue
            if oid not in complete:
                dropped.add(oid)
                continue
            won = (q.get("payout_per_share") or 0.0) > 0.9
            key = (PAPER if r.get("reason") == "actual paper fill"
                   else float(r.get("delay_s") or 0.0))
            s = stats[key]
            s["orders"] += 1
            if r["fillable"]:
                s["fill"] += 1
                s["won"] += won
                s["px"] += float(r.get("average_price") or 0.0)
            else:
                s["miss"] += 1
                s["miss_won"] += won
        if not any(s["orders"] for s in stats.values()):
            if dropped:
                print(f"\nFILL DELAY  {title}: {len(dropped)} settled orders, none "
                      f"measured at every delay yet")
            continue
        print(f"\nFILL DELAY  {title}  (same settled orders at every delay; "
              f"seconds after the decision)")
        if dropped:
            print(f"            {len(dropped)} excluded: a probe measured nothing "
                  f"(round closed, book unreadable, or bot stopped first)")
        print(f"{'delay':>7}{'orders':>8}{'fill':>6}{'won':>8}{'avg px':>8}"
              f"{'EDGE':>8}{'z / p':>15}{'missed':>8}{'missed won':>12}")
        for delay in sorted(stats):
            s = stats[delay]
            label = "  paper" if delay == PAPER else f"{delay:>6.2f}s"
            if not s["orders"]:
                continue
            if s["fill"]:
                win = s["won"] / s["fill"]
                px = s["px"] / s["fill"]
                middle = (f"{win * 100:>7.1f}%{px:>8.3f}{win - px:>+8.3f}"
                          f"  {sig(s['fill'], win, px):<13}")
            else:
                middle = f"{'--':>8}{'--':>8}{'--':>8}{'':>15}"
            missed = (f"{s['miss_won'] / s['miss'] * 100:>11.1f}%"
                      if s["miss"] else f"{'--':>12}")
            print(f"{label}{s['orders']:>8}{s['fill']:>6}{middle}"
                  f"{s['miss']:>8}{missed}")
        print("   If EDGE falls as delay grows, paper's edge depends on filling")
        print("   faster than live can. 'missed won' above 'won' means the slower")
        print("   fills lose the winners - the adverse selection live showed.")


def _read_events(path: pathlib.Path) -> list:
    """Every parseable row of an append-only journal, in file order."""
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def integrity_report(attempts: list, lots: list) -> tuple:
    """(verdict, findings) over the raw event data.

    A session that fails this must not be used to calibrate PAPER: the
    comparator's numbers are only as trustworthy as the events under them.

    Deliberately returns NO DATA rather than PASS when there is nothing to
    check. A green light from an empty journal is the exact failure mode this
    report exists to prevent.
    """
    findings = []

    def fail(name, offenders):
        offenders = list(offenders)
        if offenders:
            findings.append((name, len(offenders), offenders[:3]))

    by_attempt = collections.defaultdict(list)
    for row in attempts:
        if row.get("attempt_id"):
            by_attempt[row["attempt_id"]].append(row)
    if not by_attempt:
        return "NO DATA", [("no attempt telemetry on record", 0, [])]

    # 1. one genesis event per attempt
    fail("attempt ids with more than one genesis event",
         [aid for aid, rows in by_attempt.items()
          if sum(1 for r in rows if r.get("event") in ("attempt", "blocked")) > 1])

    # 2. duplicate authoritative trade ids
    seen_trades = collections.Counter(
        l.get("trade_id") or l.get("order_id") for l in lots)
    fail("duplicate fill/trade ids",
         [tid for tid, n in seen_trades.items() if tid and n > 1])

    # 3. lifecycle contradiction: never submitted, yet submitted
    fail("attempts both blocked before submission and submitted",
         [aid for aid, rows in by_attempt.items()
          if any(r.get("event") == "blocked" for r in rows)
          and any(r.get("event") == "post" for r in rows)])

    # 4. more than one acceptance for one intended order
    fail("attempts accepted more than once",
         [aid for aid, rows in by_attempt.items()
          if sum(1 for r in rows
                 if r.get("final_status") == "ACCEPTED") > 1])

    # 5. accepted without an order id to correlate on
    fail("accepted submissions with no order id",
         [aid for aid, rows in by_attempt.items()
          if any(r.get("final_status") == "ACCEPTED" and not r.get("order_id")
                 for r in rows)])

    # 6. negative latency
    fail("negative POST latency",
         [r.get("attempt_id") for r in attempts
          if isinstance(r.get("post_seconds"), (int, float))
          and r["post_seconds"] < 0])

    # 7. impossible ordering within one attempt
    out_of_order = []
    for aid, rows in by_attempt.items():
        genesis = next((r for r in rows if r.get("event") == "attempt"), None)
        if not genesis:
            continue
        stamps = [genesis.get(k) for k in
                  ("decision_wall", "book_read_wall", "wall")]
        stamps = [s for s in stamps if isinstance(s, (int, float))]
        if stamps != sorted(stamps):
            out_of_order.append(aid)
            continue
        posts = [r for r in rows if r.get("event") == "post"]
        for post in posts:
            sub = post.get("submitted_wall")
            resp = post.get("responded_wall")
            if (isinstance(sub, (int, float)) and isinstance(resp, (int, float))
                    and resp < sub):
                out_of_order.append(aid)
                break
            if (isinstance(sub, (int, float)) and stamps
                    and sub < stamps[0]):
                out_of_order.append(aid)
                break
    fail("events timestamped in an impossible order", out_of_order)

    # 8. fills that predate the submission that supposedly caused them, and
    #    fills with no attempt at all. Both are restricted to the window the
    #    telemetry actually covers: a ledger that predates the journal would
    #    otherwise make every historical fill look orphaned.
    submitted_at = {}
    for rows in by_attempt.values():
        for r in rows:
            oid = r.get("order_id")
            sub = r.get("submitted_wall")
            if oid and isinstance(sub, (int, float)):
                submitted_at[oid] = min(submitted_at.get(oid, sub), sub)
    covered_from = min(
        (r["wall"] for r in attempts if isinstance(r.get("wall"), (int, float))),
        default=None)
    in_window = [l for l in lots
                 if covered_from is not None
                 and isinstance(l.get("wall"), (int, float))
                 and l["wall"] >= covered_from]
    fail("confirmed fills with no recorded attempt",
         [l.get("order_id") for l in in_window
          if l.get("order_id") not in submitted_at])
    fail("fills timestamped before their own submission",
         [l.get("order_id") for l in in_window
          if l.get("order_id") in submitted_at
          and l["wall"] < submitted_at[l["order_id"]]])

    return ("PASS" if not findings else "FAIL"), findings


def integrity_section(attempts_path: pathlib.Path, lots: list) -> str:
    """Print the integrity gate and return its verdict."""
    attempts = _read_events(attempts_path)
    verdict, findings = integrity_report(attempts, lots)
    print(f"\nEVENT CHECK {verdict}   (telemetry integrity gate)")
    if verdict == "NO DATA":
        print("            no live attempt telemetry on record for this "
              "ledger.")
        print("            Nothing here is evidence about live execution, and")
        print("            PAPER must not be calibrated from this session.")
        return verdict
    print(f"            {len(attempts)} events over "
          f"{len({r.get('attempt_id') for r in attempts})} attempts")
    for name, count, examples in findings:
        print(f"            {count:>5}  {name}")
        print(f"                   e.g. {', '.join(str(e)[:24] for e in examples)}")
    if verdict == "PASS":
        print("            no duplicate attempts or trade ids, no orphan "
              "fills,")
        print("            no impossible ordering, no negative latency.")
    else:
        print("            This session is NOT valid for PAPER/LIVE "
              "calibration.")
    return verdict


def readiness_section(path: pathlib.Path) -> None:
    """Decisions LIVE refused to execute that PAPER would have taken.

    _execution_ready gates LIVE only: paper is self-accounting, live requires
    its private fill stream. That is a deliberate safety gate and stays, but
    it means paper trades LIVE structurally will not - so part of paper's
    apparent edge may come from orders live could never have placed. This
    counts them and says why.
    """
    if not path.is_file():
        return
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("event") == "execution_blocked":
            rows.append(row)
    if not rows:
        return
    reasons = collections.Counter(r.get("block_reason") or "unknown"
                                  for r in rows)
    notional = sum(float(r.get("requested_notional") or 0.0) for r in rows)
    print(f"\nREADINESS   {len(rows)} decisions PAPER would have taken and "
          f"LIVE refused")
    print(f"            ${notional:,.2f} of intended notional never submitted")
    for reason, n in reasons.most_common():
        print(f"            {n:>5}  {reason}")
    print("            These are not rejections by the venue: the order was")
    print("            never sent. A paper edge built on them is not live-")
    print("            reachable. Hypothetical P&L needs the round outcomes,")
    print("            joined by window and token from the ledger.")


def attempts_section(path: pathlib.Path, orders: dict) -> None:
    """What LIVE actually did with every intended order.

    The ledger journals only orders that matched, so orders that were
    rejected or never filled left no trace and the live fill rate was
    unknowable. This reads the append-only attempt journal and joins the
    accepted ones to the confirmed lots by order id, which is the first time
    expected-vs-actual execution can be compared at all.
    """
    if not path.is_file():
        return
    events = collections.defaultdict(list)
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("attempt_id"):
            events[row["attempt_id"]].append(row)
    if not events:
        return

    outcome = collections.Counter()
    posts, fill_lat = [], []
    px_err, qty_err = [], []
    for aid, rows in events.items():
        attempt = next((r for r in rows if r.get("event") == "attempt"), None)
        blocked = any(r.get("event") == "blocked" for r in rows)
        post_rows = [r for r in rows if r.get("event") == "post"]
        posts += [float(r["post_seconds"]) for r in post_rows
                  if isinstance(r.get("post_seconds"), (int, float))]
        if blocked:
            outcome["blocked before submission"] += 1
            continue
        accepted = [r for r in post_rows if r.get("final_status") == "ACCEPTED"]
        if not post_rows:
            outcome["no submission recorded"] += 1
            continue
        if any(r.get("final_status") == "SUBMIT_FAILED" for r in post_rows):
            outcome["submission failed"] += 1
            continue
        if not accepted:
            outcome["REJECTED"] += 1
            continue
        oid = accepted[-1].get("order_id")
        got = orders.get(oid)
        if not got or got.get("sh", 0) <= 0:
            # An accepted order with no confirmed lot is NOT a zero fill
            # until its round is over: a private-stream fill can arrive late,
            # and classifying at POST time would invent zero fills. Before
            # the round ends the honest label is UNRESOLVED.
            window_end = float(attempt.get("window_end") or 0) if attempt else 0
            settled_by = window_end + FINALISE_AFTER_S
            outcome["ACCEPTED_ZERO_FILL" if window_end and time.time() > settled_by
                    else "UNRESOLVED (awaiting late fills)"] += 1
            continue
        expected = float((attempt or {}).get("expected_shares") or 0.0)
        if expected and got["sh"] + 1e-9 < expected:
            outcome["ACCEPTED_PARTIAL"] += 1
        else:
            outcome["ACCEPTED_FILLED"] += 1
        if attempt:
            exp_sh = attempt.get("expected_shares")
            exp_px = attempt.get("expected_vwap")
            if exp_sh:
                qty_err.append(got["sh"] / float(exp_sh) - 1.0)
            if exp_px and got["sh"] > 0:
                px_err.append(got["notional"] / got["sh"] - float(exp_px))
            first_post = min((float(r["submitted_wall"]) for r in post_rows
                              if isinstance(r.get("submitted_wall"), (int, float))),
                             default=None)
            if first_post is not None and got.get("wall"):
                fill_lat.append(got["wall"] - first_post)

    total = sum(outcome.values())
    print(f"\nATTEMPTS    {total} intended live orders "
          f"({len(events)} journalled)")
    for name, n in outcome.most_common():
        print(f"            {name:<32}{n:>5}  {n / total * 100:>5.1f}%")
    filled = (outcome.get("ACCEPTED_FILLED", 0)
              + outcome.get("ACCEPTED_PARTIAL", 0))
    print(f"            LIVE FILL RATE {filled / total * 100:.1f}%"
          f"  - the number PAPER has to reproduce")

    def q(xs, frac):
        return sorted(xs)[min(len(xs) - 1, int(len(xs) * frac))]

    if posts:
        print(f"            POST latency   p50 {statistics.median(posts):.3f}s"
              f"   p95 {q(posts, 0.95):.3f}s   p99 {q(posts, 0.99):.3f}s")
    if fill_lat:
        print(f"            submit->fill   p50 {statistics.median(fill_lat):.1f}s"
              f"   p95 {q(fill_lat, 0.95):.1f}s")
    if px_err:
        ticks = [e / 0.01 for e in px_err]
        print(f"            VWAP error vs the book it saw: median "
              f"{statistics.median(px_err):+.4f} ({statistics.median(ticks):+.1f} ticks)")
    if qty_err:
        print(f"            filled quantity vs expected: median "
              f"{statistics.median(qty_err) * 100:+.1f}%")
    print("            Compare LIVE FILL RATE with the paper report's")
    print("            paper_filled share before trusting any paper P&L.")


def live_timing_section(path: pathlib.Path, orders: dict) -> None:
    """Real POST -> venue response -> fill confirmation times, per order."""
    if not path.is_file():
        return
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    posts = sorted(float(r["post_seconds"]) for r in rows
                   if isinstance(r.get("post_seconds"), (int, float)))
    confirm = sorted(
        orders[r["order_id"]]["wall"] - float(r["submitted_wall"])
        for r in rows
        if r.get("order_id") in orders
        and isinstance(r.get("submitted_wall"), (int, float)))
    if not posts and not confirm:
        return

    def q(xs, p):
        return xs[min(len(xs) - 1, int(len(xs) * p))]

    print(f"\nTIMING      {len(rows)} live POSTs recorded")
    if posts:
        print(f"            POST -> venue response  median "
              f"{statistics.median(posts):.3f}s   p90 {q(posts, 0.9):.3f}s")
    if confirm:
        print(f"            POST -> fill confirmed  median "
              f"{statistics.median(confirm):.3f}s   p90 {q(confirm, 0.9):.3f}s"
              f"   ({len(confirm)} fills)")
    print("            The response time is the closer measure of matching; the")
    print("            confirmation also waits for the trade to settle on chain.")
    print("            Calibrate PAPER_LATENCY_MS and the fill-delay probes to it.")


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

    fill_delay_section(
        p["audit"].with_name(f"{p['audit'].stem}_fill_delay.jsonl"), pos)
    settlement_section(pos)
    if open_pos:
        print(f"\nOPEN        {len(open_pos)} unsettled, ${frozen:,.2f} frozen")
    return 0


# ====================================================================== LIVE
def auth_window_marks(marks, first: float, last: float) -> bool:
    """True when at least one balance read falls inside the trading window."""
    return any(first <= m[0] <= last for m in marks)


def live_report(base: pathlib.Path) -> int:
    """The same questions, answered from what LIVE actually records.

    LIVE keeps no paper account file and no order journal. Its evidence is
    the ledger: confirmed BUY lots (from the private fill stream or REST
    reconcile), the authorization written before each order was sent, and
    balance reads taken from the venue. A FOK that crosses several price
    levels arrives as several trades, so lots are regrouped by order id and
    each order is counted once.

    The ledger records what resolution PAID, not whether that money reached
    the wallet. On a Polymarket proxy wallet winnings came back as cash during
    the session and were re-spent, so they must not be reported as "owed".
    Whether any is still claimable is a question for polymarket.com, not for
    this file.
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
        # Do NOT call this "owed". An earlier version did, and on a Polymarket
        # proxy wallet the winnings had already come back as cash and been
        # re-spent - the operator went to redeem $464.45 that did not exist.
        # The ledger knows what resolution paid, not whether it reached the
        # wallet, so the report says exactly that and no more.
        print(f"            winners paid out ${winnings:,.2f} over the session "
              f"(against ${turnover:,.2f} bought)")
        print("            If Portfolio shows nothing to claim, that money already "
              "came back and was re-spent.")
        if len(marks) >= 1 and winnings and not auth_window_marks(marks, first, last):
            print(f"            No balance reads during trading, so the starting "
                  f"wallet is unknown; if every")
            print(f"            winner was redeemed it was about "
                  f"${b1 - realized:,.2f} (last read minus realized P&L).")

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
    integrity_section(
        path("LIVE_ATTEMPTS_PATH", "live_attempts.jsonl"),
        [l for q in pos.values() for l in (q.get("lots") or [])
         if str(l.get("side", "")).upper() == "BUY"])
    readiness_section(path("LIVE_DECISIONS_PATH", "live_decisions.jsonl"))
    attempts_section(path("LIVE_ATTEMPTS_PATH", "live_attempts.jsonl"), orders)
    live_timing_section(
        path("LIVE_FILL_TIMING_PATH", "live_fill_timing.jsonl"), orders)
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
