"""Tests for gate 1: the fill comes from the recorded ladder, not a price line.

The claims under test, in the order they matter:

  1. FILL      a stake walks real levels. Depth behind the best ask changes
               the average price, and a ladder too thin to reach the venue
               minimum inside the cap is a NO FILL, not a cheap fill.
  2. PARITY    the walk agrees with the one the live/paper order path takes
               (orderbook.venue_minimum_stake), so a backtested fill is a
               fill the bot would really have taken.
  3. SETTLE    PnL is scored against the RECORDED winner and pays exactly $1
               a winning share, minus the taker fee at the price each parcel
               actually traded at.
  4. CONTRAST  the quote engine really is the naive one, and the replay runs
               both on identical signals so the gap it reports is the gap.
  5. TAPE      the recorder's file round-trips, survives a truncated final
               line, and refuses to invent levels it did not see.

    python tests_book_backtest.py
"""
from __future__ import annotations

import io
import json
import pathlib
import sys
import tempfile
from decimal import Decimal

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

import book_backtest as bb                    # noqa: E402
import book_recorder as br                    # noqa: E402
import orderbook                              # noqa: E402

P = F = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global P, F
    if cond:
        P += 1
        print(f"  pass  {name}")
    else:
        F += 1
        print(f"  FAIL  {name} {detail}")


WINDOW = 1_699_999_800                        # a real 300s boundary


def book(levels, side="asks", other=None):
    """A recorded leg. `levels` is [(price, size), ...] best first."""
    out = {side: [[str(p), str(s)] for p, s in levels]}
    out["bids" if side == "asks" else "asks"] = [
        [str(p), str(s)] for p, s in (other or ())]
    return out


def snap(secs_left, up_asks, dn_asks=((0.60, 500),), *, window=WINDOW,
         cl_strike=100.0, cl_now=101.0, wall=None):
    return {
        "schema": 1,
        "wall": window + (300 - secs_left) if wall is None else wall,
        "window": window, "secs_left": secs_left,
        "cl_strike": cl_strike, "cl_now": cl_now,
        "bn_strike": cl_strike, "bn_now": cl_now,
        "up": book(up_asks, other=[(0.30, 100)]),
        "down": book(dn_asks, other=[(0.30, 100)]),
    }


# ------------------------------------------------------------------- 1 FILL ---
def t_fill_walks_past_the_best_ask():
    """A thin top level does not fill the whole order at its price."""
    asks = bb.ladder(book([(0.40, 2), (0.50, 100)]), "asks")
    fill = bb.walk(asks, Decimal("2.50"), Decimal("0.90"), Decimal("5"), 0.07)
    # 2 shares at 0.40 = $0.80, the remaining $1.70 at 0.50 = 3.4 shares.
    check("walk consumes both levels", fill["levels"] == 2)
    check("shares are 5.4 not 6.25",
          abs(float(fill["shares"]) - 5.4) < 1e-9, f"got {fill['shares']}")
    check("average is worse than the best ask",
          float(fill["avg"]) > 0.40, f"got {fill['avg']}")
    quote = bb.quote_fill(asks, Decimal("2.50"), Decimal("0.90"), 0.07)
    check("the price line claims 6.25 shares at 0.40",
          abs(float(quote["shares"]) - 6.25) < 1e-9 and quote["avg"] == Decimal("0.40"))
    check("the price line overstates size by 0.85 shares",
          abs(float(quote["shares"] - fill["shares"]) - 0.85) < 1e-9)


def t_thin_ladder_cannot_fill():
    """Depth under the venue minimum is a refusal, not a small fill."""
    asks = bb.ladder(book([(0.40, 1.0)]), "asks")
    try:
        bb.walk(asks, Decimal("2.50"), Decimal("0.90"), Decimal("5"), 0.07)
        check("thin ladder is refused", False, "it filled")
    except bb.NoFill as exc:
        check("thin ladder is refused", True)
        check("the refusal says why", "available" in str(exc) or "minimum" in str(exc),
              str(exc))
    # The price line does not even look: it fills 6.25 shares off 1.0 share.
    quote = bb.quote_fill(asks, Decimal("2.50"), Decimal("0.90"), 0.07)
    check("the price line fills anyway", float(quote["shares"]) > 6)


