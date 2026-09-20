#!/usr/bin/env python3
"""Deterministic safety, fill, persistence, fee, and settlement tests.

    python tests_paper.py

No network is used.  All public books and market rules are fixed fixtures.
"""
from __future__ import annotations

import ast
import asyncio
import json
import pathlib
import shutil
import sys
import tempfile
import time
import types
from decimal import Decimal

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

from accounting.ledger import Ledger  # noqa: E402
from accounting.resolution import PENDING, RESOLVED, Resolution  # noqa: E402
from accounting.settlement import SettlementWorker  # noqa: E402
from paper_trade import (BookSnapshot, MarketRules, PaperBroker, PaperRejected,  # noqa: E402
                         curve_fee, estimate_fok, fetch_executable_book,
                         install_paper_execution, parse_book, parse_market_rules,
                         size_to_venue_minimum, snapshot_from_book_view)


PASS = FAIL = 0
FAILURES: list[str] = []
D = Decimal


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{name}: {detail}")
        print(f"  FAIL {name} {detail}")


def approx(a, b, tolerance=1e-8):
    return a is not None and abs(float(a) - float(b)) <= tolerance


def book(token="up", *, minimum=None):
    return BookSnapshot(
        token,
        ((D("0.40"), D("3")), (D("0.50"), D("10")),
         (D("0.995"), D("100"))),
        min_order_size=D(str(minimum)) if minimum is not None else None,
        tick_size=D("0.01"), timestamp=str(int(time.time() * 1000)),
        book_hash="abc", received_wall=time.time(), best_bid=D("0.39"))


def rules(*, minimum=None, rate="0.07", exponent=1):
    return MarketRules(
        "cond", D(rate), exponent,
        min_order_size=D(str(minimum)) if minimum is not None else None,
        tick_size=D("0.01"), source="venue",
        up_token_id="up", down_token_id="down")


def round_end():
    return (int(time.time()) // 300 + 1) * 300


def paper_order(broker, side="UP", amount=2, up="up", down="down",
                condition="cond", end=None, pre_submit_guard=None):
    return broker.place_trade(side, amount, up, down, condition,
                              round_end() if end is None else end,
                              pre_submit_guard=pre_submit_guard)


def expect_reject(name, fn, contains):
    try:
        fn()
    except PaperRejected as exc:
        check(name, contains.lower() in str(exc).lower(), str(exc))
    except Exception as exc:
        check(name, False, f"wrong exception {type(exc).__name__}: {exc}")
    else:
        check(name, False, "did not reject")


def t_book_parser_is_strict_and_sorted():
    parsed = parse_book({
        "asset_id": "up", "timestamp": int(time.time() * 1000),
        "min_order_size": "5", "bids": [{"price": "0.39", "size": "4"}],
        "asks": [
            {"price": "0.5", "size": "2"},
            {"price": "0.4", "size": "3"},
            {"price": "0.5", "size": "4"},
        ],
    }, "up")
    check("asks sorted best first", parsed.asks[0] == (D("0.4"), D("3")),
          str(parsed.asks))
    check("duplicate ask prices aggregated",
          parsed.asks[1] == (D("0.5"), D("6")), str(parsed.asks))
    check("minimum size parsed", parsed.min_order_size == D("5"))
    expect_reject("wrong book token rejected",
                  lambda: parse_book({"asset_id": "down", "asks": []}, "up"),
                  "does not match")
    expect_reject(
        "one malformed level rejects the whole snapshot",
        lambda: parse_book({"asset_id": "up", "asks": [
            {"price": "0.4", "size": "3"}, {"price": "bad", "size": "10"}
        ]}, "up"), "malformed ask")
    expect_reject(
        "zero-size level rejects the whole snapshot",
        lambda: parse_book({"asset_id": "up", "asks": [
            {"price": "0.3", "size": "0"}
        ]}, "up"), "out-of-range ask")


def t_fok_walks_depth_with_level_fees():
    quote = estimate_fok(book(), 2, 0.99, rules())
    check("FOK spends full requested amount", quote.notional == D("2.000000"),
          str(quote.notional))
    check("multi-level shares correct", approx(quote.shares, 4.6), str(quote.shares))
    check("VWAP correct", approx(quote.average_price, 2 / 4.6),
          str(quote.average_price))
    check("worst consumed ask correct", quote.worst_price == D("0.50"))
    # 3sh @ .40 = .05040 and 1.6sh @ .50 = .02800
    check("fees summed at each price level", quote.fee == D("0.07840"),
          str(quote.fee))
    check("total cost includes taker fee", quote.total_cost == D("2.078400"),
          str(quote.total_cost))


def t_fok_rejects_partial_cap_empty_and_minimum():
    expect_reject("insufficient depth is no fill",
                  lambda: estimate_fok(book(), 9, 0.50, rules()), "FOK no-fill")
    expect_reject("price cap is enforced",
                  lambda: estimate_fok(book(), 2, 0.45, rules()), "FOK no-fill")
    expect_reject("empty book is rejected",
                  lambda: estimate_fok(BookSnapshot("up", ()), 2, .99, rules()),
                  "no asks")
    expect_reject("venue minimum shares enforced",
                  lambda: estimate_fok(book(minimum=5), 2, .99, rules()),
                  "below venue minimum")
    only_fractional_cap = BookSnapshot(
        "up", ((D("0.995"), D("100")),), tick_size=D("0.01"),
        timestamp=str(int(time.time() * 1000)), best_bid=D("0.99"))
    expect_reject(
        "paper cap is floored to the venue tick exactly like live",
        lambda: estimate_fok(only_fractional_cap, 2, .995, rules()),
        "FOK no-fill")
    expect_reject(
        "book/rules tick mismatch is rejected",
        lambda: estimate_fok(
            BookSnapshot("up", ((D("0.4"), D("10")),),
                         tick_size=D("0.001")), 2, .99, rules()),
        "disagree on tick")


def t_fok_rejects_asks_outside_min_max_band():
    cheap = BookSnapshot(
        "up", ((D("0.08"), D("100")),), tick_size=D("0.01"),
        timestamp=str(int(time.time() * 1000)), best_bid=D("0.07"))
    expect_reject(
        "best ask below 0.20 is refused",
        lambda: estimate_fok(cheap, 5, 0.90, rules(), min_price=0.20),
        "below MIN_BUY_PRICE")
    rich = BookSnapshot(
        "up", ((D("0.95"), D("100")),), tick_size=D("0.01"),
        timestamp=str(int(time.time() * 1000)), best_bid=D("0.94"))
    expect_reject(
        "best ask above 0.90 is refused",
        lambda: estimate_fok(rich, 5, 0.90, rules(), min_price=0.20),
        "FOK no-fill")
    mid = BookSnapshot(
        "up", ((D("0.40"), D("100")),), tick_size=D("0.01"),
        timestamp=str(int(time.time() * 1000)), best_bid=D("0.39"))
    quote = estimate_fok(mid, 5, 0.90, rules(), min_price=0.20)
    check("ask inside 0.20-0.90 fills", quote.worst_price == D("0.40"), str(quote))


def t_paper_sizes_up_to_venue_minimum():
    snap = BookSnapshot(
        "up", ((D("0.99"), D("100")),), min_order_size=D("5"),
        tick_size=D("0.01"), timestamp=str(int(time.time() * 1000)),
        book_hash="min", received_wall=time.time(), best_bid=D("0.98"))
    sized = size_to_venue_minimum(2, snap, rules(minimum=5), 0.99)
    check("two dollars is raised to five shares at 0.99",
          sized == D("4.95"), str(sized))
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=snap, selected_rules=rules(minimum=5))
        check("sized paper FOK fills", paper_order(broker, amount=2) is True)
        check("fill notional meets the minimum",
              approx(broker.last_fill["requested_amount"], 4.95),
              str(broker.last_fill))


