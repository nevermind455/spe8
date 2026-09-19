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



# --------------------------------------------------- 6 TUNER (band_tuner.py) ---
def _tape(rounds, *, edge=0.5, seed=0, price=None, top=60.0, thin=False):
    """Synthetic rounds. `edge` is how often the signal names the winner.

    Prices vary round to round unless `price` pins them, so a sweep over many
    cells really has many cells with fills in them - a tape where only one
    cell can ever fill is not a search, and would understate the noise floor.
    Both legs' asks sum to 1.01, as the real venue's do.
    """
    import random
    rng = random.Random(seed)
    by_window, wins = {}, {}
    for i in range(rounds):
        w = WINDOW + i * 300
        drift = rng.gauss(0, 0.5)
        truth = "UP" if drift >= 0 else "DOWN"
        wins[str(w)] = truth if rng.random() < edge else (
            "DOWN" if truth == "UP" else "UP")
        up = price if price is not None else round(rng.uniform(0.28, 0.70), 2)
        dn = round(1.01 - up, 2)

        def lad(first):
            # `thin` leaves one level under the venue minimum and nothing
            # behind it, which is a refusal rather than a small fill.
            if thin:
                return [(first, top)]
            return [(first, top), (round(first + 0.05, 2), 300)]

        snaps = []
        for left in (250, 200, 150):
            s = snap(left, lad(up), window=w, cl_strike=100.0,
                     cl_now=100.0 + drift)
            s["down"] = book(lad(dn), other=[(0.20, 100)])
            snaps.append(s)
        by_window[w] = snaps
    return by_window, wins


def t_tuner_takes_one_entry_per_round():
    import band_tuner as bt
    by_window, wins = _tape(6, seed=1)
    fills, misses = bt.entries_for((300, 60, 0.30, 0.50), by_window,
                                   stake=2.50, signal="chainlink", theta=0.07,
                                   min_shares=5.0, cap=0.90)
    check("one fill per round, not one per snapshot",
          len(fills) <= 6 and len({f["window"] for f in fills}) == len(fills),
          f"{len(fills)} fills over 6 rounds")
    check("no phantom misses when depth is deep", misses == 0)


def t_tuner_counts_a_thin_round_as_a_miss_not_a_win():
    import band_tuner as bt
    by_window, wins = _tape(4, seed=2, price=0.40, top=1.0, thin=True)
    fills, misses = bt.entries_for((300, 60, 0.30, 0.50), by_window,
                                   stake=2.50, signal="chainlink", theta=0.07,
                                   min_shares=5.0, cap=0.90)
    # Only rounds whose signal picked the leg priced inside the band are
    # entries at all; the rest never wanted the trade and are not misses.
    wanted = sum(1 for snaps in by_window.values()
                 if bb.side_for(snaps[0], "chainlink") == "UP")
    check("a round that could not fill produces no fill", not fills)
    check("every round the band wanted is counted as a miss",
          misses == wanted and misses > 0, f"{misses} misses vs {wanted} wanted")


def t_tuner_clusters_by_round():
    """Two fills in one round are one observation, because they settle once."""
    import band_tuner as bt
    fills = [{"window": WINDOW, "side": "UP", "stake": 2.5, "shares": 6.0,
              "fee": 0.1, "price": 0.4},
             {"window": WINDOW, "side": "UP", "stake": 2.5, "shares": 6.0,
              "fee": 0.1, "price": 0.4}]
    s = bt.score(fills, {str(WINDOW): "UP"})
    check("two fills in one round collapse to one round", s["rounds"] == 1)
    check("but both their PnL is counted",
          abs(s["net"] - 2 * (6.0 - 2.5 - 0.1)) < 1e-9, str(s["net"]))