def t_cap_stops_the_walk():
    """Levels above the price cap are not buyable and do not count."""
    asks = bb.ladder(book([(0.40, 2), (0.95, 1000)]), "asks")
    try:
        bb.walk(asks, Decimal("2.50"), Decimal("0.90"), Decimal("5"), 0.07)
        check("walk stops at the cap", False, "it bought above the cap")
    except bb.NoFill as exc:
        check("walk stops at the cap", "0.80" in str(exc) or "available" in str(exc),
              str(exc))
    deep = bb.ladder(book([(0.95, 1000)]), "asks")
    try:
        bb.walk(deep, Decimal("2.50"), Decimal("0.90"), Decimal("5"), 0.07)
        check("a book entirely above the cap is refused", False)
    except bb.NoFill as exc:
        check("a book entirely above the cap is refused", "cap" in str(exc), str(exc))


def t_empty_and_dirty_levels():
    check("no asks is a refusal",
          _raises(lambda: bb.walk([], Decimal("2.50"), Decimal("0.9"),
                                  Decimal("5"), 0.07)))
    dirty = book([(0.40, 2)])
    dirty["asks"] += [["nonsense", "4"], ["0.45", "-3"], ["0.50", "100"]]
    asks = bb.ladder(dirty, "asks")
    check("unparseable levels are dropped, not treated as the end of book",
          [str(p) for p, _s in asks] == ["0.4", "0.50"], str(asks))


def _raises(fn) -> bool:
    try:
        fn()
    except bb.NoFill:
        return True
    return False


# ----------------------------------------------------------------- 2 PARITY ---
def t_minimum_stake_matches_the_live_path():
    """The backtest raises a stake exactly the way the order paths do."""
    asks = bb.ladder(book([(0.51, 3), (0.60, 100)]), "asks")
    live = orderbook.venue_minimum_stake(
        Decimal("2.50"), asks, Decimal("5"), Decimal("0.90"))
    fill = bb.walk(asks, Decimal("2.50"), Decimal("0.90"), Decimal("5"), 0.07)
    check("stake is raised to the venue minimum", live > Decimal("2.50"))
    check("the backtest uses that same raised stake", fill["stake"] == live,
          f"{fill['stake']} vs {live}")
    check("and the fill really clears the minimum",
          fill["shares"] >= Decimal("5"))


def t_fee_is_charged_per_parcel_at_its_own_price():
    from accounting import fees
    asks = bb.ladder(book([(0.40, 2), (0.50, 100)]), "asks")
    fill = bb.walk(asks, Decimal("2.50"), Decimal("0.90"), Decimal("5"), 0.07)
    expect = (fees.taker_fee(2.0, 0.40, th=0.07)
              + fees.taker_fee(3.4, 0.50, th=0.07))
    check("fee is the sum of each parcel's own fee",
          abs(float(fill["fee"]) - expect) < 0.011,
          f"{fill['fee']} vs {expect:.4f}")
    flat = fees.taker_fee(float(fill["shares"]), 0.40, th=0.07)
    check("which is not the fee at the best ask alone",
          abs(float(fill["fee"]) - flat) > 1e-6)


# ----------------------------------------------------------------- 3 SETTLE ---
def t_settlement_pays_a_dollar_a_winning_share():
    fill = {"shares": Decimal("5.4"), "stake": Decimal("2.50"),
            "fee": Decimal("0.10")}
    check("a winner pays shares - stake - fee",
          bb._pnl(fill, True) == Decimal("2.80"), str(bb._pnl(fill, True)))
    check("a loser loses stake and fee",
          bb._pnl(fill, False) == Decimal("-2.60"), str(bb._pnl(fill, False)))
    check("no fill is no PnL", bb._pnl(None, True) == Decimal("0"))