def t_paper_sizes_up_when_top_of_book_cannot_fill_the_minimum():
    """$2.50 at a 0.50 best ask is 5 shares only if that level has the size.

    A 3-share top and the rest at 0.51 is the live btc-updown-5m case that
    produced `order size 4.959885 shares is below venue minimum 5`.
    """
    thin = BookSnapshot(
        "up", ((D("0.50"), D("3")), (D("0.51"), D("10"))),
        min_order_size=D("5"), tick_size=D("0.01"),
        timestamp=str(int(time.time() * 1000)),
        book_hash="thin", received_wall=time.time(), best_bid=D("0.49"))
    sized = size_to_venue_minimum(2.50, thin, rules(minimum=5), 0.90)
    check("thin top of book sizes up past best-ask notional",
          sized == D("2.52"), str(sized))
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=thin, selected_rules=rules(minimum=5))
        check("thin-book FOK fills after sizing up",
              paper_order(broker, amount=2.50) is True, broker.last_error)
        check("filled shares meet the venue minimum",
              broker.last_fill["shares"] >= 5,
              str(broker.last_fill))


def t_paper_uses_live_ws_asks_when_rest_is_empty():
    view = types.SimpleNamespace(
        token="up", status="LIVE", asks=((0.76, 50.0),),
        best_bid=0.68, exchange_ts_ms=int(time.time() * 1000),
        tick_size=0.01, hash="ws")
    snap = snapshot_from_book_view(view, expected_token="up")
    check("websocket view carries asks", snap.asks[0][0] == D("0.76"), str(snap.asks))
    preferred = fetch_executable_book(
        "up", host="https://public.invalid", ws_view=view)
    check("LIVE websocket book is preferred over REST",
          preferred.asks[0][0] == D("0.76"), str(preferred.asks))
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=snap, selected_rules=rules(minimum=5))
        check("paper FOK fills from the websocket-shaped book",
              paper_order(broker, amount=5) is True)


def t_paper_ws_book_without_exchange_ts_falls_back_to_rest():
    """Receipt time is not exchange time.

    Stamping `now` on a socket book that carried no exchange timestamp makes a
    silently gapped feed look perfectly fresh, which defeats the staleness
    check the whole FOK path depends on. The snapshot refuses it, and the
    executable path falls through to the REST book, which carries the venue's
    own timestamp.
    """
    import paper_trade

    view = types.SimpleNamespace(
        token="up", status="LIVE", asks=((0.55, 80.0),),
        best_bid=0.54, exchange_ts_ms=None,
        tick_size=0.01, hash="ws-no-ts")
    expect_reject(
        "a socket book with no exchange timestamp is refused",
        lambda: snapshot_from_book_view(view, expected_token="up"),
        "exchange timestamp")

    rest = BookSnapshot(token_id="up", asks=((D("0.60"), D("40")),),
                        best_bid=D("0.59"), tick_size=D("0.01"),
                        timestamp=str(int(time.time() * 1000)),
                        book_hash="rest", received_wall=time.time())
    original = paper_trade.fetch_public_book
    paper_trade.fetch_public_book = lambda token, *, host, timeout=8.0: rest
    try:
        preferred = fetch_executable_book(
            "up", host="https://public.invalid", ws_view=view)
    finally:
        paper_trade.fetch_public_book = original
    check("the untimestamped socket book is not used for the fill",
          preferred.asks[0][0] == D("0.60"), str(preferred.asks))
    check("the REST book's own venue timestamp is kept",
          preferred.timestamp == rest.timestamp, str(preferred.timestamp))


def t_paper_quiet_book_is_not_mistaken_for_a_stale_one():
    """Paper FOK must use the same held/quiet split as the live book parser.

    The previous check treated exchange-timestamp age as copy freshness, so a
    book the venue had not changed for more than 8s was refused even though
    REST had just returned it. On btc-updown-5m, 33s+ gaps between changes
    are ordinary.
    """
    from dataclasses import replace

    import timer

    feeds = pathlib.Path(__file__).with_name("run_feeds.py").read_text(encoding="utf-8")
    check("runner wires the quiet bound into paper",
          "max_quiet_s=config.ORDERBOOK_MAX_QUIET_SECONDS" in feeds)
    check("runner wires the future-dated bound into paper",
          "future_tol_s=config.ORDERBOOK_FUTURE_TOLERANCE_SECONDS" in feeds)

    now = timer.wall()

    def snap(*, quiet_s=1.0, held_s=0.05):
        return replace(
            book(),
            timestamp=str(int((now - quiet_s) * 1000)),
            received_wall=now - held_s,
        )

    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=snap(quiet_s=95.0))
        check("a book unchanged for 95s still fills",
              paper_order(broker) is True, broker.last_error)

    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=snap(held_s=40.0))
        check("a copy held 40s is refused",
              paper_order(broker) is False, broker.last_error)
        check("held refusal names the copy, not quiet time",
              "stale in hand" in (broker.last_error or ""), broker.last_error)

    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=snap(quiet_s=1200.0))
        check("a book unchanged past the frozen-venue bound is refused",
              paper_order(broker) is False, broker.last_error)
        check("the frozen-venue refusal names the cause",
              "not changed" in (broker.last_error or ""), broker.last_error)

    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=snap(quiet_s=-30.0))
        check("a book dated well ahead of us is refused",
              paper_order(broker) is False, broker.last_error)
        check("the future refusal names the clock",
              "future-dated" in (broker.last_error or ""), broker.last_error)

    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=snap(quiet_s=-2.0))
        check("a book inside the future tolerance still fills",
              paper_order(broker) is True, broker.last_error)