def t_tuner_searching_more_cells_raises_the_bar():
    """The property the floor exists to price: search inflates the best |t|.

    Asserted as a comparison rather than against a fixed number, because the
    claim is not "the bar is 2.4", it is "the bar goes up when you look in
    more places" - which is what makes a single cell's t-stat misleading.
    """
    import band_tuner as bt
    by_window, wins = _tape(50, edge=0.5, seed=5)
    kw = dict(stake=2.50, signal="chainlink", theta=0.07, min_shares=5.0,
              cap=0.90)
    wide = bt.build_cells(by_window, list(bt.sweep_bands(
        [(300, 180), (180, 60)], bt.DEFAULT_GRID)), **kw)
    one = bt.build_cells(by_window, [("single", (300, 60, 0.30, 0.40))], **kw)
    floor_wide = bt.noise_floor(wide, wins, nulls=120, seed=0)
    floor_one = bt.noise_floor(one, wins, nulls=120, seed=0)
    check("a floor is produced for both", bool(floor_wide) and bool(floor_one))
    check("searching many cells raises the bar above searching one",
          floor_wide["median"] > floor_one["median"],
          f"{len(wide)} cells -> {floor_wide['median']:.2f}, "
          f"1 cell -> {floor_one['median']:.2f}")
    check("the 95th percentile is above the median",
          floor_wide["p95"] >= floor_wide["median"])


def t_tuner_p_value_brackets():
    import band_tuner as bt
    floor = {"draws": 100, "values": sorted(float(i) / 10 for i in range(100))}
    check("a t nothing beats is near p = 0",
          bt.beats_floor(99.0, floor) < 0.02)
    check("a t everything beats is near p = 1",
          bt.beats_floor(0.0, floor) > 0.98)
    check("no floor means no p-value", bt.beats_floor(3.0, {}) is None)
    check("a non-finite t has no p-value",
          bt.beats_floor(float("nan"), floor) is None)


def t_tuner_rejects_noise_and_detects_signal():
    """The property the whole tool exists for, at small scale."""
    import band_tuner as bt
    kw = dict(stake=2.50, signal="chainlink", theta=0.07, min_shares=5.0,
              cap=0.90)
    grid = list(bt.sweep_bands([(300, 180), (180, 60)], bt.DEFAULT_GRID))

    noise_by_w, noise_wins = _tape(60, edge=0.5, seed=7)
    cells = bt.build_cells(noise_by_w, grid, **kw)
    floor = bt.noise_floor(cells, noise_wins, nulls=60, seed=0)
    best = max((bt.score(f, noise_wins)["t"] for _n, _b, f, _m in cells if f),
               key=lambda t: abs(t) if t == t else 0)
    p_noise = bt.beats_floor(best, floor)
    check("a tape with no edge does not clear its own noise floor",
          p_noise is None or p_noise > 0.05, f"p = {p_noise}")

    edge_by_w, edge_wins = _tape(60, edge=0.85, seed=7)
    cells = bt.build_cells(edge_by_w, grid, **kw)
    floor = bt.noise_floor(cells, edge_wins, nulls=60, seed=0)
    best = max((bt.score(f, edge_wins)["t"] for _n, _b, f, _m in cells if f),
               key=lambda t: abs(t) if t == t else 0)
    p_edge = bt.beats_floor(best, floor)
    check("a tape with a strong edge does clear it",
          p_edge is not None and p_edge <= 0.05, f"p = {p_edge}")


def t_tuner_report_refuses_to_rank_an_empty_tape():
    import band_tuner as bt
    by_window, wins = _tape(3, seed=9, price=0.85)   # every price outside band
    cells = bt.build_cells(by_window, [("x", (300, 60, 0.30, 0.40))],
                           stake=2.50, signal="chainlink", theta=0.07,
                           min_shares=5.0, cap=0.90)
    buf = io.StringIO()
    stdout, sys.stdout = sys.stdout, buf
    try:
        bt.report(cells, wins, {}, title="EMPTY")
    finally:
        sys.stdout = stdout
    check("an unmeasurable sweep says so instead of ranking",
          "nothing is measurable" in buf.getvalue())
    check("and it says the search was unadjusted",
          "unadjusted for the search" in buf.getvalue())

if __name__ == "__main__":
    raise SystemExit(main())