def t_unresolved_rounds_are_skipped_not_guessed():
    snaps = [snap(250, [(0.40, 500)])]
    out = bb.replay(snaps, {}, bb.parse_bands("300:240:0.30:0.50"),
                    stake=2.50, signal="chainlink", theta=0.07,
                    min_shares=5.0, cap=0.90)
    check("a round with no recorded winner is not traded", not out["trades"])
    check("and is counted as unresolved", out["skipped"]["unresolved"] == 1)
    out = bb.replay(snaps, {str(WINDOW): "SPLIT"},
                    bb.parse_bands("300:240:0.30:0.50"), stake=2.50,
                    signal="chainlink", theta=0.07, min_shares=5.0, cap=0.90)
    check("a split round is named, not scored",
          not out["trades"] and out["skipped"]["split"] == 1)


# --------------------------------------------------------------- 4 CONTRAST ---
def t_replay_runs_both_engines_on_the_same_signal():
    """One thin round: the quote engine books a trade the book engine cannot."""
    snaps = [snap(250, [(0.40, 1.0)])]        # 1 share is under the minimum
    bands = bb.parse_bands("300:240:0.30:0.50")
    out = bb.replay(snaps, {str(WINDOW): "UP"}, bands, stake=2.50,
                    signal="chainlink", theta=0.07, min_shares=5.0, cap=0.90)
    check("the entry is signalled", len(out["trades"]) == 1)
    trade = out["trades"][0]
    check("chainlink above strike buys UP", trade["side"] == "UP")
    check("the book engine did not fill", trade["book"] is None)
    check("the quote engine did", trade["quote"] is not None)
    check("the miss is reported", len(out["misses"]) == 1)
    check("phantom profit is booked by the quote engine only",
          trade["quote_pnl"] > 0 and trade["book_pnl"] == Decimal("0"),
          f"{trade['quote_pnl']} / {trade['book_pnl']}")


def t_one_entry_per_band_per_round():
    snaps = [snap(250, [(0.40, 500)]), snap(245, [(0.40, 500)]),
             snap(200, [(0.40, 500)])]
    bands = bb.parse_bands("300:240:0.30:0.50,240:180:0.30:0.50")
    out = bb.replay(snaps, {str(WINDOW): "UP"}, bands, stake=2.50,
                    signal="chainlink", theta=0.07, min_shares=5.0, cap=0.90)
    check("two bands, two entries, not three", len(out["trades"]) == 2,
          str(len(out["trades"])))
    check("the first matching tick in a band wins",
          out["trades"][0]["secs_left"] == 250)


def t_a_price_outside_the_band_is_not_traded():
    snaps = [snap(250, [(0.80, 500)])]
    out = bb.replay(snaps, {str(WINDOW): "UP"},
                    bb.parse_bands("300:240:0.30:0.50"), stake=2.50,
                    signal="chainlink", theta=0.07, min_shares=5.0, cap=0.90)
    check("a best ask above the band is skipped", not out["trades"])


def t_signals_read_the_recorded_fields():
    s = snap(250, [(0.40, 500)], cl_strike=100.0, cl_now=99.0)
    check("chainlink below strike buys DOWN", bb.side_for(s, "chainlink") == "DOWN")
    s["cl_now"] = None
    check("a missing reading abstains", bb.side_for(s, "chainlink") is None)
    check("binance is read separately", bb.side_for(s, "binance") == "DOWN")
    deep = snap(250, [(0.40, 500)])
    deep["up"] = book([(0.40, 10)], other=[(0.30, 900)])
    check("the book signal votes with resting depth",
          bb.side_for(deep, "book") == "UP")
    one_sided = snap(250, [(0.40, 500)])
    one_sided["up"] = book([(0.40, 10)], other=[])
    check("a one-sided book abstains", bb.side_for(one_sided, "book") is None)


def t_bands_are_validated():
    check("a reversed band is refused",
          _exits(lambda: bb.parse_bands("240:300:0.3:0.5")))
    check("an out-of-range price is refused",
          _exits(lambda: bb.parse_bands("300:240:0.3:1.5")))
    check("lo above hi is refused",
          _exits(lambda: bb.parse_bands("300:240:0.6:0.5")))
    check("a live PHASE1_BANDS string with an interval still parses",
          bb.parse_bands("120:60:0.55:0.75:8")[0][:4] == (120, 60, 0.55, 0.75))