def t_paper_ws_held_age_uses_socket_receipt_not_conversion_time():
    """Stamping ``now`` at conversion would hide an already-old LIVE book."""
    import timer

    now_ms = int(timer.wall() * 1000)
    view = types.SimpleNamespace(
        token="up", status="LIVE", asks=((0.55, 80.0),),
        best_bid=0.54, exchange_ts_ms=now_ms - 95_000,
        tick_size=0.01, hash="ws-quiet",
        updated_mono=time.monotonic() - 40.0,
    )
    snap = snapshot_from_book_view(view, expected_token="up")
    held = timer.wall() - snap.received_wall
    check("socket receipt age is preserved on the snapshot",
          39.0 <= held <= 41.0, str(held))
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=snap)
        check("an old socket copy is refused as stale in hand",
              paper_order(broker) is False, broker.last_error)
        check("the refusal is held-age, not quiet-age",
              "stale in hand" in (broker.last_error or ""), broker.last_error)


def t_dynamic_v2_fee_curve_and_missing_fee_rejection():
    metadata = {
        "condition_id": "cond", "seconds_delay": 0,
        "tokens": [{"outcome": "Up", "token_id": "up"},
                   {"outcome": "Down", "token_id": "down"}],
    }
    parsed = parse_market_rules(
        {"mos": 5, "mts": .01, "fd": {"r": .25, "e": 2, "to": True},
         "t": [{"o": "Up", "t": "up"}, {"o": "Down", "t": "down"}],
         "itode": True},
        "cond", market_data=metadata)
    check("V2 fee rate parsed", parsed.fee_rate == D("0.25"), str(parsed))
    check("V2 fee exponent parsed", parsed.fee_exponent == 2, str(parsed))
    check("V2 exponent applied",
          curve_fee(D("100"), D("0.5"), parsed) == D("1.56250"),
          str(curve_fee(D("100"), D("0.5"), parsed)))
    expect_reject(
        "missing venue fee cannot produce exact paper PnL",
        lambda: parse_market_rules(
            {"mos": 5,
             "t": [{"o": "Up", "t": "up"}, {"o": "Down", "t": "down"}]},
            "cond", category="crypto", market_data=metadata),
        "fee parameters")
    # The venue marks whether the published curve is actually charged to the
    # taker. Without that flag the rate is not known to apply, and a paper
    # fill that guesses either way misstates PnL.
    expect_reject(
        "fee data without the venue taker flag is not simulated",
        lambda: parse_market_rules(
            {"mos": 5, "mts": .01, "fd": {"r": .07, "e": 1},
             "t": [{"o": "Up", "t": "up"}, {"o": "Down", "t": "down"}]},
            "cond", category="crypto", market_data=metadata),
        "fee parameters")
    check("actual public seconds_delay is used, not itode guesswork",
          parsed.taker_delay_ms == 0.0, str(parsed.taker_delay_ms))

    unknown_delay = parse_market_rules(
        {"mos": 5, "mts": .01, "fd": {"r": .07, "e": 1, "to": True}, "itode": True,
         "t": [{"o": "Up", "t": "up"}, {"o": "Down", "t": "down"}]},
        "cond")
    check("itode alone never invents a delay duration",
          unknown_delay.taker_delay_ms is None, str(unknown_delay.taker_delay_ms))


def _broker(tmp, *, balance=100, selected_book=None, selected_rules=None,
            context=None):
    tmp = pathlib.Path(tmp)
    ledger = Ledger(path=str(tmp / "paper_ledger.json"))
    ctx = context or {"condition_id": "cond", "up_token_id": "up",
                      "down_token_id": "down"}
    broker = PaperBroker(
        ledger, market_context=lambda: dict(ctx), host="https://public.invalid",
        max_buy_price=.99, start_balance=balance,
        account_path=tmp / "paper_account.json",
        audit_path=tmp / "paper_orders.jsonl",
        book_fetch=lambda token: selected_book or book(token),
        rules_fetch=lambda cid: selected_rules or rules(),
        min_seconds_to_expiry=0, trade_window_seconds=300,
    )
    return broker


def t_broker_records_only_realistic_fills_and_cash():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp)
        check("paper FOK reports filled", paper_order(broker))
        summary = broker.summary(mark=lambda _token: .39)
        check("one fill persisted", summary["fills_counted"] == 1, str(summary))
        check("paper cash deducts notional plus fee",
              approx(summary["cash"], 100 - 2.0784), str(summary["cash"]))
        check("open position marked to bid",
              approx(summary["unrealized_mark_to_bid"], 4.6 * .39 - 2.0784),
              str(summary))
        rows = [json.loads(line) for line in
                (pathlib.Path(tmp) / "paper_orders.jsonl").read_text().splitlines()]
        check("full execution audit written", rows[0]["status"] == "FILLED" and
              len(rows[0]["levels"]) == 2, str(rows))