def _exits(fn) -> bool:
    try:
        fn()
    except SystemExit:
        return True
    return False


def t_report_names_the_gap():
    snaps = [snap(250, [(0.40, 1.0)])]
    out = bb.replay(snaps, {str(WINDOW): "UP"},
                    bb.parse_bands("300:240:0.30:0.50"), stake=2.50,
                    signal="chainlink", theta=0.07, min_shares=5.0, cap=0.90)
    buf = io.StringIO()
    stdout, sys.stdout = sys.stdout, buf
    try:
        bb.report(out, per_round=True)
    finally:
        sys.stdout = stdout
    text = buf.getvalue()
    check("the report names the unfillable entries", "COULD NOT FILL" in text)
    check("the report prices the phantom PnL", "Phantom PnL" in text)
    check("the report gives a verdict", "VERDICT" in text)
    check("the per-round table marks the no-fill", "NOFILL" in text)


# ------------------------------------------------------------------- 5 TAPE ---
def t_tape_round_trips_and_survives_a_kill():
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "tape.jsonl"
        rows = [snap(250, [(0.40, 500)]), snap(120, [(0.60, 500)])]
        with path.open("w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
            fh.write('{"window": 1699999800, "up": {"asks": [["0.4"')
        back = br.snapshots(path)
        check("the truncated final line is dropped", len(back) == 2,
              str(len(back)))
        check("levels survive the round trip as exact strings",
              back[0]["up"]["asks"][0] == ["0.4", "500"],
              str(back[0]["up"]["asks"][0]))
        check("snapshots come back oldest first",
              back[0]["secs_left"] > back[1]["secs_left"])
        check("a missing tape is named, not silently empty",
              _exits(lambda: br.snapshots(pathlib.Path(d) / "nope.jsonl")))


def t_depth_is_recorded_in_full():
    """_depth keeps every level both sides - that is the whole point."""
    calls = {}

    def fake_get(token):
        calls["token"] = token
        bids = [{"price": "0.39", "size": "10"}, {"price": "0.38", "size": "20"}]
        asks = [{"price": "0.40", "size": "2"}, {"price": "0.50", "size": "99"}]
        return bids, asks

    real_get = orderbook.get_orderbook
    real_report = orderbook.LAST_TIMESTAMP_REPORT
    orderbook.get_orderbook = fake_get
    orderbook.LAST_TIMESTAMP_REPORT = {"exchange_ts_s": 123.0, "quiet_s": 1.0,
                                       "held_s": 0.2}
    try:
        out = br._depth("12345")
    finally:
        orderbook.get_orderbook = real_get
        orderbook.LAST_TIMESTAMP_REPORT = real_report
    check("both ask levels are kept", out["asks"] == [["0.40", "2"], ["0.50", "99"]])
    check("both bid levels are kept", out["bids"] == [["0.39", "10"], ["0.38", "20"]])
    check("the venue timestamp is kept", out["book_ts"] == 123.0)
    check("the token is recorded", out["token"] == "12345")


def t_an_unreadable_book_is_not_recorded():
    def boom(_token):
        raise ValueError("CLOB book response is stale in hand")

    real = orderbook.get_orderbook
    orderbook.get_orderbook = boom
    try:
        br._depth("12345")
        check("a refused book propagates instead of being written", False)
    except ValueError:
        check("a refused book propagates instead of being written", True)
    finally:
        orderbook.get_orderbook = real


def main() -> int:
    print("gate 1: real-book backtest suite\n")
    for name, fn in sorted(globals().items()):
        if not name.startswith("t_"):
            continue
        print(f"{name}")
        try:
            fn()
        except Exception as exc:                # noqa: BLE001
            global F
            F += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{P} passed, {F} failed")
    return 1 if F else 0


if __name__ == "__main__":
    raise SystemExit(main())