def t_paper_pre_submit_guard_runs_after_latency_and_quote():
    import paper_trade as paper

    events = []
    allowed = {"value": True}
    original_sleep = paper.time.sleep
    original_estimate = paper.estimate_fok

    def tracked_estimate(*args, **kwargs):
        quote = original_estimate(*args, **kwargs)
        events.append("quote")
        allowed["value"] = False
        return quote

    def guard():
        events.append("guard")
        return allowed["value"]

    paper.time.sleep = lambda _seconds: events.append("latency")
    paper.estimate_fok = tracked_estimate
    try:
        with tempfile.TemporaryDirectory() as tmp:
            broker = _broker(tmp)
            broker.latency_ms = 25.0
            accepted = paper_order(broker, pre_submit_guard=guard)
            check("paper guard runs after modeled latency and final quote",
                  events == ["latency", "quote", "guard"], str(events))
            check("signal flip during paper latency/quote rejects the fill",
                  accepted is False and not broker.ledger.seen,
                  f"accepted={accepted} seen={broker.ledger.seen}")
            check("paper guard rejection is explicit",
                  "pre-submit guard rejected" in (broker.last_error or "").lower(),
                  str(broker.last_error))

            def broken_guard():
                raise RuntimeError("do-not-persist-this-detail")

            accepted = paper_order(broker, pre_submit_guard=broken_guard)
            audit = (pathlib.Path(tmp) / "paper_orders.jsonl").read_text()
            check("paper guard exception fails closed without a fill",
                  accepted is False and not broker.ledger.seen,
                  f"accepted={accepted} seen={broker.ledger.seen}")
            check("paper guard exception is clear but redacted in durable audit",
                  "pre-submit guard failed closed: RuntimeError" in audit
                  and "do-not-persist-this-detail" not in audit,
                  audit)
    finally:
        paper.estimate_fok = original_estimate
        paper.time.sleep = original_sleep


def t_unmarkable_open_position_never_fakes_total_equity():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp)
        paper_order(broker)
        summary = broker.summary(mark=lambda _token: None)
        check("unmarkable paper position is flagged",
              summary["unmarkable_positions"] == 1, str(summary))
        check("unmarkable paper position withholds equity",
              summary["equity"] is None and summary["total_pnl"] is None,
              str(summary))


def t_rejections_never_create_positions():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp, selected_book=book(minimum=100))
        check("unfillable paper order returns false",
              paper_order(broker) is False)
        check("rejection creates no ledger fill",
              broker.ledger.summary()["fills_counted"] == 0,
              str(broker.ledger.summary()))
        check("rejection reason retained",
              "minimum" in (broker.last_error or "").lower()
              or "no-fill" in (broker.last_error or "").lower()
              or "no fill" in (broker.last_error or "").lower(),
              str(broker.last_error))


def t_cash_and_round_mapping_are_fail_closed():
    with tempfile.TemporaryDirectory() as tmp:
        poor = _broker(tmp, balance=1)
        check("insufficient paper cash rejects",
              paper_order(poor) is False)
        check("cash reject records no fill", not poor.ledger.seen)
    with tempfile.TemporaryDirectory() as tmp:
        mismatch = _broker(tmp)
        check("wrong-round token rejects",
              paper_order(mismatch, up="old-up") is False)
        check("wrong-round token never reaches ledger", not mismatch.ledger.seen)
    with tempfile.TemporaryDirectory() as tmp:
        missing = _broker(tmp, context={"up_token_id": "up", "down_token_id": "down"})
        check("missing condition rejects because PnL could not settle",
              paper_order(missing) is False)
        check("missing condition creates no fill", not missing.ledger.seen)


def t_official_resolution_credits_cash_and_pnl_once():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp)
        paper_order(broker)
        before = broker.cash_balance()
        done = broker.ledger.settle(Resolution(
            "cond", RESOLVED, {"up": 1.0, "down": 0.0}, winning_label="Up"))
        after = broker.cash_balance()
        check("venue resolution settles paper position", len(done) == 1)
        check("winner payout credited to paper cash once",
              approx(after, before + 4.6), f"{before} -> {after}")
        check("realized PnL includes exact fee",
              approx(broker.summary()["realized_pnl"], 4.6 - 2.0784),
              str(broker.summary()))
        broker.ledger.settle(Resolution(
            "cond", RESOLVED, {"up": 1.0, "down": 0.0}, winning_label="Up"))
        check("repeat resolution cannot double-credit", approx(broker.cash_balance(), after))


def t_background_settlement_credits_cash_once_and_survives_reload():
    async def scenario(tmp):
        broker = _broker(tmp)
        paper_order(broker)
        before = broker.cash_balance()
        fetches = []

        def fetch(cid):
            fetches.append(cid)
            if len(fetches) == 1:
                return Resolution(cid, PENDING, {}, detail="venue still open")
            return Resolution(cid, RESOLVED, {"up": 1.0, "down": 0.0},
                              winning_label="Up")

        worker = SettlementWorker(broker.ledger, fetch=fetch, interval=0.01)
        worker.start()
        try:
            deadline = time.monotonic() + 1.0
            while (time.monotonic() < deadline
                   and not broker.ledger.positions["up"].settled):
                await asyncio.sleep(0.005)
            after = broker.cash_balance()
            check("background settlement observes pending before resolved",
                  len(fetches) >= 2, str(fetches))
            check("background worker settles the paper position",
                  broker.ledger.positions["up"].settled, worker.summary())
            check("background settlement credits the exact winner payout once",
                  approx(after, before + 4.6), f"{before} -> {after}")
            surfaced = broker.get_balance_allowance()
            check("settled payout is immediately visible through the balance API",
                  approx(surfaced["balance"], after)
                  and approx(surfaced["allowance"], after),
                  str(surfaced))
            check("settled position leaves no work for another poll",
                  await worker.run_once() == 0, str(worker.summary()))
            check("repeat polling cannot double-credit background settlement",
                  approx(broker.cash_balance(), after), str(broker.cash_balance()))
        finally:
            await worker.stop()

        reloaded = _broker(tmp, balance=999999)
        check("background settlement survives ledger reload",
              reloaded.ledger.positions["up"].settled,
              str(reloaded.ledger.positions["up"]))
        check("reloaded broker reconstructs the credited cash exactly",
              approx(reloaded.cash_balance(), after),
              f"{reloaded.cash_balance()} vs {after}")

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(tmp))


def t_execution_window_covers_every_enabled_phase():
    """A phase-1 order arrives ~4 minutes before phase 2's window opens.

    The broker refuses anything outside the round's execution interval, so
    sizing that interval from TRADE_LAST_SECONDS alone rejects every phase-1
    submission before it can reach a book. That failure is invisible to a loop
    test that stubs place_trade, which is exactly how it shipped.
    """
    import config

    check("the execution window reaches phase 1's first second",
          config.EXECUTION_WINDOW_SECONDS >= config.PHASE1_START_SECONDS,
          f"{config.EXECUTION_WINDOW_SECONDS} < {config.PHASE1_START_SECONDS}")
    check("and still covers phase 2",
          config.EXECUTION_WINDOW_SECONDS >= config.TRADE_LAST_SECONDS,
          str(config.EXECUTION_WINDOW_SECONDS))

    import timer as timer_mod
    from dataclasses import replace

    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp)
        # Size the interval the way run_feeds does.
        broker.trade_window_seconds = float(config.EXECUTION_WINDOW_SECONDS)
        end = round_end()
        original_unix = timer_mod.unix
        original_wall = timer_mod.wall
        # Stand at the top of the round, where phase 1 trades and phase 2
        # has not opened yet.
        sampled = end - 290.0
        timer_mod.unix = lambda *_a, **_k: sampled
        timer_mod.wall = lambda *_a, **_k: sampled
        try:
            # Interval uses Unix; book freshness uses CLOB wall. Keep them
            # on the same synthetic clock so this is an interval test.
            broker._book_fetch = lambda token: replace(
                book(),
                timestamp=str(int(sampled * 1000)),
                received_wall=sampled,
            )
            accepted = paper_order(broker, amount=2.5, end=end)
        finally:
            timer_mod.unix = original_unix
            timer_mod.wall = original_wall
        check("a phase-1 timestamp is not refused as out-of-interval",
              "outside the current round execution interval"
              not in (broker.last_error or ""), str(broker.last_error))
        check("a phase-1 order fills against a contemporaneous book",
              accepted is True, broker.last_error)

        # And the guard still bites outside every phase.
        timer_mod.unix = lambda *_a, **_k: end - 900.0
        try:
            paper_order(broker, amount=2.5, end=end)
        finally:
            timer_mod.unix = original_unix
        check("a timestamp before the round still is refused",
              "outside the current round execution interval"
              in (broker.last_error or ""), str(broker.last_error))
    tree = ast.parse((ROOT / "run_feeds.py").read_text(encoding="utf-8"))
    runner = next((node for node in ast.walk(tree)
                   if isinstance(node, ast.AsyncFunctionDef)
                   and node.name == "_run_inner"), None)
    check("feed runner still owns settlement startup", runner is not None)
    if runner is None:
        return

    # Scan the whole runner module, not just `_run_inner`: startup is split
    # across helpers it calls, and the invariant is that run_feeds builds one
    # worker at one second - not which function holds the call.
    constructors = [node for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and ast.unparse(node.func) == "SettlementWorker"]
    check("feed runner constructs exactly one settlement worker",
          len(constructors) == 1, str(len(constructors)))
    if len(constructors) != 1:
        return

    interval = next((kw.value for kw in constructors[0].keywords
                     if kw.arg == "interval"), None)
    check("production settlement polling is explicitly one second",
          isinstance(interval, ast.Constant) and interval.value == 1.0,
          "missing interval" if interval is None else ast.unparse(interval))


def t_directory_fsync_permission_denied_does_not_block_persist():
    import os
    from accounting.ledger import _fsync_directory
    with tempfile.TemporaryDirectory() as tmp:
        real_open = os.open

        def deny_dir(path, flags, *args, **kwargs):
            if flags == os.O_RDONLY and pathlib.Path(path).is_dir():
                raise PermissionError(13, "Permission denied", path)
            return real_open(path, flags, *args, **kwargs)

        os.open = deny_dir
        try:
            _fsync_directory(tmp)
            broker = _broker(tmp)
            check("paper account created without directory fsync",
                  (pathlib.Path(tmp) / "paper_account.json").exists())
            check("ledger save survives directory PermissionError",
                  broker.ledger.save())
        finally:
            os.open = real_open
    with tempfile.TemporaryDirectory() as tmp:
        first = _broker(tmp, balance=100)
        paper_order(first)
        cash = first.cash_balance()
        second_ledger = Ledger(path=str(pathlib.Path(tmp) / "paper_ledger.json"))
        second = PaperBroker(
            second_ledger,
            market_context=lambda: {"condition_id": "cond", "up_token_id": "up",
                                    "down_token_id": "down"},
            host="https://public.invalid", max_buy_price=.99,
            start_balance=999999,
            account_path=pathlib.Path(tmp) / "paper_account.json",
            audit_path=pathlib.Path(tmp) / "paper_orders.jsonl",
            book_fetch=lambda token: book(token), rules_fetch=lambda cid: rules(),
            min_seconds_to_expiry=0, trade_window_seconds=300)
        check("saved starting balance wins after restart", second.start_balance == 100,
              str(second.start_balance))
        check("restart reconstructs identical cash", approx(second.cash_balance(), cash),
              f"{second.cash_balance()} vs {cash}")
        check("restart preserves deduplicated fills", len(second.ledger.seen) == 1)


def t_missing_account_file_cannot_silently_reset_existing_ledger():
    with tempfile.TemporaryDirectory() as tmp:
        first = _broker(tmp, balance=100)
        paper_order(first)
        (pathlib.Path(tmp) / "paper_account.json").unlink()
        loaded = Ledger(path=str(pathlib.Path(tmp) / "paper_ledger.json"))
        try:
            PaperBroker(
                loaded,
                market_context=lambda: {"condition_id": "cond", "up_token_id": "up",
                                        "down_token_id": "down"},
                host="https://public.invalid", max_buy_price=.99,
                start_balance=100,
                account_path=pathlib.Path(tmp) / "paper_account.json",
                audit_path=pathlib.Path(tmp) / "paper_orders.jsonl",
                book_fetch=lambda token: book(token), rules_fetch=lambda cid: rules(),
                min_seconds_to_expiry=0, trade_window_seconds=300)
        except RuntimeError as exc:
            check("existing ledger without account fails closed",
                  "contains fills" in str(exc), str(exc))
        else:
            check("existing ledger without account fails closed", False,
                  "paper account was silently recreated")


def t_paper_account_and_ledger_have_one_durable_identity():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        broker = _broker(root, balance=123)
        account = json.loads((root / "paper_account.json").read_text())
        ledger = json.loads((root / "paper_ledger.json").read_text())
        check("new paper account uses portable identity schema",
              account.get("version") == 2, str(account))
        check("new paper ledger identity is durable before the first fill",
              ledger.get("version") == 4 and
              ledger.get("ledger_id") == account.get("ledger_id") == broker.ledger.ledger_id,
              f"account={account.get('ledger_id')} ledger={ledger.get('ledger_id')}")


def t_paper_state_directory_move_migrates_legacy_pair_without_reset():
    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        source = base / "old-location"
        source.mkdir()
        first = _broker(source, balance=100)
        check("legacy-move fixture has a durable fill", paper_order(first))
        cash_before = first.cash_balance()

        # Recreate the exact pre-fix V1/V3 schemas, including the absolute old
        # path that used to make a whole-directory move unbootable.
        account_path = source / "paper_account.json"
        account = json.loads(account_path.read_text())
        account["version"] = 1
        account.pop("ledger_id", None)
        account_path.write_text(json.dumps(account), encoding="utf-8")
        ledger_path = source / "paper_ledger.json"
        ledger = json.loads(ledger_path.read_text())
        ledger["version"] = 3
        ledger.pop("ledger_id", None)
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

        destination = base / "new-location"
        shutil.move(str(source), str(destination))
        reopened = _broker(destination, balance=999999)
        migrated_account = json.loads(
            (destination / "paper_account.json").read_text())
        migrated_ledger = json.loads(
            (destination / "paper_ledger.json").read_text())
        check("moved legacy state keeps its original starting cash",
              reopened.start_balance == 100, str(reopened.start_balance))
        check("moved legacy state keeps every fill and exact cash",
              len(reopened.ledger.seen) == 1 and
              approx(reopened.cash_balance(), cash_before),
              f"fills={reopened.ledger.seen} cash={reopened.cash_balance()}")
        check("legacy account and ledger migrate to one V2/V4 identity",
              migrated_account.get("version") == 2 and
              migrated_ledger.get("version") == 4 and
              migrated_account.get("ledger_id") == migrated_ledger.get("ledger_id"),
              f"account={migrated_account} ledger_id={migrated_ledger.get('ledger_id')}")
        check("migrated account records its current diagnostic path",
              pathlib.Path(migrated_account["ledger_path"]).resolve()
              == (destination / "paper_ledger.json").resolve(),
              migrated_account["ledger_path"])


def t_missing_or_replaced_paper_ledger_cannot_reset_pnl():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        first = _broker(root, balance=100)
        check("missing-ledger fixture has a fill", paper_order(first))
        account_before = (root / "paper_account.json").read_bytes()
        (root / "paper_ledger.json").unlink()
        try:
            _broker(root, balance=999999)
        except RuntimeError as exc:
            check("account without its ledger fails closed",
                  "ledger file is missing" in str(exc), str(exc))
        else:
            check("account without its ledger fails closed", False,
                  "PnL silently reset to an empty ledger")
        check("failed recovery does not rewrite the surviving account",
              (root / "paper_account.json").read_bytes() == account_before)

    with tempfile.TemporaryDirectory() as tmp:
        base = pathlib.Path(tmp)
        left, right = base / "left", base / "right"
        left.mkdir()
        right.mkdir()
        _broker(left, balance=100)
        _broker(right, balance=200)
        shutil.copyfile(left / "paper_account.json", right / "paper_account.json")
        try:
            _broker(right, balance=200)
        except RuntimeError as exc:
            check("account paired with a replacement ledger fails closed",
                  "identities do not match" in str(exc), str(exc))
        else:
            check("account paired with a replacement ledger fails closed", False,
                  "mismatched state pair was accepted")


def t_paper_install_blocks_live_client_and_rebinds_every_action():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp)
        fake_live = types.SimpleNamespace(_client="old", last_order_error=None)
        fake_live._get_client = lambda: "DANGEROUS"
        def disable_live_execution():
            fake_live._client = None
        fake_live.disable_live_execution = disable_live_execution
        fake_main = types.SimpleNamespace(
            execution_mode="LIVE", _paper_broker=None,
            get_balance_allowance=lambda: None,
            cancel_all_open_orders=lambda: False,
            place_trade=lambda *_a: False,
            TRADE_LOG=pathlib.Path("live.csv"))
        previous = sys.modules.get("polymarket_trade")
        sys.modules["polymarket_trade"] = fake_live
        try:
            install_paper_execution(fake_main, broker, log_path=pathlib.Path(tmp) / "paper.csv")
            check("paper mode banner state installed", fake_main.execution_mode == "PAPER")
            check("balance rebound to paper account",
                  fake_main.get_balance_allowance.__self__ is broker)
            check("cancel rebound to paper engine",
                  fake_main.cancel_all_open_orders.__self__ is broker)
            check("order rebound to paper engine", fake_main.place_trade.__self__ is broker)
            try:
                fake_live._get_client()
            except RuntimeError as exc:
                check("live client gateway poisoned",
                      "disabled by --paper" in str(exc), str(exc))
            else:
                check("live client gateway poisoned", False, "guard did not raise")
            check("cached live client erased", fake_live._client is None)
        finally:
            if previous is None:
                sys.modules.pop("polymarket_trade", None)
            else:
                sys.modules["polymarket_trade"] = previous


def t_runner_paper_build_never_derives_credentials():
    import run_feeds
    from feeds import adapters

    original = run_feeds.derive_creds
    calls = {"n": 0}

    def forbidden():
        calls["n"] += 1
        raise AssertionError("credential derivation reached")

    run_feeds.derive_creds = forbidden
    try:
        _hub, cfg, _agreement = run_feeds.build_hub(paper=True)
        check("paper hub never derives credentials", calls["n"] == 0, str(calls))
        check("paper user websocket forced off", cfg.user_ws == "off", cfg.describe())
        check("paper wallet reconcile forced off", cfg.reconcile == "off", cfg.describe())
    finally:
        run_feeds.derive_creds = original
        if getattr(adapters, "_installed", False):
            adapters.uninstall()


def t_paper_feed_start_omits_private_user_task():
    import run_feeds

    class FakeHub:
        def __init__(self):
            self.user_requested = None
            self.binance_requested = None

        def start(self, *, user=True, binance=True):
            self.user_requested = user
            self.binance_requested = binance
            return []

    hub = FakeHub()
    cfg = types.SimpleNamespace(user_ws="off", btc_feed="ws")
    tasks = run_feeds._start_feed_tasks(hub, cfg)
    check("paper start creates no private user task", hub.user_requested is False,
          str(hub.user_requested))
    check("paper start still runs the public price feed",
          hub.binance_requested is True, str(hub.binance_requested))
    check("paper start returns only enabled tasks", tasks == [], str(tasks))


def t_paper_dashboard_renders_exact_geometry():
    from dashboard import TerminalState, build, glyphs, snapshot

    state = TerminalState()
    state.mode = "PAPER"
    state.balance.set({"balance": 987.654321, "allowance": 987.654321,
                       "paper": True}, source="paper ledger")
    state.accounting = {
        "realized_pnl": -1.25,
        "unrealized_mark_to_bid": 0.75,
        "total_pnl": -0.50,
        "pending_cost": 11.10,
        "win_rate": 2 / 3,
        "wins": 2,
        "losses": 1,
    }
    snap = snapshot(state)
    for cols, rows in ((200, 60), (120, 40), (80, 24), (40, 12), (20, 4)):
        frame = build(snap, cols, rows, glyphs())
        check(f"paper dashboard row count {cols}x{rows}", len(frame) == rows,
              str(len(frame)))
        widths = [sum(len(text) for text, _style in row) for row in frame]
        check(f"paper dashboard column count {cols}x{rows}",
              all(width == cols for width in widths), str(widths))
    full_text = "\n".join(
        "".join(text for text, _style in row)
        for row in build(snap, 120, 40, glyphs()))
    check("paper dashboard visibly identifies mode", "PAPER" in full_text, full_text)
    check("paper dashboard exposes PnL", "TOTAL PNL" in full_text, full_text)


def t_fill_delay_probes_record_later_quotes_without_touching_cash():
    """Probes re-quote the same order later and change nothing about the fill.

    Paper matched every archived order after exactly PAPER_LATENCY_MS while
    live won 10.5 points less often at the same prices. The probes measure how
    that gap opens with delay, and they are only trustworthy if they are pure
    observation - so this pins that they never alter cash or the ledger.
    """
    import paper_trade as paper

    scheduled = []
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp)
        broker.fill_delay_probes = (1.0, 3.0)
        broker._schedule_probe = lambda wait, fn: scheduled.append((wait, fn))
        check("a probed order still fills", paper_order(broker))
        cash_after_fill = broker.cash_balance()
        seen_after_fill = len(broker.ledger.seen)
        check("one probe is scheduled per configured delay",
              [round(w, 3) for w, _ in scheduled] == [1.0, 3.0], str(scheduled))
        if len(scheduled) == 2:
            scheduled[0][1]()                    # 1s later: same book, fills
            original = paper.estimate_fok
            paper.estimate_fok = lambda *a, **k: (_ for _ in ()).throw(
                paper.PaperRejected("FOK no-fill: price moved past the cap"))
            try:
                scheduled[1][1]()                # 3s later: the price has run
            finally:
                paper.estimate_fok = original

        journal = pathlib.Path(tmp) / "paper_orders_fill_delay.jsonl"
        rows = ([json.loads(line) for line in journal.read_text().splitlines()]
                if journal.exists() else [])
        by_delay = {r["delay_s"]: r for r in rows
                    if r.get("reason") != "actual paper fill"}
        check("the actual fill is recorded as the baseline row",
              sum(r.get("reason") == "actual paper fill" for r in rows) == 1
              and rows[0].get("fillable") is True, str(rows))
        check("a later book that still fills is recorded as fillable",
              by_delay.get(1.0, {}).get("fillable") is True
              and bool(by_delay.get(1.0, {}).get("average_price")), str(rows))
        check("a later book that would miss is a no-fill, not an error",
              by_delay.get(3.0, {}).get("fillable") is False
              and "no-fill" in by_delay.get(3.0, {}).get("reason", ""), str(rows))
        check("probes never move cash",
              approx(broker.cash_balance(), cash_after_fill),
              f"{broker.cash_balance()} vs {cash_after_fill}")
        check("probes never write to the ledger",
              len(broker.ledger.seen) == seen_after_fill, str(broker.ledger.seen))


def t_fill_delay_probe_failures_never_reach_the_fill_path():
    """A broken probe must not stop the bot.

    place_trade treats an unexpected exception as fatal and re-raises it, so
    a scheduler or book-read failure escaping the probe code would halt
    trading over a diagnostic.
    """
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp)
        broker.fill_delay_probes = (2.0,)

        def exploding_scheduler(_wait, _fn):
            raise RuntimeError("timer thread unavailable")

        broker._schedule_probe = exploding_scheduler
        check("a failing scheduler still lets the fill complete",
              paper_order(broker) is True, str(broker.last_error))

        captured = []
        broker._schedule_probe = lambda _wait, fn: captured.append(fn)
        paper_order(broker)
        check("the next fill schedules its probe", bool(captured), str(captured))
        raised = None
        if captured:
            broker._book_fetch = lambda _token: (_ for _ in ()).throw(
                TimeoutError("book read timed out"))
            try:
                captured[0]()
            except Exception as exc:
                raised = exc
        check("a probe whose book read fails does not raise", raised is None,
              repr(raised))
        journal = pathlib.Path(tmp) / "paper_orders_fill_delay.jsonl"
        rows = ([json.loads(line) for line in journal.read_text().splitlines()]
                if journal.exists() else [])
        errored = [r for r in rows if r.get("fillable") is None]
        check("a failed probe is recorded as an error, kept out of statistics",
              bool(errored) and "probe error" in errored[-1].get("reason", ""),
              str(rows))


def t_fill_delay_probes_are_off_by_default():
    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp)
        check("no probes are configured by default", broker.fill_delay_probes == ())
        paper_order(broker)
        check("and no delay journal is written",
              not (pathlib.Path(tmp) / "paper_orders_fill_delay.jsonl").exists())


def t_live_fill_timing_journal_records_post_and_response():
    """Live must record when an order was POSTed and when the venue answered.

    Receipts carried no timestamps, so the real submit-to-match time - the
    number that decides whether paper can predict live - was never measured.
    """
    import os
    import polymarket_trade as trade

    rows = []
    swallowed = False
    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "timing.jsonl"
        saved = os.environ.get("LIVE_FILL_TIMING_PATH")
        os.environ["LIVE_FILL_TIMING_PATH"] = str(target)
        try:
            trade._record_fill_timing("oid-1", "matched", None, 100.0, 100.25,
                                      token_id="up", condition_id="cond")
            rows = [json.loads(line) for line in target.read_text().splitlines()]
            # A directory is not appendable: the write must fail quietly.
            os.environ["LIVE_FILL_TIMING_PATH"] = tmp
            trade._record_fill_timing("oid-2", "matched", None, 1.0, 2.0,
                                      token_id="up", condition_id="cond")
            swallowed = True
        except Exception:
            swallowed = False
        finally:
            if saved is None:
                os.environ.pop("LIVE_FILL_TIMING_PATH", None)
            else:
                os.environ["LIVE_FILL_TIMING_PATH"] = saved
    check("a POST is recorded with both timestamps",
          bool(rows) and rows[0]["order_id"] == "oid-1"
          and rows[0]["submitted_wall"] == 100.0
          and abs(rows[0]["post_seconds"] - 0.25) < 1e-9, str(rows))
    check("an unwritable journal never raises into the order path", swallowed)


def _book_at(best, token="up"):
    """A one-level book at ``best`` so a fill's price is unambiguous."""
    return BookSnapshot(
        token, ((D(str(best)), D("50")),),
        min_order_size=None, tick_size=D("0.01"),
        timestamp=str(int(time.time() * 1000)), book_hash="abc",
        received_wall=time.time(), best_bid=D(str(round(best - 0.01, 2))))


def _inflight_broker(tmp, books, minimum=None):
    """A broker whose book changes between the decision and the arrival."""
    tmp = pathlib.Path(tmp)
    ledger = Ledger(path=str(tmp / "paper_ledger.json"))
    return PaperBroker(
        ledger,
        market_context=lambda: {"condition_id": "cond", "up_token_id": "up",
                                "down_token_id": "down"},
        host="https://public.invalid", max_buy_price=.99, start_balance=100,
        account_path=tmp / "paper_account.json",
        audit_path=tmp / "paper_orders.jsonl",
        book_fetch=lambda _token: books.pop(0) if books else _book_at(0.40),
        rules_fetch=lambda _cid: rules(minimum=minimum),
        min_seconds_to_expiry=0, trade_window_seconds=300,
        latency_ms=1.0)


def t_paper_fills_on_the_price_limit_exactly_as_a_live_fok_does():
    """A FOK cares about the limit price, not which way the ask moved.

    Paper used to refuse a fill whenever the ask ticked up, however far the
    order's cap actually was - so it kept only the fills where the price had
    moved in its favour and booked those improved prices as edge. Live has no
    such rule: a signed order fills at any price up to its limit and refuses
    past it.
    """
    with tempfile.TemporaryDirectory() as tmp:
        risen = _inflight_broker(tmp, [_book_at(0.40), _book_at(0.45)])
        check("an ask that rose but stayed under the cap still fills",
              paper_order(risen, amount=2), str(risen.last_error))
        check("and it is booked at the price actually paid, not the one seen",
              abs(risen.last_fill["average_price"] - 0.45) < 1e-9,
              str(risen.last_fill["average_price"]))

    with tempfile.TemporaryDirectory() as tmp:
        # 0.995 is above the broker's 0.99 ceiling; 0.95 would still fill.
        past_cap = _inflight_broker(tmp, [_book_at(0.40), _book_at(0.995)])
        check("an ask that rose past the cap does not fill",
              paper_order(past_cap, amount=2) is False, str(past_cap.last_error))

    with tempfile.TemporaryDirectory() as tmp:
        fallen = _inflight_broker(tmp, [_book_at(0.40), _book_at(0.35)])
        check("an ask that fell still fills - the adverse half is kept too",
              paper_order(fallen, amount=2), str(fallen.last_error))


def t_paper_fixes_the_stake_before_the_delay_like_a_signed_order():
    """Live sizes and signs in preflight; the amount cannot change in flight.

    Paper sized AFTER its simulated latency, against the very book it was
    about to fill on, so it could tune its stake to liquidity a real order
    had not seen yet. Here the venue minimum is 5 shares: $2.00 buys them at
    the decision price of 0.40, but not at the arrival price of 0.60. A fixed
    stake must therefore be refused for being under the minimum. Re-sizing
    after the delay would quietly raise the stake to $3.00 and fill.
    """
    with tempfile.TemporaryDirectory() as tmp:
        moved = _inflight_broker(tmp, [_book_at(0.40), _book_at(0.60)],
                                 minimum=5)
        filled = paper_order(moved, amount=2)
        check("the stake is not re-sized against the arrival book",
              filled is False, str(moved.last_error))
        check("and it is refused for the venue minimum, not for the price",
              "minimum" in (moved.last_error or "").lower(),
              str(moved.last_error))

    with tempfile.TemporaryDirectory() as tmp:
        steady = _inflight_broker(tmp, [_book_at(0.40), _book_at(0.40)],
                                  minimum=5)
        check("an unchanged book still fills at the fixed stake",
              paper_order(steady, amount=2), str(steady.last_error))
        check("buying exactly the venue minimum",
              abs(steady.last_fill["shares"] - 5.0) < 1e-9,
              str(steady.last_fill["shares"]))


def t_paper_refuses_to_size_an_order_from_a_stale_book():
    """The book an order is SIZED from must be as fresh as the one it fills on.

    Fixing the stake at decision time introduced a second book read, and it
    was validated only for shape - not for age. Live gets that check for
    free: every read goes through parse_orderbook, which enforces
    ORDERBOOK_MAX_AGE_SECONDS, so live can never size off a book paper would
    happily have used.
    """
    stale = BookSnapshot(
        "up", ((D("0.40"), D("50")),), tick_size=D("0.01"),
        timestamp=str(int(time.time() * 1000)), book_hash="abc",
        received_wall=time.time() - 40.0, best_bid=D("0.39"))

    with tempfile.TemporaryDirectory() as tmp:
        broker = _inflight_broker(tmp, [stale, _book_at(0.40)])
        check("a stale decision book is refused before any sizing",
              paper_order(broker, amount=2) is False, str(broker.last_error))
        check("and it says the book was stale, not that the price was wrong",
              "stale in hand" in (broker.last_error or ""), str(broker.last_error))

    with tempfile.TemporaryDirectory() as tmp:
        arrival = _inflight_broker(tmp, [_book_at(0.40), stale])
        check("a stale arrival book is still refused too",
              paper_order(arrival, amount=2) is False, str(arrival.last_error))


def main() -> int:
    for name, test in sorted(globals().items()):
        if not name.startswith("t_") or not callable(test):
            continue
        try:
            test()
        except Exception as exc:
            global FAIL
            FAIL += 1
            FAILURES.append(f"{name} raised {type(exc).__name__}: {exc}")
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print(f"\n{PASS} passed, {FAIL} failed")
    for failure in FAILURES[:30]:
        print("  -", failure)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
