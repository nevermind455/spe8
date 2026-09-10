#!/usr/bin/env python3
"""Tests for the C1 freeze fix and the Chainlink strike.

    python tests_fixes.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib.util
import io
import json
import pathlib
import sys
import threading
import time
import types
from decimal import Decimal

sys.path.insert(0, ".")

# Run against config.py's built-in defaults, never the operator's .env.
#
# Every assertion in this file is written against the defaults. config.py calls
# load_dotenv(override=False), so the suite cannot neutralise a knob by
# deleting it - .env simply repopulates it on the next reload - and knobs whose
# default is computed (MAX_ROUND_EXPOSURE) cannot be pinned to a literal
# either. Stubbing the loader before config is first imported is the only way
# to get all 46 keys at once, and it survives importlib.reload() because
# config re-runs `from dotenv import load_dotenv` each time.
#
# Without this the suite graded whatever the operator had most recently tuned
# instead of the code: a 7-band ladder plus PHASE2_MULTI_SIGNAL=1 turned 22
# tests red, including one that timed out because no order was ever reached.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *_a, **_kw: False

if importlib.util.find_spec("requests") is None:
    requests_stub = types.ModuleType("requests")
    requests_stub.get = lambda *_a, **_kw: None
    sys.modules["requests"] = requests_stub

import chainlink_strike as strike_mod  # noqa: E402
import market_discovery  # noqa: E402
from chainlink_strike import (ChainlinkStrike, PING_EVERY, RAW_TOPIC,  # noqa: E402
                              SDK_TOPIC, TWAP_WINDOW, window_start)

P = F = 0


def check(name, cond, detail=""):
    global P, F
    if cond:
        P += 1
    else:
        F += 1
        print(f"  FAIL {name} {detail}")


# ----------------------------------------------------------------- C1 ---
# threading.active_count() is a noisy global - other libraries start threads
# and a flaky test is worse than no test. Count the blocked workers directly.
BLOCKED = {"n": 0}


def _blocking_wait(ev):
    BLOCKED["n"] += 1
    ev.wait()
    BLOCKED["n"] -= 1


async def t_c1_old_pattern_leaks_threads():
    """The original: wait_for cancels the future, the thread never returns."""
    ev = threading.Event()
    BLOCKED["n"] = 0
    timeouts = 0
    for _ in range(6):
        try:
            await asyncio.wait_for(asyncio.to_thread(_blocking_wait, ev), timeout=0.05)
        except asyncio.TimeoutError:
            timeouts += 1
    check("old pattern times out on every stranded wait", timeouts == 6, str(timeouts))
    check("old pattern strands a worker per attempt", BLOCKED["n"] >= 5,
          f"only {BLOCKED['n']} blocked after 6 attempts")
    ev.set()
    await asyncio.sleep(0.2)
    check("stranded workers only clear when the event fires", BLOCKED["n"] == 0,
          str(BLOCKED["n"]))


async def _new_sleep(seconds, stop_event, tick=0.1):
    """Exactly what main_bot.py now runs."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and not stop_event.is_set():
        await asyncio.sleep(tick)


async def t_c1_fix_leaks_nothing():
    ev = threading.Event()
    BLOCKED["n"] = 0
    for _ in range(12):
        await _new_sleep(0.05, ev, tick=0.01)
    check("fixed pattern strands nothing", BLOCKED["n"] == 0, str(BLOCKED["n"]))
    check("fixed pattern uses no worker thread at all", BLOCKED["n"] == 0)


async def t_c1_fix_still_exits_early():
    ev = threading.Event()
    t0 = time.monotonic()
    task = asyncio.create_task(_new_sleep(6.0, ev, tick=0.02))
    await asyncio.sleep(0.1)
    ev.set()
    await task
    dt = time.monotonic() - t0
    check("stop_event still interrupts the wait", dt < 0.5, f"{dt:.2f}s")


async def t_c1_fix_waits_the_full_time():
    ev = threading.Event()
    t0 = time.monotonic()
    await _new_sleep(0.4, ev, tick=0.02)
    dt = time.monotonic() - t0
    check("waits the full duration when not stopped", 0.35 <= dt <= 0.7, f"{dt:.2f}s")


# ------------------------------------------------------------- strike ---
def _msg(value, ts_ms, topic=RAW_TOPIC, symbol="btc/usd",
         twap_window=TWAP_WINDOW, full_accuracy_value=None):
    payload = {"symbol": symbol, "timestamp": ts_ms,
               "value": str(value), "window_s": twap_window}
    if full_accuracy_value is not None:
        payload["full_accuracy_value"] = str(full_accuracy_value)
    return json.dumps({"topic": topic, "type": "update", "timestamp": ts_ms,
                       "payload": payload})


def t_strike_takes_the_exact_boundary_observation():
    s = ChainlinkStrike()
    w = 1786320000                       # a 300-aligned boundary
    check("boundary maths", window_start(w + 17) == w, str(window_start(w + 17)))

    s._handle(_msg(64_894.00, w * 1000))          # first print of the window
    s._handle(_msg(64_910.55, (w + 12) * 1000))   # later prints must not win
    s._handle(_msg(64_870.10, (w + 240) * 1000))
    check("strike is the exact-boundary TWAP observation",
          s.strike_for(w) == Decimal("64894.0"), str(s.strike_for(w)))
    check("latest TWAP still tracked",
          s.value == Decimal("64870.1"), str(s.value))

    s._handle(_msg(65_000.00, (w + 300) * 1000))  # next window
    check("next window gets its own strike", s.strike_for(w + 300) == 65_000.00)
    check("previous window untouched", s.strike_for(w) == 64_894.00)
    check("unseen window returns None, never a guess", s.strike_for(w + 600) is None)


def t_strike_accepts_raw_and_sdk_twap_topics():
    for topic in (RAW_TOPIC, SDK_TOPIC):
        s = ChainlinkStrike()
        w = 1786320000
        s._handle(_msg(1.0, w * 1000, topic=topic))
        check(f"parses {topic}", s.strike_for(w) == 1.0)


def t_strike_prefers_exact_e18_and_rejects_stale_values():
    s = ChainlinkStrike(stale_after=20.0)
    now_ms = int(time.time() * 1000)
    exact = 64894123456789012345678
    s._handle(_msg(1.0, now_ms, full_accuracy_value=exact))
    check("signed E18 value wins over display float",
          s.value == Decimal(exact) / (Decimal(10) ** 18), str(s.value))
    check("new TWAP is live", s.current_value() == s.value)
    s.value_mono -= 21.0
    check("stale TWAP is withheld from the strategy", s.current_value() is None)


def t_out_of_order_packet_cannot_rewind_live_twap():
    s = ChainlinkStrike()
    w = 1786320000
    s._handle(_msg(101, (w + 10) * 1000))
    s._handle(_msg(99, w * 1000))
    check("late delivery can still supply the exact boundary observation",
          s.strike_for(w) == Decimal("99"), str(s.strike_for(w)))
    check("late packet cannot rewind the live TWAP",
          s.value == Decimal("101"), str(s.value))


def t_boundary_plus_one_is_never_used_as_the_opening_twap():
    s = ChainlinkStrike()
    w = 1786320000
    s._handle(_msg(101, (w + 1) * 1000))
    check("boundary+1s is not the official opening observation",
          s.strike_for(w) is None, str(s.strikes))
    s._handle(_msg(99, w * 1000))
    check("an out-of-order exact-boundary packet is accepted",
          s.strike_for(w) == Decimal("99"), str(s.strikes))


def t_strike_ignores_noise():
    s = ChainlinkStrike()
    w = 1786320000
    s._handle("PONG")
    s._handle("not json")
    s._handle(_msg(9.0, w * 1000, topic="crypto_prices_chainlink"))  # spot topic
    s._handle(_msg(9.0, w * 1000, topic="prices.crypto.chainlink")) # SDK spot
    s._handle(_msg(9.0, w * 1000, symbol="eth/usd"))             # wrong symbol
    s._handle(_msg(9.0, w * 1000, twap_window=30))                # wrong TWAP window
    s._handle(_msg("abc", w * 1000))                              # bad value
    check("noise never becomes a strike", s.strike_for(w) is None, str(s.strikes))
    check("PONG does not crash", True)


def t_divergence_reports_the_binance_gap():
    s = ChainlinkStrike()
    w = 1786320000
    s._handle(_msg(64_894.00, w * 1000))
    d = s.divergence(64_901.50, w)
    check("diff computed", abs(d["diff"] - 7.50) < 1e-9, str(d))
    check("bps computed", abs(d["diff_bps"] - 1.1558) < 1e-3, str(d))
    check("no strike means no claim", s.divergence(64_901.50, w + 300)["diff"] is None)
    check("no binance price means no claim", s.divergence(None, w)["diff"] is None)


def t_strike_memory_is_bounded():
    s = ChainlinkStrike()
    w = 1786320000
    for i in range(400):
        s._handle(_msg(1000 + i, (w + i * 300) * 1000))
    check("strike map stays bounded", len(s.strikes) <= 200, str(len(s.strikes)))


def t_strike_default_window_uses_unix_not_a_lagging_clob_clock():
    import timer

    original_wall = timer.wall
    original_unix = timer.unix
    original_time = strike_mod.time.time
    try:
        # CLOB /time has already crossed the boundary; Unix has not.
        # Round identity must stay on Unix or the displayed round overruns
        # and the next market opens late.
        strike_mod.time.time = lambda: 1_786_320_299.8
        timer.unix = lambda *_a, **_k: 1_786_320_299.8
        timer.wall = lambda *_a, **_k: 1_786_320_301.2
        check("RTDS default window follows Unix, not CLOB /time",
              window_start() == 1_786_320_000, str(window_start()))
    finally:
        timer.wall = original_wall
        timer.unix = original_unix
        strike_mod.time.time = original_time


def t_strike_rejects_nonfinite_and_malformed_matching_frames():
    s = ChainlinkStrike()
    w = 1_786_320_000
    s._handle(_msg("Infinity", w * 1000))
    s._handle(_msg("NaN", w * 1000))
    s._handle(json.dumps({"topic": RAW_TOPIC, "type": "update", "payload": [1]}))
    s._handle(123)
    check("non-finite RTDS prices are rejected", s.value is None, str(s.value))
    check("malformed matching RTDS frames are counted", s.invalid_messages == 4,
          str(s.health()))
    s._handle(_msg("100", w * 1000))
    check("a trusted update clears recovered malformed-frame health",
          s.value == Decimal("100") and s.last_error is None, str(s.health()))


async def t_strike_start_is_idempotent():
    s = ChainlinkStrike()

    async def idle():
        await s._stop.wait()

    s._run = idle
    first = s.start()
    second = s.start()
    check("duplicate strike start returns the existing task", first is second)
    check("duplicate strike start creates one named task",
          sum(t.get_name() == "chainlink_strike" for t in asyncio.all_tasks()) == 1)
    await s.stop()
    check("strike stop releases its task reference", s._task is None)


def t_mid_window_reconnect_never_invents_a_boundary_strike():
    s = ChainlinkStrike()
    w = 1786320000
    s._connection_window = w
    s._handle(_msg(64_950.0, (w + 180) * 1000))
    check("mid-window first observation is not called the strike",
          s.strike_for(w) is None, str(s.strikes))
    check("skipped partial window is visible in health",
          s.health()["partial_windows_skipped"] == 1, str(s.health()))
    s._handle(_msg(65_000.0, (w + 300) * 1000))
    check("next full window can be captured",
          s.strike_for(w + 300) == 65_000.0, str(s.strikes))


def t_round_state_cannot_reuse_a_previous_strike():
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    # Both must be cleared at the transition, but they need not be adjacent -
    # a per-round one-shot flag legitimately sits between them. Match the
    # block rather than the exact two lines, so adding such a flag is not a
    # false failure on an invariant that still holds.
    _transition = source.split("active_window = round_window", 1)[-1][:600]
    check("round transition explicitly clears both start prices",
          "start_price = None" in _transition
          and "start_chainlink_price = None" in _transition)
    check("Binance strike is latched from the exchange timestamp",
          "active_window * 1000 <= ts_ms < (active_window + 5) * 1000" in source)
    check("stale last-known opening fallback was removed entirely",
          "last_known" not in source)
    # Reversed 2026-08-17: paper used to substitute a mid-round price when it
    # missed the open, which measures a different question than the market
    # asks and inverted the signal once price had moved.
    check("paper no longer latches a mid-round reference",
          "PAPER mid-round Binance reference" not in source)
    check("60s TWAP strike is looked up by active round",
          "chainlink_twap_for_round(active_window)" in source)
    check("spot Chainlink aggregator is absent from the decision path",
          "get_chainlink_btc_price" not in source and "import chainlink\n" not in source)
    check("Chainlink signal compares TWAP start with TWAP current",
          "chainlink_signal(\n                active_window, start_chainlink_price, current_cl)" in source and
          "current_cl = current_chainlink_twap()" in source)


async def t_rtds_subscription_matches_the_documented_contract():
    sent = []

    class FakeWS:
        async def send(self, payload):
            sent.append(payload)

    class FakeContext:
        async def __aenter__(self):
            return FakeWS()
        async def __aexit__(self, *_a):
            return False

    original = strike_mod.websockets.connect
    strike_mod.websockets.connect = lambda *_a, **_kw: FakeContext()
    try:
        service = ChainlinkStrike()
        service._stop.set()  # send the subscription, then leave immediately
        await service._session()
    finally:
        strike_mod.websockets.connect = original

    frame = json.loads(sent[0])
    check("RTDS heartbeat interval is five seconds", PING_EVERY == 5.0, str(PING_EVERY))
    check("one canonical Chainlink subscription is sent",
          len(frame["subscriptions"]) == 1, str(frame))
    sub = frame["subscriptions"][0]
    # The market's resolution text names the 60-second stream as the
    # settlement source, so subscribing to the 30-second one models a
    # different market.
    check("the settlement TWAP topic and update type match RTDS",
          sub["topic"] == "crypto_prices_twap_sixty" and sub["type"] == "update",
          str(sub))
    check("Chainlink symbol is encoded in filters",
          json.loads(sub["filters"]) == {"symbol": "btc/usd"}, str(sub))


async def t_rtds_close_reports_code_and_reason_not_a_generic_message():
    """A dead RTDS socket must say WHY, not just that something broke.

    The custom send-side heartbeat this module replaced (see
    t_rtds_subscription_matches_the_documented_contract for the protocol-
    ping rewrite) used to log a distinct "heartbeat send/close failed"
    message on a half-open socket. The protocol-level ping/pong keepalive
    that replaced it detects the same class of dead connection, but nothing
    reported WHY the connection ended beyond whatever generic message
    _run()'s outer `except Exception` produced. This checks that a
    ConnectionClosed's code/reason reach `last_error`, and that _run()'s
    generic handler does not clobber that specific message with a less
    informative one.
    """
    from websockets.exceptions import ConnectionClosed
    from websockets.frames import Close

    class FakeWS:
        async def send(self, _payload):
            pass

        async def recv(self):
            raise ConnectionClosed(Close(1011, "keepalive ping timed out"), None)

    class FakeContext:
        async def __aenter__(self):
            return FakeWS()

        async def __aexit__(self, *_a):
            return False

    original = strike_mod.websockets.connect
    strike_mod.websockets.connect = lambda *_a, **_kw: FakeContext()
    try:
        direct = ChainlinkStrike()
        try:
            await direct._session()
            check("_session raises ConnectionClosed rather than swallowing it", False)
        except ConnectionClosed:
            pass
        check("the close code and reason reach last_error",
              "code=1011" in (direct.last_error or "")
              and "keepalive ping timed out" in (direct.last_error or ""),
              str(direct.last_error))

        via_run = ChainlinkStrike()
        task = asyncio.ensure_future(via_run._run())
        try:
            await asyncio.sleep(0.05)  # let the first failed attempt run
            check("_run()'s generic handler keeps the specific diagnostic",
                  "code=1011" in (via_run.last_error or "")
                  and "keepalive ping timed out" in (via_run.last_error or ""),
                  str(via_run.last_error))
        finally:
            via_run._stop.set()
            await asyncio.wait_for(task, timeout=2.0)
    finally:
        strike_mod.websockets.connect = original


def t_live_venue_minimum_sizing_matches_paper():
    """paper_trade and polymarket_trade must size identically off one walk.

    Both used to hand-implement the same "walk the ask ladder up to the
    venue minimum" logic independently, with polymarket_trade's docstring
    admitting it only "mirrors" the paper copy rather than sharing it - a
    correction to one could silently not apply to the other. Both now
    delegate to orderbook.venue_minimum_stake; this checks polymarket_trade
    against the exact thin-book scenario tests_paper.py locks in for the
    paper path (a 3-share top level, the rest at the next tick), so a future
    change to the shared walk that breaks parity fails here too.
    """
    import polymarket_trade

    asks = [{"price": "0.50", "size": "3"}, {"price": "0.51", "size": "10"}]
    sized = polymarket_trade._size_to_venue_minimum(
        2.50, asks, {"minimum": "5"}, 0.90)
    # Cross-checked against paper_trade.size_to_venue_minimum's own published
    # result for the identical book (tests_paper.py t_paper_sizes_up_when_
    # top_of_book_cannot_fill_the_minimum: $2.50 -> $2.52).
    check("live sizing raises exactly to the venue minimum notional",
          abs(sized - 2.52) < 1e-9, str(sized))

    # An unreadable market must never silently enlarge a live order.
    check("malformed rules leave the amount unchanged",
          polymarket_trade._size_to_venue_minimum(
              2.50, asks, {"minimum": "not-a-number"}, 0.90) == 2.50)
    check("no asks leaves the amount unchanged",
          polymarket_trade._size_to_venue_minimum(
              2.50, [], {"minimum": "5"}, 0.90) == 2.50)


def t_market_tokens_are_mapped_by_outcome_and_tradeability():
    event = {
        "slug": "btc-updown-5m-12300",
        "closed": False,
        "active": True,
        "markets": [
            {"closed": True, "active": True, "acceptingOrders": True,
             "outcomes": ["Up", "Down"], "clobTokenIds": ["closed-up", "closed-down"]},
            {"id": "12300", "closed": False, "active": True,
             "acceptingOrders": True, "enableOrderBook": True,
             "eventStartTime": "1970-01-01T03:25:00Z",
             "endDate": "1970-01-01T03:30:00Z",
             "cryptoMarketConfig": {
                 "asset": "btc", "duration": "5m", "twapEnabled": True,
                 "twapLookbackSeconds": 60,
             },
             "conditionId": "0x" + "a" * 64,
             "outcomes": json.dumps(["Down", "Up"]),
             "clobTokenIds": json.dumps(["202", "101"])},
        ],
    }
    parsed = market_discovery._parse_event(event, 12300)
    check("closed market is skipped",
          parsed["condition_id"] == "0x" + "a" * 64, str(parsed))
    check("UP token follows its label, not index zero",
          parsed["up_token_id"] == "101", str(parsed))
    check("DOWN token follows its label",
          parsed["down_token_id"] == "202", str(parsed))

    event["markets"][1]["acceptingOrders"] = False
    check("market refusing orders is rejected",
          market_discovery._parse_event(event, 12300) is None)


def t_market_discovery_requires_the_declared_60s_twap_contract():
    market = {
        "id": "12300", "closed": False, "active": True,
        "acceptingOrders": True, "enableOrderBook": True,
        "eventStartTime": "1970-01-01T03:25:00Z",
        "endDate": "1970-01-01T03:30:00Z",
        "conditionId": "0x" + "a" * 64,
        "outcomes": ["Up", "Down"], "clobTokenIds": ["101", "202"],
        "cryptoMarketConfig": {
            "asset": "btc", "duration": "5m", "twapEnabled": True,
            "twapLookbackSeconds": 60,
        },
    }
    event = {"slug": "btc-updown-5m-12300", "closed": False,
             "active": True, "markets": [market]}
    check("the current BTC/5m/60s TWAP contract is accepted",
          market_discovery._parse_event(event, 12300) is not None)

    valid = dict(market["cryptoMarketConfig"])
    cases = (
        ("missing crypto config", None),
        ("malformed crypto config", "not-an-object"),
        ("30-second TWAP", {**valid, "twapLookbackSeconds": 30}),
        ("spot market", {**valid, "twapEnabled": False}),
        ("wrong asset", {**valid, "asset": "eth"}),
        ("wrong duration", {**valid, "duration": "15m"}),
        ("string lookback", {**valid, "twapLookbackSeconds": "60"}),
        ("boolean lookback", {**valid, "twapLookbackSeconds": True}),
    )
    for name, config in cases:
        if config is None:
            market.pop("cryptoMarketConfig", None)
        else:
            market["cryptoMarketConfig"] = config
        check(f"{name} is rejected before token discovery",
              market_discovery._parse_event(event, 12300) is None,
              str(config))
    market["cryptoMarketConfig"] = valid


def t_market_discovery_never_falls_back_to_a_closed_round():
    calls = []
    current = market_discovery._current_5m_window_start_unix()
    original = market_discovery._fetch_slug
    market_discovery._fetch_slug = lambda slug: calls.append(slug) or None
    try:
        check("missing current round returns no tokens",
              market_discovery.get_btc_5m_tokens(current) is None)
        check("expired previous round is rejected before any API lookup",
              market_discovery.get_btc_5m_tokens(current - 300) is None)
    finally:
        market_discovery._fetch_slug = original
    check("only the requested round was queried",
          calls == [f"btc-updown-5m-{current}"], str(calls))


def t_order_failures_read_the_live_error_value():
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    check("main bot reads the module's current order error",
          "polymarket_trade.last_order_error" in source)
    check("main bot no longer imports a stale immutable error value",
          "get_balance_allowance, last_order_error" not in source)


def t_paper_startup_does_not_abort_on_measured_clock_drift():
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    check("live mode still fail-closes on unverified clock",
          'if mode == "LIVE":' in source
          and "CLOB clock synchronization could not be verified" in source)
    check("paper mode keeps Unix round windows when CLOB drift is large",
          "PAPER continues" in source
          and "Unix 5-minute windows" in source)
    check("strategy samples Unix time for round identity",
          "sampled_wall = timer.unix()" in source)


class _ClockResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def t_clock_offset_aligns_round_identity_to_clob():
    import timer
    timer.reset_clock_cache()
    server_ahead = 2.363
    orig = timer.http_pool.get

    def fake_get(*_a, **_k):
        return _ClockResp(time.time() + server_ahead)

    timer.http_pool.get = fake_get
    try:
        ok, detail, drift = timer.check_clock("https://clob.example", 2.0, cache_s=0)
        check("measured drift beyond 2s is not within live tolerance",
              ok is False, detail)
        check("local-behind server produces negative drift",
              drift is not None and drift < -2.0, str(drift))
        check("offset is stored for CLOB-aligned wall()",
              timer.clock_measured() and abs(timer.clock_offset() - drift) < 1e-9)
        residual = timer.wall() - (time.time() + server_ahead)
        check("wall() tracks CLOB time within a few hundred ms",
              abs(residual) < 0.25, f"{residual:.4f}s")
        check("window_start ignores CLOB offset and stays on Unix",
              timer.window_start() == timer.window_start(time.time()))
        explicit = 1_786_320_017.4
        check("explicit timestamps are not shifted",
              timer.window_start(explicit) == 1_786_320_000)
        check("seconds_left uses the supplied sample",
              timer.seconds_left(explicit) == 283)
    finally:
        timer.http_pool.get = orig
        timer.reset_clock_cache()
    check("reset clears the measured offset",
          not timer.clock_measured() and timer.clock_offset() == 0.0)


def t_binance_and_fresh_snapshot_follow_clob_time():
    import json
    import price_ws
    import timer
    from feeds.binance import BinanceTradeFeed

    timer.reset_clock_cache()
    orig = timer.http_pool.get
    orig_price = price_ws.latest_price
    orig_mono = price_ws.latest_price_mono
    orig_ts = price_ws.latest_price_ts_ms
    orig_id = price_ws.latest_trade_id
    timer.http_pool.get = lambda *_a, **_k: _ClockResp(time.time() + 3.5)
    try:
        timer.check_clock("https://clob.example", 2.0, cache_s=0)
        trade_ms = int(time.time() * 1000) + 3500
        feed = BinanceTradeFeed()
        feed._handle(json.dumps({"e": "trade", "p": "64123.50", "T": trade_ms}))
        check("binance keeps a print that is future-dated on the local clock",
              feed.price == 64123.50, str(feed.health.detail))
        published = price_ws.publish_price(64123.50, exchange_ts_ms=trade_ms)
        check("price bus accepts the CLOB-aligned print", published is True)
        fresh, ts = price_ws.fresh_snapshot(3.0)
        check("fresh_snapshot uses CLOB time, not the lagging local clock",
              fresh == 64123.50 and ts == trade_ms, str((fresh, ts)))
        price_ws.latest_price = None
        price_ws.latest_price_mono = None
        price_ws.latest_price_ts_ms = None
        price_ws.latest_trade_id = None
        past_feed = BinanceTradeFeed()
        past_ms = int(timer.wall() * 1000) - 3200
        past_feed._handle(json.dumps({"e": "trade", "p": "64124.00", "T": past_ms, "t": 2}))
        check("binance keeps a print a few seconds behind CLOB time",
              past_feed.price == 64124.00, str(past_feed.health.detail))
        published_past = price_ws.publish_price(64124.00, exchange_ts_ms=past_ms, trade_id=2)
        check("price bus accepts a cross-venue-aged print", published_past is True)
        fresh_past, ts_past = price_ws.fresh_snapshot(3.0)
        check("fresh_snapshot does not treat CLOB/Binance skew as local staleness",
              fresh_past == 64124.00 and ts_past == past_ms, str((fresh_past, ts_past)))
    finally:
        timer.http_pool.get = orig
        timer.reset_clock_cache()
        price_ws.latest_price = orig_price
        price_ws.latest_price_mono = orig_mono
        price_ws.latest_price_ts_ms = orig_ts
        price_ws.latest_trade_id = orig_id


def t_clock_check_failure_does_not_clear_last_offset():
    import timer
    timer.reset_clock_cache()
    orig = timer.http_pool.get
    timer.http_pool.get = lambda *_a, **_k: _ClockResp(time.time() + 0.05)
    try:
        ok, _detail, drift = timer.check_clock("https://clob.example", 2.0, cache_s=0)
        check("small drift is within tolerance", ok is True, str(drift))
        stored = timer.clock_offset()
        timer.http_pool.get = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("down"))
        ok2, detail2, drift2 = timer.check_clock("https://clob.example", 2.0, cache_s=0)
        check("network failure is fail-closed", ok2 is False and drift2 is None, detail2)
        check("last good offset is kept after a failed refresh",
              timer.clock_measured() and abs(timer.clock_offset() - stored) < 1e-9)
    finally:
        timer.http_pool.get = orig
        timer.reset_clock_cache()


def t_clock_cache_is_invalidated_by_a_local_wall_clock_jump():
    import timer

    timer.reset_clock_cache()
    original = timer.http_pool.get
    calls = []

    def fake_get(*_a, **_k):
        calls.append(True)
        return _ClockResp(time.time())

    timer.http_pool.get = fake_get
    try:
        timer.check_clock("https://clob.example", 2.0, cache_s=30)
        timer.check_clock("https://clob.example", 2.0, cache_s=30)
        check("stable local clock uses the cached CLOB measurement",
              len(calls) == 1, str(calls))
        # Model a ten-second wall-clock adjustment without waiting in real
        # time.  Monotonic elapsed time has not moved with it.
        timer._clock_sample_wall -= 10.0
        timer.check_clock("https://clob.example", 2.0, cache_s=30)
        check("wall-clock jump forces an immediate CLOB recheck",
              len(calls) == 2, str(calls))
    finally:
        timer.http_pool.get = original
        timer.reset_clock_cache()


# ------------------------------------------------------ one-sided books ---
# In the last minute of a five-minute round the winning token keeps only bids
# and the losing token only asks.  The book vote must abstain there instead of
# naming the token nobody is offering, and the loop must find that side
# unbuyable before it pays for a submission that cannot fill.
def t_one_sided_book_casts_no_vote():
    import orderbook

    check("two-sided book still compares depth",
          orderbook.liquidity_signal([{"price": "0.4", "size": "10"}],
                                     [{"price": "0.6", "size": "20"}]) == "DOWN")
    check("winning token (bids only) casts no vote",
          orderbook.liquidity_signal([{"price": "0.99", "size": "12783"}], []) is None)
    check("losing token (asks only) casts no vote",
          orderbook.liquidity_signal([], [{"price": "0.01", "size": "12796"}]) is None)
    check("empty book still returns None", orderbook.liquidity_signal([], []) is None)
    check("zero-size levels count as an absent side",
          orderbook.liquidity_signal([{"price": "0.99", "size": "12783"}],
                                     [{"price": "0.5", "size": "0"}]) is None)


def t_unbuyable_side_is_refused_before_submission():
    import orderbook

    original = orderbook.get_orderbook

    def gate(bids, asks, min_price=0.0):
        """Return the refusal reason for this book shape, or None if buyable."""
        orderbook.get_orderbook = lambda *_a, **_kw: (bids, asks)
        try:
            orderbook.validate_buy_liquidity("1", 5.0, 0.99, 0.25, min_price=min_price)
        except ValueError as exc:
            return str(exc)
        return None

    try:
        check("winning side with no offers is refused",
              gate([{"price": "0.99", "size": "12783"}], []) == "selected token has no asks")
        check("losing side with no bid is refused",
              "no bids" in (gate([], [{"price": "0.01", "size": "12796"}]) or ""))
        check("an ask above MAX_BUY_PRICE is refused",
              "exceeds MAX_BUY_PRICE" in (gate([{"price": "0.99", "size": "5"}],
                                               [{"price": "0.995", "size": "5"}]) or ""))
        check("an ask below MIN_BUY_PRICE is refused",
              "below MIN_BUY_PRICE" in (gate([{"price": "0.19", "size": "100"}],
                                             [{"price": "0.08", "size": "100"}],
                                             min_price=0.20) or ""))
        check("depth below the bet size is refused",
              "only $" in (gate([{"price": "0.40", "size": "100"}],
                                [{"price": "0.50", "size": "2"}]) or ""))
        check("a two-sided book with real depth passes",
              gate([{"price": "0.49", "size": "100"}],
                   [{"price": "0.50", "size": "100"}]) is None)
        check("a book inside the 0.20-0.90 band passes the min floor",
              gate([{"price": "0.49", "size": "100"}],
                   [{"price": "0.50", "size": "100"}], min_price=0.20) is None)
    finally:
        orderbook.get_orderbook = original


def t_http_pool_split_timeout_respects_a_caller_larger_than_the_floor():
    """A caller's explicit timeout must not be silently truncated at connect.

    _split_timeout used to return (min(HTTP_CONNECT_TIMEOUT_SECONDS, read),
    read) - so orderbook.py's timeout=8.0 and market_discovery.py's
    timeout=10 both got their connect phase hard-capped at the configured
    floor (4.0 by default) no matter what they asked for, even though the
    module's own warm() docstring measures real connect times up to 15.8s.
    """
    import config
    import http_pool

    floor = config.HTTP_CONNECT_TIMEOUT_SECONDS
    check("a timeout under the floor is left untouched on both phases",
          http_pool._split_timeout(floor / 2.0) == (floor / 2.0, floor / 2.0))
    check("a timeout exactly at the floor is left untouched",
          http_pool._split_timeout(floor) == (floor, floor))

    connect, read = http_pool._split_timeout(floor * 2.0)
    check("a caller asking for double the floor gets at least the floor on connect",
          connect >= floor, str(connect))
    check("connect+read stays close to what the caller asked for",
          abs((connect + read) - floor * 2.0) < 1e-9, str((connect, read)))

    connect, read = http_pool._split_timeout(10.0)
    check("a much larger caller timeout is never truncated back to the floor",
          connect > floor, str(connect))
    check("read never collapses to zero for a large caller timeout",
          read > 0, str(read))

    check("an explicit (connect, read) pair passes through untouched",
          http_pool._split_timeout((1.5, 9.0)) == (1.5, 9.0))
    check("no timeout at all still uses the configured connect floor",
          http_pool._split_timeout(None) == (floor, None))


def t_orderbook_rejects_nonfinite_controls_before_network_io():
    import orderbook

    original = orderbook.get_orderbook
    calls = []
    orderbook.get_orderbook = lambda *_a, **_k: calls.append(True) or ([], [])
    try:
        for name, kwargs in (
                ("amount", {"amount": float("nan")}),
                ("max price", {"max_price": float("inf")}),
                ("spread", {"max_spread": float("nan")}),
                ("minimum", {"min_price": -0.1})):
            args = {"token_id": "1", "amount": 5.0, "max_price": 0.9,
                    "max_spread": 0.25, "min_price": 0.2}
            args.update(kwargs)
            try:
                orderbook.validate_buy_liquidity(**args)
            except ValueError:
                check(f"invalid {name} is rejected", True)
            else:
                check(f"invalid {name} is rejected", False)
    finally:
        orderbook.get_orderbook = original
    check("invalid controls are rejected before a CLOB request", not calls, str(calls))

    now = time.time()
    data = {"asset_id": "1", "timestamp": str(int(now * 1000)),
            "bids": [{"price": "0.4", "size": "1"}], "asks": []}
    for label, kwargs in (
            ("nonfinite now", {"now": float("nan")}),
            ("nonfinite age limit", {"now": now, "max_age_s": float("nan")})):
        try:
            orderbook.parse_orderbook(data, "1", **kwargs)
        except ValueError:
            check(f"{label} cannot bypass freshness validation", True)
        else:
            check(f"{label} cannot bypass freshness validation", False)


def t_orderbook_retries_one_transient_read_then_validates_identity():
    import orderbook
    import timer

    class Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"asset_id": "123", "timestamp": str(int(timer.wall() * 1000)),
                    "bids": [{"price": "0.4", "size": "2"}],
                    "asks": [{"price": "0.5", "size": "2"}]}

    calls = []
    # orderbook now reads through the pooled session, so the stub goes on
    # http_pool.get rather than requests.get.
    original_get = orderbook.http_pool.get
    original_sleep = orderbook.time.sleep
    try:
        def fake_get(*_a, **_k):
            calls.append(True)
            if len(calls) == 1:
                raise orderbook.requests.Timeout("temporary")
            return Resp()

        orderbook.http_pool.get = fake_get
        orderbook.time.sleep = lambda *_a: None
        bids, asks = orderbook.get_orderbook("123")
        check("one transient CLOB book failure is retried", len(calls) == 2, str(calls))
        check("retried CLOB book is still strictly parsed",
              bids[0]["price"] == "0.4" and asks[0]["price"] == "0.5")
    finally:
        orderbook.http_pool.get = original_get
        orderbook.time.sleep = original_sleep


def t_market_discovery_backs_off_and_retries_rate_limits():
    class Resp:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"Retry-After": "0.01"}

        def raise_for_status(self):
            if self.status_code >= 400:
                exc = market_discovery.requests.HTTPError("rate limited")
                exc.response = self
                raise exc

        def json(self):
            return {"slug": "btc-updown-5m-12300"}

    calls, sleeps = [], []
    # market_discovery reads through the pooled session now.
    original_get = market_discovery.http_pool.get
    original_sleep = market_discovery.time.sleep
    try:
        market_discovery.http_pool.get = lambda *_a, **_k: (
            calls.append(True) or Resp(429 if len(calls) == 1 else 200))
        market_discovery.time.sleep = lambda delay: sleeps.append(delay)
        event = market_discovery._fetch_slug("btc-updown-5m-12300")
        check("Gamma 429 is retried exactly once", len(calls) == 2, str(calls))
        check("Gamma rate-limit retry uses bounded backoff",
              len(sleeps) == 1 and 0.05 <= sleeps[0] <= 1.0, str(sleeps))
        check("Gamma retry returns only the exact requested slug",
              event and event["slug"] == "btc-updown-5m-12300", str(event))
    finally:
        market_discovery.http_pool.get = original_get
        market_discovery.time.sleep = original_sleep

    check("overflowing market windows fail closed",
          market_discovery.get_btc_5m_tokens(float("inf")) is None)


async def t_transient_unfillable_book_is_retried_next_attempt():
    """A temporary empty/spread book must not blacklist a side for 5 minutes."""
    import main_bot

    active = 1_786_320_000
    calls = {"probe": 0, "orders": 0}
    rows = []

    class Strike:
        def strike_for(self, _window):
            return Decimal("100")

        def current_value(self):
            return Decimal("100")

        def divergence(self, *_a):
            return {"diff": None}

    bids = [{"price": "0.49", "size": "20"}]
    asks = [{"price": "0.50", "size": "10"}]

    def probe(*_a, **_k):
        calls["probe"] += 1
        if calls["probe"] == 1:
            raise ValueError("temporary empty ask side")
        return bids, asks

    def submit(*_a, **_k):
        calls["orders"] += 1
        main_bot.stop_event.set()
        return True

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    try:
        replace(main_bot, "execution_mode", "PAPER")
        replace(main_bot, "_paper_broker", object())
        replace(main_bot, "_round_exposure_provider", None)
        replace(main_bot, "_round_held_tokens_provider", None)
        replace(main_bot, "_execution_ready_provider", None)
        replace(main_bot, "_strike", Strike())
        replace(main_bot, "get_balance_allowance",
                lambda: {"balance": 1000.0, "allowance": 1000.0})
        replace(main_bot, "place_trade", submit)
        replace(main_bot, "_append_trade", lambda row: rows.append(row))
        replace(main_bot.polymarket_trade, "live_execution_disabled", lambda: True)
        replace(main_bot.market_discovery, "get_tokens_for_current_round", lambda _w: {
            "window_start": active, "window_end": active + 300,
            "up_token_id": "11", "down_token_id": "12",
            "orderbook_token_id": "11", "condition_id": "0x" + "a" * 64,
        })
        replace(main_bot.orderbook, "get_orderbook", lambda *_a, **_k: (bids, asks))
        replace(main_bot.orderbook, "validate_buy_liquidity", probe)
        # Stamp the print inside the opening 5s so the boundary strike latches
        # legitimately. It used to sit at +100s and rely on the PAPER
        # mid-round fallback, which no longer exists.
        replace(main_bot.price_ws, "latest_snapshot",
                lambda: (100.0, time.monotonic(), (active + 1) * 1000))
        replace(main_bot.price_ws, "fresh_snapshot",
                lambda *_a, **_k: (101.0, (active + 100) * 1000))
        replace(main_bot.timer, "unix", lambda *_a, **_k: active + 100.0)
        replace(main_bot.timer, "wall", lambda *_a, **_k: active + 100.0)
        replace(main_bot.timer, "check_clock",
                lambda *_a, **_k: (True, "clock synchronized", 0.0))
        replace(main_bot.config, "TRADE_INTERVAL_SECONDS", 0.01)
        replace(main_bot.config, "TRADE_LAST_SECONDS", 300)
        replace(main_bot.config, "MAX_ROUND_EXPOSURE", 100.0)
        replace(main_bot.config, "CANCEL_OPEN_BEFORE_TRADE", False)
        # This case is about the signal path, so pin the phases: phase 1 owns
        # the same seconds and would otherwise answer first.
        replace(main_bot.config, "PHASE1_ENABLED", False)
        replace(main_bot.config, "PHASE2_ENABLED", True)
        main_bot.stop_event.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            await asyncio.wait_for(main_bot.run_bot(), timeout=1.5)
    finally:
        main_bot.stop_event.set()
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        main_bot.stop_event.clear()

    check("transient unfillable side is probed again", calls["probe"] == 2, str(calls))
    check("replenished book can submit on the next attempt", calls["orders"] == 1,
          str(calls))
    check("the failed attempt remains visible in the journal",
          rows and rows[0]["result"] == "skipped_unfillable", str(rows))


async def _drive_phase1(books, *, remaining=200.0, timeout=1.5,
                        bands=((300, 120, 0.25, 0.50, 12.0),),
                        held_provider=None, exposure_provider=None,
                        execution_mode="PAPER", execution_ready_provider=None,
                        price_samples=None, skip_joined_round=False):
    """Run one phase-1 pass against a fixed pair of books. Returns what it did."""
    import main_bot

    active = 1_786_320_000
    seen = {"orders": [], "rows": [], "price_samples": 0,
            "executor_guards": []}
    samples = list(price_samples if price_samples is not None else (99.0, 99.0, 99.0))

    def fresh_price(*_a, **_k):
        index = min(seen["price_samples"], len(samples) - 1)
        seen["price_samples"] += 1
        value = samples[index]
        return ((None, None) if value is None else
                (float(value), (active + 100) * 1000))

    class Strike:
        def strike_for(self, _window):
            return Decimal("100")

        def current_value(self):
            return Decimal("100")

        def divergence(self, *_a):
            return {"diff": None}

    def submit(side, *args, **kwargs):
        guard = kwargs.get("pre_submit_guard")
        allowed = guard() if callable(guard) else None
        seen["executor_guards"].append(allowed)
        if allowed is not True:
            main_bot.stop_event.set()
            return False
        seen["orders"].append(side)
        # the band's ceiling travels with the order as its price cap
        seen["caps"] = seen.get("caps", [])
        seen["caps"].append(kwargs.get("max_price", args[-1] if args else None))
        # the band's floor travels with it too, or the walk can fill below it
        seen["floors"] = seen.get("floors", [])
        seen["floors"].append(kwargs.get("min_price"))
        main_bot.stop_event.set()
        return True

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    try:
        is_paper = execution_mode == "PAPER"
        replace(main_bot, "execution_mode", execution_mode)
        replace(main_bot, "_paper_broker", object() if is_paper else None)
        replace(main_bot, "_round_exposure_provider", exposure_provider)
        replace(main_bot, "_round_held_tokens_provider", held_provider)
        replace(main_bot, "_execution_ready_provider", execution_ready_provider)
        replace(main_bot, "_strike", Strike())
        replace(main_bot, "get_balance_allowance",
                lambda: {"balance": 1000.0, "allowance": 1000.0})
        replace(main_bot, "place_trade", submit)
        replace(main_bot, "_append_trade", lambda row: seen["rows"].append(row))
        replace(main_bot.polymarket_trade, "live_execution_disabled", lambda: is_paper)
        replace(main_bot.market_discovery, "get_tokens_for_current_round", lambda _w: {
            "window_start": active, "window_end": active + 300,
            "up_token_id": "11", "down_token_id": "12",
            "orderbook_token_id": "11", "condition_id": "0x" + "a" * 64,
        })
        replace(main_bot.orderbook, "get_orderbook",
                lambda token, *_a, **_k: books[str(token)])
        replace(main_bot.orderbook, "validate_buy_liquidity",
                lambda *_a, **_k: books["11"])
        replace(main_bot.price_ws, "latest_snapshot",
                lambda: (100.0, time.monotonic(), (active + 1) * 1000))
        replace(main_bot.price_ws, "fresh_snapshot", fresh_price)
        replace(main_bot.timer, "unix", lambda *_a, **_k: active + (300.0 - remaining))
        replace(main_bot.timer, "wall", lambda *_a, **_k: active + (300.0 - remaining))
        replace(main_bot.timer, "check_clock",
                lambda *_a, **_k: (True, "clock synchronized", 0.0))
        replace(main_bot.config, "PHASE1_ENABLED", True)
        replace(main_bot.config, "PHASE2_ENABLED", False)
        replace(main_bot.config, "PHASE1_INTERVAL_SECONDS", 0.01)
        replace(main_bot.config, "PHASE1_BANDS", tuple(bands))
        replace(main_bot.config, "BET_SIZE", 2.50)
        replace(main_bot.config, "MAX_ROUND_EXPOSURE", 100.0)
        replace(main_bot.config, "CANCEL_OPEN_BEFORE_TRADE", False)
        replace(main_bot.config, "SKIP_JOINED_ROUND", skip_joined_round)
        main_bot.stop_event.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(main_bot.run_bot(), timeout=timeout)
    finally:
        main_bot.stop_event.set()
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        main_bot.stop_event.clear()
    return seen


def _book(ask):
    return ([{"price": f"{ask - 0.01:.2f}", "size": "40"}],
            [{"price": f"{ask:.2f}", "size": "40"}])


async def _drive_phase2_with_hold(*, execution_mode, held_provider,
                                  execution_ready_provider=None,
                                  timeout=0.55, price_votes=None,
                                  book_vote="DOWN", chainlink_vote="DOWN",
                                  minority_rule=False,
                                  diagnostic_side="DOWN",
                                  stop_after_vote=None,
                                  stop_after_orders=1,
                                  allow_signal_flips=False,
                                  taper_hedge_enabled=False, token_books=None,
                                  liquidity_probe=None, primary_band=None,
                                  decision_rule=None, unsettled_provider=None,
                                  max_unsettled=0.0, require_unanimity=False,
                                  taper_advance=0, primary_slots=None,
                                  primary_ladder=None, hedge_ladder=None):
    """Drive a forced DOWN signal through phase 2 after a restart."""
    import main_bot

    active = 1_786_320_000
    seen = {"orders": 0, "probes": 0, "price_votes": 0, "order_sides": [],
            "order_amounts": [], "executor_guards": [],
            # (max_price, min_price) actually handed to each submission and
            # to each liquidity probe - the band that order may fill in.
            "order_bands": [], "probe_bands": []}
    votes = list(price_votes or ())
    bids, asks = _book(0.50)

    class Strike:
        def strike_for(self, _window):
            return Decimal("100")

        def current_value(self):
            return Decimal("101")

        def divergence(self, *_a):
            return {"diff": None}

    def submit(side, *_args, **kwargs):
        guard = kwargs.get("pre_submit_guard")
        allowed = guard() if callable(guard) else None
        seen["executor_guards"].append(allowed)
        if allowed is not True:
            main_bot.stop_event.set()
            return False
        seen["orders"] += 1
        seen["order_sides"].append(side)
        seen["order_amounts"].append(_args[0] if _args else None)
        # place_trade(side, amount, up, down, condition, window_end,
        #             max_price, min_price, *, pre_submit_guard)
        seen["order_bands"].append(
            (_args[5] if len(_args) > 5 else kwargs.get("max_price"),
             _args[6] if len(_args) > 6 else kwargs.get("min_price")))
        if (stop_after_orders is not None
                and seen["orders"] >= stop_after_orders):
            main_bot.stop_event.set()
        return True

    def tagged_price_signal(_round, _start, _current):
        index = min(seen["price_votes"], len(votes) - 1)
        seen["price_votes"] += 1
        vote = votes[index]
        if stop_after_vote is not None and seen["price_votes"] >= stop_after_vote:
            main_bot.stop_event.set()
        return vote

    def probe(*_args, **_kwargs):
        seen["probes"] += 1
        # validate_buy_liquidity(token, amount, max_price, spread, min_price=)
        seen["probe_bands"].append(
            (_args[2] if len(_args) > 2 else _kwargs.get("max_price"),
             _kwargs.get("min_price")))
        return bids, asks

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    try:
        is_paper = execution_mode == "PAPER"
        replace(main_bot, "execution_mode", execution_mode)
        replace(main_bot, "_paper_broker", object() if is_paper else None)
        replace(main_bot, "_round_exposure_provider",
                lambda _window, _condition: 0.0)
        replace(main_bot, "_round_held_tokens_provider", held_provider)
        replace(main_bot, "_execution_ready_provider",
                execution_ready_provider or (lambda _condition: True))
        replace(main_bot, "_strike", Strike())
        replace(main_bot, "get_balance_allowance",
                lambda: {"balance": 1000.0, "allowance": 1000.0})
        replace(main_bot, "place_trade", submit)
        replace(main_bot, "_append_trade", lambda _row: None)
        replace(main_bot.polymarket_trade, "live_execution_disabled", lambda: is_paper)
        replace(main_bot.market_discovery, "get_tokens_for_current_round", lambda _w: {
            "window_start": active, "window_end": active + 300,
            "up_token_id": "11", "down_token_id": "12",
            "orderbook_token_id": "11", "condition_id": "0x" + "a" * 64,
        })
        replace(main_bot.orderbook, "get_orderbook",
                lambda token, *_a, **_k: token_books[str(token)] if token_books else (bids, asks))
        if token_books is None:
            replace(main_bot.orderbook, "liquidity_signal",
                    lambda *_a, **_k: book_vote)
        replace(main_bot.orderbook, "validate_buy_liquidity",
                liquidity_probe if liquidity_probe is not None else probe)
        replace(main_bot.strategy, "decide", lambda *_a, **_k: "DOWN")
        replace(main_bot.strategy, "final_decision",
                lambda *_a, **_k: diagnostic_side)
        replace(main_bot, "chainlink_signal",
                lambda *_a, **_k: chainlink_vote)
        if votes:
            replace(main_bot, "price_signal", tagged_price_signal)
        replace(main_bot.price_ws, "latest_snapshot",
                lambda: (100.0, time.monotonic(), (active + 1) * 1000))
        replace(main_bot.price_ws, "fresh_snapshot",
                lambda *_a, **_k: (101.0, (active + 100) * 1000))
        replace(main_bot.timer, "unix", lambda *_a, **_k: active + 100.0)
        replace(main_bot.timer, "wall", lambda *_a, **_k: active + 100.0)
        replace(main_bot.timer, "check_clock",
                lambda *_a, **_k: (True, "clock synchronized", 0.0))
        replace(main_bot.config, "PHASE1_ENABLED", False)
        replace(main_bot.config, "PHASE2_ENABLED", True)
        replace(main_bot.config, "TRADE_INTERVAL_SECONDS", 0.01)
        replace(main_bot.config, "TRADE_LAST_SECONDS", 300)
        replace(main_bot.config, "MAX_ROUND_EXPOSURE", 100.0)
        replace(main_bot.config, "CANCEL_OPEN_BEFORE_TRADE", False)
        replace(main_bot.config, "PAPER_ALLOW_SIGNAL_FLIPS", allow_signal_flips)
        replace(main_bot.config, "SIGNAL_MINORITY_RULE", minority_rule)
        replace(main_bot.config, "SIGNAL_DECISION_RULE",
                decision_rule or ("minority" if minority_rule else "price"))
        replace(main_bot.config, "MAX_UNSETTLED_EXPOSURE", max_unsettled)
        replace(main_bot.config, "REQUIRE_SIGNAL_UNANIMITY", require_unanimity)
        replace(main_bot.config, "TAPER_ADVANCE_AFTER_SKIPS", taper_advance)
        if primary_slots is not None:
            replace(main_bot.config, "TAPER_PRIMARY_SLOTS", primary_slots)
        if primary_ladder is not None:
            replace(main_bot.config, "TAPER_PRIMARY_LADDER", primary_ladder)
        if hedge_ladder is not None:
            replace(main_bot.config, "TAPER_HEDGE_LADDER", hedge_ladder)
        replace(main_bot, "_unsettled_exposure_provider", unsettled_provider)
        replace(main_bot.config, "TAPER_HEDGE_ENABLED", taper_hedge_enabled)
        if primary_band is not None:
            replace(main_bot.config, "PRIMARY_ENTRY_MIN_PRICE", primary_band[0])
            replace(main_bot.config, "PRIMARY_ENTRY_MAX_PRICE", primary_band[1])
        main_bot.stop_event.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(main_bot.run_bot(), timeout=timeout)
    finally:
        main_bot.stop_event.set()
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        main_bot.stop_event.clear()
    return seen


async def t_phase1_honours_the_joined_round_switch():
    """SKIP_JOINED_ROUND must stop the BANDS too, not only phase 2.

    The guard used to sit between the two phases, so phase 1 had already
    `continue`d past it. The round is joined with no opening print, but
    BOUNDARY_BACKFILL_AFTER recovers that print from REST, and from then on
    the bands traded a round the operator asked the bot to sit out.
    """
    books = {"11": _book(0.60), "12": _book(0.40)}
    trading = await _drive_phase1(books, remaining=200.0, skip_joined_round=False)
    check("with the switch off, phase 1 still trades the joined round",
          trading["orders"] == ["DOWN"], str(trading["orders"]))
    skipped = await _drive_phase1(books, remaining=200.0, skip_joined_round=True)
    check("with the switch on, phase 1 sits the joined round out",
          skipped["orders"] == [], str(skipped["orders"]))


async def t_phase1_sends_both_ends_of_its_band_to_the_executor():
    """A band caps the walk at its top AND its bottom.

    Only the ceiling used to travel with the order. The floor lived in the
    pre-submit liquidity check alone, and the book moves between that check
    and the fill, so a leg quoted inside the band could fill below it - at a
    price the band experiment never meant to measure, in a book that had just
    moved against the side being bought.
    """
    seen = await _drive_phase1({"11": _book(0.60), "12": _book(0.40)},
                               bands=((300, 120, 0.25, 0.50, 12.0),))
    check("the order carries its band's ceiling", seen.get("caps") == [0.50],
          str(seen.get("caps")))
    check("the order carries its band's floor too", seen.get("floors") == [0.25],
          str(seen.get("floors")))


def t_executor_floor_may_only_rise_never_fall():
    """`min_price` mirrors `max_price`: it can tighten, never loosen."""
    import paper_trade
    from decimal import Decimal as D

    rules = paper_trade.MarketRules(
        "0x" + "a" * 64, D("0"), D("1"), min_order_size=D("5"),
        tick_size=D("0.01"), source="test", up_token_id="11", down_token_id="12")

    def book(ask):
        a = D(str(ask))
        return paper_trade.BookSnapshot(
            token_id="11", asks=((a, D("500")),), min_order_size=D("5"),
            tick_size=D("0.01"), timestamp=0, book_hash="h", received_wall=0.0,
            best_bid=a - D("0.01"), bids=((a - D("0.01"), D("500")),))

    def fills(ask, floor):
        try:
            spend = paper_trade.size_to_venue_minimum(
                D("2.50"), book(ask), rules, D("0.65"))
            paper_trade.estimate_fok(book(ask), spend, D("0.65"), rules,
                                     min_price=floor)
            return True
        except paper_trade.PaperRejected:
            return False

    account_floor = D("0.30")
    band_floor = max(account_floor, D("0.55"))
    check("an ask inside the band still fills", fills("0.60", band_floor))
    check("an ask below the band no longer fills", not fills("0.54", band_floor))
    check("a floor below the account minimum cannot loosen it",
          max(account_floor, D("0.10")) == account_floor
          and not fills("0.20", max(account_floor, D("0.10"))))


async def t_phase1_buys_whichever_leg_is_in_the_band():
    seen = await _drive_phase1({"11": _book(0.60), "12": _book(0.40)})
    check("phase 1 takes the leg inside the band, not the favourite",
          seen["orders"] == ["DOWN"], str(seen["orders"]))
    row = seen["rows"][0] if seen["rows"] else {}
    check("the fill is tagged phase1", row.get("phase") == "phase1", str(row))
    check("phase 1 records the fresh price signal that authorized the side",
          row.get("price_side") == "DOWN" and row.get("book_side") == ""
          and row.get("chainlink_side") == "", str(row))
    check("phase 1 supplies a passing executor-side SIG PRICE permit",
          seen["executor_guards"] == [True], str(seen))


async def t_phase1_price_signal_is_a_hard_order_gate():
    books = {"11": _book(0.60), "12": _book(0.40)}
    opposite = await _drive_phase1(
        books, timeout=0.55, price_samples=(101.0, 101.0, 101.0))
    check("phase 1 cannot buy opposite fresh SIG PRICE",
          opposite["orders"] == [], str(opposite))

    neutral = await _drive_phase1(
        books, timeout=0.55, price_samples=(100.0, 100.0, 100.0))
    check("phase 1 skips a neutral price signal",
          neutral["orders"] == [], str(neutral))

    stale = await _drive_phase1(
        books, timeout=0.55, price_samples=(99.0, None, None))
    check("phase 1 skips when SIG PRICE becomes stale during validation",
          stale["orders"] == [], str(stale))

    flipped = await _drive_phase1(
        books, timeout=0.55, price_samples=(99.0, 99.0, 101.0))
    check("phase 1 skips a last-moment SIG PRICE flip",
          flipped["orders"] == [] and flipped["price_samples"] >= 3,
          str(flipped))


async def t_restart_held_up_blocks_phase1_down_in_paper_and_live():
    condition = "0x" + "a" * 64
    books = {"11": _book(0.60), "12": _book(0.40)}
    paper_calls = []

    def paper_held(window, discovered_condition):
        paper_calls.append((window, discovered_condition))
        return {"11"} if discovered_condition == condition else set()

    paper = await _drive_phase1(
        books, timeout=0.55, held_provider=paper_held,
        exposure_provider=lambda _window, _condition: 0.0,
        execution_mode="PAPER")
    check("paper restart refreshes held UP after condition discovery",
          any(discovered == condition for _window, discovered in paper_calls),
          str(paper_calls))
    check("paper restart blocks phase-1 DOWN complement",
          paper["orders"] == [], str(paper["orders"]))

    live_calls = []

    def live_held(window, discovered_condition):
        live_calls.append((window, discovered_condition))
        return {"11"}

    live = await _drive_phase1(
        books, timeout=0.55, held_provider=live_held,
        exposure_provider=lambda _window, _condition: 0.0,
        execution_mode="LIVE",
        execution_ready_provider=lambda _condition: True)
    check("live restart restores accepted UP at round start",
          live_calls and live_calls[0][1] is None, str(live_calls))
    check("live restart blocks phase-1 DOWN complement",
          live["orders"] == [], str(live["orders"]))


async def t_restart_held_up_blocks_phase2_down_in_paper_and_live():
    condition = "0x" + "a" * 64
    paper_calls = []

    def paper_held(window, discovered_condition):
        paper_calls.append((window, discovered_condition))
        return {"11"} if discovered_condition == condition else set()

    paper = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=paper_held)
    check("paper phase 2 refreshes condition-backed held tokens",
          any(discovered == condition for _window, discovered in paper_calls),
          str(paper_calls))
    check("paper restart blocks phase-2 DOWN complement before liquidity/submit",
          paper["orders"] == 0 and paper["probes"] == 0, str(paper))

    live_calls = []

    def live_held(window, discovered_condition):
        live_calls.append((window, discovered_condition))
        return {"11"}

    live = await _drive_phase2_with_hold(
        execution_mode="LIVE", held_provider=live_held)
    check("live phase 2 restores window-backed held tokens at round start",
          live_calls and live_calls[0][1] is None, str(live_calls))
    check("live restart blocks phase-2 DOWN complement before liquidity/submit",
          live["orders"] == 0 and live["probes"] == 0, str(live))


async def t_phase2_price_signal_is_the_only_order_side_authority():
    matching = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("DOWN", "DOWN", "DOWN"), diagnostic_side="UP")
    check("diagnostic consensus cannot override fresh SIG PRICE",
          matching["order_sides"] == ["DOWN"] and matching["price_votes"] >= 4
          and matching["executor_guards"] == [True],
          str(matching))

    neutral = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=(None,), stop_after_vote=1)
    check("phase 2 skips a neutral initial SIG PRICE",
          neutral["orders"] == 0, str(neutral))

    final_flip = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("DOWN", "UP"), stop_after_vote=2)
    check("phase 2 skips a SIG PRICE flip during final validation",
          final_flip["orders"] == 0 and final_flip["price_votes"] >= 2,
          str(final_flip))

    submit_stale = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("DOWN", "DOWN", None), stop_after_vote=3)
    check("phase 2 skips stale SIG PRICE immediately before submission",
          submit_stale["orders"] == 0 and submit_stale["price_votes"] >= 3,
          str(submit_stale))


async def t_phase2_minority_rule_revalidates_the_deciding_side():
    """Minority mode must validate its decision, not compare it to SIG PRICE."""
    stable = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4, book_vote="UP", chainlink_vote="DOWN",
        minority_rule=True)
    check("stable dissenting side reaches the executor",
          stable["order_sides"] == ["DOWN"], str(stable))
    check("minority order retains a passing fresh-price commit guard",
          stable["executor_guards"] == [True], str(stable))

    changed = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        # Initial UP/UP/DOWN chooses DOWN. Once SIG PRICE moves DOWN, the
        # votes become DOWN/UP/DOWN and the minority decision changes to UP.
        price_votes=("UP", "DOWN", "DOWN", "DOWN"),
        book_vote="UP", chainlink_vote="DOWN", minority_rule=True,
        stop_after_vote=2)
    check("changed minority decision blocks the stale order side",
          changed["orders"] == 0, str(changed))


async def t_phase2_guard_uses_signal_book_for_down_orders():
    # The UP book votes UP and the complementary DOWN book votes DOWN.
    # Stable price=UP/book=UP/chainlink=DOWN selects minority DOWN.
    # Reading DOWN's depth as the global book vote would falsely flip it UP.
    books = {
        "11": ([{"price": "0.49", "size": "80"}],
               [{"price": "0.50", "size": "40"}]),
        "12": ([{"price": "0.49", "size": "40"}],
               [{"price": "0.50", "size": "80"}]),
    }
    for mode in ("PAPER", "LIVE"):
        stable = await _drive_phase2_with_hold(
            execution_mode=mode, held_provider=lambda *_a: set(),
            price_votes=("UP",) * 4, chainlink_vote="DOWN",
            minority_rule=True, token_books=books)
        check(f"{mode} valid DOWN order passes with complementary books",
              stable["order_sides"] == ["DOWN"]
              and stable["executor_guards"] == [True], str(stable))
        changed = await _drive_phase2_with_hold(
            execution_mode=mode, held_provider=lambda *_a: set(),
            price_votes=("UP", "UP", "UP", "DOWN"), chainlink_vote="DOWN",
            minority_rule=True, token_books=books)
        check(f"{mode} actual signal change at commit still rejects",
              changed["orders"] == 0 and len(changed["executor_guards"]) == 1
              and changed["executor_guards"][0] is not True, str(changed))


def t_pre_submit_guard_explains_why_it_refused():
    """The console must say WHY, not just 'pre-submit guard rejected order'.

    That generic string comes from paper_trade._pre_submit_guard_error and
    can't carry a reason - the guard is a plain bool-returning callable. The
    fix is _fresh_price_permit printing its own diagnostic on every refusal
    path, so an operator watching the console can tell a round rollover from
    a stale feed from a genuine signal reversal instead of guessing.
    """
    import main_bot

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    round_key = 1_786_320_000
    try:
        replace(main_bot.timer, "unix", lambda *_a, **_k: round_key + 100.0)
        replace(main_bot.timer, "window_start",
                lambda ts=None: round_key if ts is None or ts < round_key + 300
                else round_key + 300)
        replace(main_bot.price_ws, "fresh_snapshot",
                lambda *_a, **_k: (101.0, (round_key + 100) * 1000))
        replace(main_bot, "price_signal", lambda *_a, **_k: "DOWN")
        replace(main_bot, "chainlink_signal", lambda *_a, **_k: "UP")
        replace(main_bot, "current_chainlink_twap", lambda: 101.0)
        replace(main_bot.orderbook, "get_orderbook", lambda *_a, **_k: ((), ()))
        replace(main_bot.orderbook, "liquidity_signal", lambda *_a, **_k: "UP")
        replace(main_bot.config, "SIGNAL_MINORITY_RULE", True)
        replace(main_bot.config, "SIGNAL_DECISION_RULE", "minority")

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            # price=DOWN, book=UP, chainlink=UP -> unanimous-ish majority UP,
            # so the minority (authority) side is DOWN... wait: 1 DOWN vs 2 UP
            # is a majority, so minority_decision picks the DISSENTER, DOWN.
            # Expecting UP here forces a mismatch against the actual minority
            # pick (DOWN), which is exactly the "signal changed" scenario.
            allowed = main_bot._fresh_price_permit(
                round_key, 100.0, "UP", book_token="tok",
                chainlink_start=100.0)
        out = buf.getvalue()
        check("mismatched authority side is refused", allowed is False)
        check("the console explains it was a deciding-signal mismatch, "
              "not a generic string",
              "deciding signal is now" in out and "wanted UP" in out, out)

        # Round rollover: sampled wall is past the round's own window.
        replace(main_bot.timer, "unix", lambda *_a, **_k: round_key + 400.0)
        buf2 = io.StringIO()
        with contextlib.redirect_stdout(buf2):
            allowed2 = main_bot._fresh_price_permit(
                round_key, 100.0, "UP", book_token="tok",
                chainlink_start=100.0)
        out2 = buf2.getvalue()
        check("a rolled-over round is refused", allowed2 is False)
        check("the console names the round rollover specifically",
              "round rolled over" in out2, out2)
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


async def t_taper_hedge_grows_primary_then_hedges_the_complement():
    """PAPER-only: $3, then $2, then $1-to-the-complement, per confirmation.

    Backtested (see the entry-cap analysis) against 102 real settled rounds
    before being wired in. This locks in the mechanics: the first two
    confirmations still grow the primary side at a tapering size, the third
    onward buys the complement instead, the flag defaults off with no
    behavior change, and it stays inert in LIVE regardless of the flag.
    """
    tapered = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 20, stop_after_orders=3,
        taper_hedge_enabled=True, timeout=2.0)
    check("first two confirmations still grow the primary side",
          tapered["order_sides"][:2] == ["UP", "UP"], str(tapered))
    check("the third confirmation buys the complement instead",
          tapered["order_sides"][2] == "DOWN", str(tapered))
    check("amounts taper $3 -> $2 -> $1",
          [round(a, 2) for a in tapered["order_amounts"]] == [3.0, 2.0, 1.0],
          str(tapered["order_amounts"]))

    off = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 20, stop_after_orders=3,
        taper_hedge_enabled=False, timeout=2.0)
    check("default behavior (flag off) is unchanged: flat amount, same side",
          off["order_sides"] == ["UP", "UP", "UP"], str(off["order_sides"]))
    check("no tapering when the flag is off",
          len({round(a, 2) for a in off["order_amounts"]}) == 1,
          str(off["order_amounts"]))

    # One order is enough to prove inertness: a LIVE fill trips this test
    # harness's own "unjournaled order" safety stop after any single order
    # (last_order_receipt is never set by the fake place_trade this harness
    # installs), so asking for a 2nd/3rd here would test that unrelated
    # harness limitation, not taper-hedge.
    live = await _drive_phase2_with_hold(
        execution_mode="LIVE", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 8, stop_after_orders=1,
        taper_hedge_enabled=True, timeout=1.0)
    check("TAPER_HEDGE_ENABLED is inert in LIVE regardless of the flag",
          live["order_sides"] == ["UP"], str(live["order_sides"]))
    import config as _config
    check("LIVE amount stays at plain BET_SIZE even with the flag on",
          live["order_amounts"][0] == _config.BET_SIZE, str(live["order_amounts"]))


async def t_taper_cycle_reanchors_to_the_new_signal_without_restarting():
    """A flip changes WHICH side the cycle builds, not WHERE it is.

    Two separate things could react to a signal flip, and they were once
    conflated. The side must follow the signal - continuing to grow a side
    the signal has left is acting on a withdrawn decision. The cycle
    POSITION must not, because it only advances on a fill: resetting it sent
    the cycle back to slot 1 every flip and starved slot 3, the opposite-side
    buy, down to one in every 5.7 fills instead of one in three.

    Backtested rationale for the 1-in-3 cap itself: an unbounded hedge (grow
    twice, then hedge every remaining confirmation) turned +$1.50 total into
    +$71.10 once capped and repeated, because an unbounded hedge quietly
    inverts a long, correctly-held round into a loss.
    """
    steady = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 40, stop_after_orders=6,
        taper_hedge_enabled=True, timeout=3.0)
    check("an unflipped signal runs UP, UP, DOWN twice",
          steady["order_sides"] == ["UP", "UP", "DOWN", "UP", "UP", "DOWN"],
          str(steady))
    check("amounts taper each cycle, not just the first",
          [round(a, 2) for a in steady["order_amounts"]]
          == [3.0, 2.0, 1.0, 3.0, 2.0, 1.0],
          str(steady["order_amounts"]))

    # Flip immediately after entry 1. The side must move to DOWN, and the
    # position must stay at slot 2 - so this is entry-2 SIZE on the new side,
    # not a fresh entry-1.
    early = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN",) * 60, stop_after_orders=2,
        taper_hedge_enabled=True, timeout=3.0)
    check("the side follows the flip", early["order_sides"] == ["UP", "DOWN"],
          str(early))
    check("the cycle keeps its place: entry-2 size, not a restart",
          [round(a, 2) for a in early["order_amounts"]] == [3.0, 2.0],
          str(early["order_amounts"]))

    # A flip landing ON the opposite-side slot must not deadlock. The
    # anchored side can then be one this round never bought, and a guard
    # testing that specific token could never pass - so the slot could never
    # fill, the counter could never advance, and the cadence stopped dead.
    onslot = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 8 + ("DOWN",) * 60, stop_after_orders=6,
        taper_hedge_enabled=True, timeout=4.0)
    check("a flip on the opposite-side slot does not stall the cycle",
          len(onslot["order_sides"]) == 6, str(onslot["order_sides"]))
    check("and the cadence still completes two full cycles",
          [round(a, 2) for a in onslot["order_amounts"]]
          == [3.0, 2.0, 1.0, 3.0, 2.0, 1.0],
          str(onslot["order_amounts"]))


async def t_signal_decision_rule_selects_the_side_and_the_gates_agree():
    """SIGNAL_DECISION_RULE picks the side, and every gate asks the same rule.

    The gates matter more than the rule. Phase 2 re-validates the side three
    times after choosing it - final validation, the pre-submit guard, and
    again immediately before submission - and each calls _authority_side. If
    the chooser used one rule and the gates another, a correctly chosen order
    would be rejected on nearly every attempt (that is exactly what happened
    when the gates still compared raw SIG PRICE to a minority pick). So the
    test that counts is not "which side" but "did the order actually leave".

    price=UP book=DOWN chainlink=DOWN separates all three rules:
        price    -> UP   (book and chainlink stay diagnostics)
        minority -> UP   (UP is the dissenter, 1 vs 2)
        final    -> DOWN (price and book disagree, chainlink sides with book)
    """
    for rule, want in (("price", "UP"), ("minority", "UP"), ("final", "DOWN")):
        run = await _drive_phase2_with_hold(
            execution_mode="PAPER", held_provider=lambda *_a: set(),
            price_votes=("UP",) * 40, book_vote="DOWN", chainlink_vote="DOWN",
            decision_rule=rule, stop_after_orders=1, timeout=3.0)
        check(f"rule {rule!r} orders {want}",
              run["order_sides"] == [want], f"{rule}: {run['order_sides']}")
        check(f"rule {rule!r} actually submitted - the gates agreed",
              run["orders"] == 1 and run["executor_guards"] == [True],
              f"{rule}: orders={run['orders']} guards={run['executor_guards']}")


async def t_final_decision_rule_drives_a_whole_taper_cycle():
    """The taper cycle must ride the chosen rule, not SIG PRICE underneath it.

    Every slot anchors to what entry 1 bought and the flip check compares the
    live decision against that anchor, so if the cycle read a different rule
    from the chooser it would either restart on every attempt or never
    restart at all. Here the decision is DOWN while SIG PRICE says UP: two
    primaries must grow DOWN and the hedge must buy its complement, UP.
    """
    run = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 40, book_vote="DOWN", chainlink_vote="DOWN",
        decision_rule="final", taper_hedge_enabled=True,
        stop_after_orders=3, timeout=3.0)
    check("the cycle grows the DECIDED side, not SIG PRICE",
          run["order_sides"][:2] == ["DOWN", "DOWN"], str(run["order_sides"]))
    check("the hedge buys the complement of the decided side",
          run["order_sides"][2] == "UP", str(run["order_sides"]))
    check("amounts still taper $3 -> $2 -> hedge",
          [round(a, 2) for a in run["order_amounts"]][:2] == [3.0, 2.0],
          str(run["order_amounts"]))
    check("no attempt was rejected by a gate reading a different rule",
          run["executor_guards"] == [True, True, True],
          str(run["executor_guards"]))


def t_signal_decision_rule_is_validated_and_defaults_to_the_old_flag():
    """An unknown rule fails at import, and existing .env files are unchanged."""
    bad = _reload_config(SIGNAL_DECISION_RULE="majority")
    check("an unknown rule is refused",
          "SIGNAL_DECISION_RULE" in (bad or ""), str(bad))
    # _reload_config restores the environment before returning, so the value
    # has to be read while the override is still installed.
    import importlib
    import os
    import config as cfg
    saved = dict(os.environ)
    try:
        for flag, want in (("0", "price"), ("1", "minority")):
            os.environ["SIGNAL_MINORITY_RULE"] = flag
            os.environ.pop("SIGNAL_DECISION_RULE", None)
            importlib.reload(cfg)
            check(f"SIGNAL_MINORITY_RULE={flag} still means {want!r}",
                  cfg.SIGNAL_DECISION_RULE == want, cfg.SIGNAL_DECISION_RULE)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(cfg)


async def t_primary_entries_are_price_banded_but_hedge_legs_are_not():
    """A primary entry may be held to a tighter band than the hedge leg.

    Measured over 620 settled fills the two leg types are priced completely
    differently for almost the same hit rate - primary paid 0.577 for a 52%
    hit rate (edge -0.058), the hedge paid 0.418 for 54% (edge +0.118) - and
    the 0.60-0.80 band alone carried 52% of turnover at about -0.05 edge. So
    the ceiling belongs on the leg that follows the signal, while the hedge
    stays on the account bounds: its edge is largest in exactly the cheap
    buckets a shared floor would forbid.

    The band must reach the ORDER, not only the probe. The broker walks the
    book, so a probe that passed at the band's top says nothing about where
    the fill actually lands; both paths are checked.
    """
    import main_bot
    band = (0.25, 0.80)                     # (min, max) for primary entries
    acct = (main_bot.config.MAX_BUY_PRICE, main_bot.config.MIN_BUY_PRICE)

    run = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 40, stop_after_orders=3,
        taper_hedge_enabled=True, primary_band=band, timeout=3.0)

    check("the cycle ran two primaries then a hedge",
          run["order_sides"] == ["UP", "UP", "DOWN"], str(run["order_sides"]))
    check("both primary entries are submitted inside the primary band",
          run["order_bands"][:2] == [(0.80, 0.25), (0.80, 0.25)],
          str(run["order_bands"]))
    check("the hedge leg is submitted on the ACCOUNT band, not the primary one",
          run["order_bands"][2] == acct,
          f"{run['order_bands'][2]} vs account {acct}")
    check("the liquidity probe asks about the band it will submit under",
          (0.80, 0.25) in run["probe_bands"], str(run["probe_bands"][:4]))

    # Left at the account band the feature is inert - existing behaviour.
    plain = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 40, stop_after_orders=2,
        taper_hedge_enabled=True, primary_band=(acct[1], acct[0]), timeout=3.0)
    check("band defaulted to the account band changes nothing",
          all(b == acct for b in plain["order_bands"]),
          str(plain["order_bands"]))


def t_primary_entry_band_may_only_tighten_the_account_band():
    """Config refuses a band the brokers would silently ignore.

    Both brokers clamp a per-order bound to the account band (tighten-only),
    so a PRIMARY_ENTRY_MAX_PRICE above MAX_BUY_PRICE would read as configured
    and do nothing at all. Failing loudly at import beats that.
    """
    ok = _reload_config(MIN_BUY_PRICE="0.10", MAX_BUY_PRICE="0.90",
                        PRIMARY_ENTRY_MIN_PRICE="0.15",
                        PRIMARY_ENTRY_MAX_PRICE="0.80")
    check("a band inside the account band is accepted", ok is None, str(ok))

    loose_max = _reload_config(MIN_BUY_PRICE="0.10", MAX_BUY_PRICE="0.90",
                               PRIMARY_ENTRY_MAX_PRICE="0.95")
    check("a ceiling above MAX_BUY_PRICE is refused",
          "PRIMARY_ENTRY_MAX_PRICE" in (loose_max or ""), str(loose_max))

    loose_min = _reload_config(MIN_BUY_PRICE="0.20", MAX_BUY_PRICE="0.90",
                               PRIMARY_ENTRY_MIN_PRICE="0.05")
    check("a floor below MIN_BUY_PRICE is refused",
          "PRIMARY_ENTRY_MIN_PRICE" in (loose_min or ""), str(loose_min))

    inverted = _reload_config(MIN_BUY_PRICE="0.10", MAX_BUY_PRICE="0.90",
                              PRIMARY_ENTRY_MIN_PRICE="0.85",
                              PRIMARY_ENTRY_MAX_PRICE="0.80")
    check("a floor above its own ceiling is refused",
          "below PRIMARY_ENTRY_MAX_PRICE" in (inverted or ""), str(inverted))


def _raise_unreadable():
    raise RuntimeError("ledger unreadable")


def t_unsettled_exposure_guard_is_off_by_default_and_fails_closed():
    """Capital frozen in unresolved rounds must stop new ones being opened.

    MAX_ROUND_EXPOSURE bounds a single round, which is blind to the failure
    that actually empties an account: the venue stalling on settlement while
    every new round still passes its own budget check. Observed live - 20
    open rounds holding 103% of a $300 wallet, $3 left tradeable, purely
    because Polymarket had not written resolutions on chain. The bot must not
    settle those itself (that would be inventing an outcome), so declining to
    open more is the only honest lever.
    """
    import main_bot
    saved_cap = main_bot.config.MAX_UNSETTLED_EXPOSURE
    saved_provider = main_bot._unsettled_exposure_provider
    try:
        main_bot._unsettled_exposure_provider = lambda: 309.80

        main_bot.config.MAX_UNSETTLED_EXPOSURE = 0
        check("0 disables the guard entirely",
              main_bot._unsettled_exposure_block() is None)

        main_bot.config.MAX_UNSETTLED_EXPOSURE = 500.0
        check("under the cap, entries are permitted",
              main_bot._unsettled_exposure_block() is None)

        main_bot.config.MAX_UNSETTLED_EXPOSURE = 150.0
        blocked = main_bot._unsettled_exposure_block()
        check("at or over the cap, entries are refused", bool(blocked), str(blocked))
        check("the reason names both the figure and the cap",
              "309.80" in blocked and "150.00" in blocked, str(blocked))

        # A risk guard that cannot read its input must stop, not assume zero.
        main_bot._unsettled_exposure_provider = _raise_unreadable
        blocked = main_bot._unsettled_exposure_block()
        check("an unreadable figure fails CLOSED",
              bool(blocked) and "could not be read" in blocked, str(blocked))
        main_bot._unsettled_exposure_provider = lambda: float("nan")
        check("a non-finite figure fails CLOSED",
              bool(main_bot._unsettled_exposure_block()))

        main_bot._unsettled_exposure_provider = None
        check("no provider wired leaves the guard inert",
              main_bot._unsettled_exposure_block() is None)
    finally:
        main_bot.config.MAX_UNSETTLED_EXPOSURE = saved_cap
        main_bot._unsettled_exposure_provider = saved_provider


def t_ledger_unsettled_cost_counts_only_open_positions():
    """The figure the guard reads must exclude anything already resolved."""
    import pathlib
    import tempfile
    from accounting.ledger import Ledger
    led = Ledger(path=str(pathlib.Path(tempfile.mkdtemp()) / "l.json"))
    check("an empty ledger reports nothing frozen",
          led.unsettled_cost() == 0.0, str(led.unsettled_cost()))


async def t_unsettled_exposure_guard_stops_phase2_entries():
    """The guard has to reach the order path, not merely evaluate correctly."""
    frozen = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 20, stop_after_orders=1,
        unsettled_provider=lambda: 400.0, max_unsettled=150.0, timeout=1.5)
    check("no order is placed while frozen capital is above the cap",
          frozen["orders"] == 0, str(frozen["order_sides"]))

    clear = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 20, stop_after_orders=1,
        unsettled_provider=lambda: 10.0, max_unsettled=150.0, timeout=3.0)
    check("orders resume once it is back under the cap",
          clear["orders"] == 1, str(clear["order_sides"]))


def t_signal_unanimity_treats_silence_as_abstention_not_dissent():
    """A missing signal must not make an otherwise-agreed read "contested"."""
    import main_bot
    u = main_bot._signals_unanimous
    check("all three agreeing is unanimous", u("UP", "UP", "UP"))
    check("two agreeing with the third absent is still unanimous",
          u("UP", "UP", None))
    check("a lone voter is unanimous", u(None, "DOWN", None))
    check("one dissenter breaks it", not u("UP", "UP", "DOWN"))
    check("a 1-1 split is not unanimous", not u("UP", "DOWN", None))
    check("no votes at all is not unanimous", not u(None, None, None))


async def t_unanimity_filter_refuses_contested_reads_without_breaking_cadence():
    """Contested reads are skipped, and the taper cycle keeps its place.

    The cadence is defined over FILLS - taper_count advances only when an
    order fills - so a refused attempt leaves the cycle on the same slot
    rather than restarting it. That is the property that lets a filter and
    the 2:1 cycle coexist; resetting the counter is what broke it before.
    """
    # price=UP book=DOWN -> contested. Nothing may be sent.
    contested = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 20, book_vote="DOWN", chainlink_vote="DOWN",
        require_unanimity=True, stop_after_orders=1, timeout=1.5)
    check("a contested read places no order", contested["orders"] == 0,
          str(contested["order_sides"]))

    # Same signals, filter off -> it trades, proving the filter is the cause.
    off = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 20, book_vote="DOWN", chainlink_vote="DOWN",
        require_unanimity=False, stop_after_orders=1, timeout=3.0)
    check("the same read trades with the filter off", off["orders"] == 1,
          str(off["order_sides"]))

    # Unanimous reads pass, and the cadence still runs 2 signal : 1 opposite.
    agreed = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 40, book_vote="UP", chainlink_vote="UP",
        require_unanimity=True, taper_hedge_enabled=True,
        stop_after_orders=6, timeout=4.0)
    check("unanimous reads are traded",
          agreed["order_sides"] == ["UP", "UP", "DOWN", "UP", "UP", "DOWN"],
          str(agreed["order_sides"]))
    check("and the filter leaves the 2:1 cadence intact",
          [round(a, 2) for a in agreed["order_amounts"]]
          == [3.0, 2.0, 1.0, 3.0, 2.0, 1.0],
          str(agreed["order_amounts"]))


async def t_taper_advances_past_a_slot_whose_side_has_priced_out():
    """A slot that cannot fill must not hold the cycle for the whole round.

    Slots 1 and 2 both buy the anchored side, so once that side prices
    outside MIN/MAX_BUY_PRICE they are BOTH unfillable - and because the
    cycle only advances on a fill, it never reaches slot 3, the complement,
    which in a binary market is the leg that is cheap exactly when the anchor
    is expensive. A stable signal plus a priced-out anchor therefore costs
    the rest of the round.

    TAPER_ADVANCE_AFTER_SKIPS steps past such a slot. The trade is explicit:
    the 2:1 cadence is exact over fills only while nothing advances on a
    skip, so with this set the ratio becomes approximate.
    """
    def make_probe():
        seen = {"up": 0}
        def probe(token, *_a, **_k):
            if str(token) == "11":      # up_token_id here; fills twice then
                seen["up"] += 1         # prices out of the band for good
                if seen["up"] > 2:
                    raise ValueError("best ask 0.95 is above MAX_BUY_PRICE 0.80")
            return (), ()
        return probe

    stuck = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 80, stop_after_orders=4,
        taper_hedge_enabled=True, liquidity_probe=make_probe(),
        taper_advance=0, timeout=4.0)
    check("with advancing off the cycle wedges once UP prices out",
          stuck["order_sides"] == ["UP", "UP", "DOWN"], str(stuck["order_sides"]))

    freed = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 80, stop_after_orders=4,
        taper_hedge_enabled=True, liquidity_probe=make_probe(),
        taper_advance=2, timeout=4.0)
    check("advancing steps past the unfillable slots and keeps trading",
          len(freed["order_sides"]) > len(stuck["order_sides"]),
          f"{freed['order_sides']} vs {stuck['order_sides']}")
    check("and the extra fill is on the side that is actually buyable",
          freed["order_sides"][3] == "DOWN", str(freed["order_sides"]))

    # Nothing held means nothing to hedge, so advancing must NOT manufacture a
    # naked complement out of an empty round.
    never = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 80, stop_after_orders=2,
        taper_hedge_enabled=True,
        liquidity_probe=lambda token, *_a, **_k: (_ for _ in ()).throw(
            ValueError("nothing buyable")) if str(token) == "11" else ((), ()),
        taper_advance=2, timeout=3.0)
    check("advancing never buys a complement with no position to hedge",
          never["orders"] == 0, str(never["order_sides"]))

    # Off by default: an unset config must behave exactly as before.
    import main_bot
    check("the default leaves the cycle waiting, as it always did",
          main_bot.config.TAPER_ADVANCE_AFTER_SKIPS == 0
          or taper_advance is not None)


async def t_taper_primary_slots_sets_the_signal_to_opposite_ratio():
    """TAPER_PRIMARY_SLOTS controls how many signal-side buys per opposite.

    The cycle was hardcoded to two primary slots and one complement. This
    makes the ratio configurable so it can be TESTED - not because a best
    value is known. On the 1032-fill sample that motivated it, neither leg's
    edge reached significance (primary +0.027 p=0.096, hedge -0.050 p=0.115)
    and the previous run ranked the two legs the other way round with both
    significant. Roughly 1,100 fills per leg are needed to call a 3-point
    edge, so every ratio here currently sits inside the noise band.
    """
    async def cycle(slots, orders):
        return await _drive_phase2_with_hold(
            execution_mode="PAPER", held_provider=lambda *_a: set(),
            price_votes=("UP",) * 80, stop_after_orders=orders,
            taper_hedge_enabled=True, primary_slots=slots, timeout=5.0)

    two = await cycle(2, 6)
    check("the default 2 keeps the original 2:1 shape",
          two["order_sides"] == ["UP", "UP", "DOWN", "UP", "UP", "DOWN"],
          str(two["order_sides"]))
    check("with the original 3 / 2 / hedge sizing",
          [round(a, 2) for a in two["order_amounts"]] == [3.0, 2.0, 1.0] * 2,
          str(two["order_amounts"]))

    six = await cycle(6, 7)
    check("6 gives six signal-side buys then one opposite",
          six["order_sides"] == ["UP"] * 6 + ["DOWN"], str(six["order_sides"]))
    check("entry-1 size once, entry-2 size for the rest, then the hedge",
          [round(a, 2) for a in six["order_amounts"]]
          == [3.0] + [2.0] * 5 + [1.0], str(six["order_amounts"]))

    one = await cycle(1, 4)
    check("1 alternates signal and opposite",
          one["order_sides"] == ["UP", "DOWN", "UP", "DOWN"],
          str(one["order_sides"]))
    check("and skips the entry-2 size entirely, having no middle slot",
          [round(a, 2) for a in one["order_amounts"]] == [3.0, 1.0] * 2,
          str(one["order_amounts"]))


def t_taper_primary_slots_is_validated():
    """A cycle needs at least one signal-side slot to anchor the hedge to."""
    bad = _reload_config(TAPER_PRIMARY_SLOTS="0")
    check("zero primary slots is refused",
          "TAPER_PRIMARY_SLOTS" in (bad or ""), str(bad))
    ok = _reload_config(TAPER_PRIMARY_SLOTS="6")
    check("a larger ratio is accepted", ok is None, str(ok))


async def t_taper_ladder_sizes_each_slot_and_sets_the_cycle_length():
    """An explicit ladder gives every slot its own size.

    The old scheme had exactly two primary sizes (entry-1 then entry-2
    repeated) and a single complement slot, so a shape like 4/3/2 then 2 was
    not expressible. The ladder's LENGTH now sets the slot count, and the
    values set the sizes.

    Note the venue's 5-share minimum compresses a ladder at high prices: an
    order costs at least 5 x price, so near 0.80 every slot costs about the
    same whatever it asks for. A ladder separates only where fills are cheap.
    """
    laddered = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 80, stop_after_orders=8,
        taper_hedge_enabled=True, primary_ladder=(4.0, 3.0, 2.0),
        hedge_ladder=(2.0,), timeout=5.0)
    check("three signal-side slots then one opposite, twice over",
          laddered["order_sides"] == ["UP", "UP", "UP", "DOWN"] * 2,
          str(laddered["order_sides"]))
    check("each slot takes its own laddered size",
          [round(a, 2) for a in laddered["order_amounts"]]
          == [4.0, 3.0, 2.0, 2.0] * 2, str(laddered["order_amounts"]))

    # Several complement slots are expressible too, and the hedge ladder's
    # length is what decides how many.
    wide = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 80, stop_after_orders=5,
        taper_hedge_enabled=True, primary_ladder=(3.0, 2.0),
        hedge_ladder=(1.0, 1.0), timeout=5.0)
    check("a two-slot hedge ladder gives two complement buys",
          wide["order_sides"][:4] == ["UP", "UP", "DOWN", "DOWN"],
          str(wide["order_sides"]))

    # Unset, the previous behaviour has to be byte-for-byte intact.
    legacy = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 80, stop_after_orders=6,
        taper_hedge_enabled=True, primary_ladder=(), hedge_ladder=(),
        timeout=5.0)
    check("no ladder leaves the entry1/entry2/hedge scheme unchanged",
          [round(a, 2) for a in legacy["order_amounts"]] == [3.0, 2.0, 1.0] * 2,
          str(legacy["order_amounts"]))


def t_taper_ladder_is_parsed_and_validated():
    """A malformed ladder must fail at import, not size an order wrongly."""
    ok = _reload_config(TAPER_PRIMARY_LADDER="4,3,2", TAPER_HEDGE_LADDER="2")
    check("a well-formed ladder is accepted", ok is None, str(ok))
    for bad, why in ((("4,x,2", ""), "non-numeric"),
                     (("4,0,2", ""), "zero"),
                     (("4,-3", ""), "negative")):
        err = _reload_config(TAPER_PRIMARY_LADDER=bad[0])
        check(f"a {why} amount is refused", bool(err), f"{bad[0]} -> {err}")
    orphan = _reload_config(TAPER_HEDGE_LADDER="2")
    check("a hedge ladder with no primary ladder is refused",
          "TAPER_HEDGE_LADDER" in (orphan or ""), str(orphan))


async def t_taper_hedge_leg_survives_a_stale_primary_side_liquidity_check():
    """A hedge leg must not die because the PRIMARY side's book blipped.

    The early liquidity probe (well before the taper decision) checks
    side's own token - the right gate for a plain entry, but irrelevant to
    a hedge leg, which targets the complement. Found while investigating a
    live report of a flat, un-tapered $2.5 entry: that probe was still
    checking config.BET_SIZE on side's token regardless of what was about
    to actually be submitted. This simulates side's own book failing the
    check right as the hedge slot comes up (entries 1-2 succeed normally,
    then UP's liquidity blips) and checks the hedge still goes through on
    DOWN, which was never at issue.
    """
    calls = {"11": 0}

    def flaky_probe(token, *_a, **_k):
        token = str(token)
        if token == "11":  # up_token_id in this harness
            calls["11"] += 1
            if calls["11"] > 2:
                raise ValueError("simulated liquidity blip on the primary side")
        return (), ()

    result = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 20, stop_after_orders=3,
        taper_hedge_enabled=True, liquidity_probe=flaky_probe, timeout=3.0)
    check("entries 1-2 still fill normally before the blip",
          result["order_sides"][:2] == ["UP", "UP"], str(result))
    check("the hedge leg still fills despite the primary side's own "
          "liquidity check failing - it was never buying that side",
          result["order_sides"][2:] == ["DOWN"], str(result))
    check("amounts still taper correctly through the blip",
          [round(a, 2) for a in result["order_amounts"]] == [3.0, 2.0, 1.0],
          str(result["order_amounts"]))


async def t_paper_signal_flip_mode_keeps_repeats_and_allows_verified_flip():
    repeat = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 8, stop_after_orders=2,
        allow_signal_flips=True, timeout=1.0)
    check("PAPER signal mode retains same-side cadence entries",
          repeat["order_sides"] == ["UP", "UP"], str(repeat))

    flipped = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN",) * 4,
        stop_after_orders=2, allow_signal_flips=True, timeout=1.0)
    check("PAPER signal mode accepts UP then DOWN after a stable epoch change",
          flipped["order_sides"] == ["UP", "DOWN"], str(flipped))
    check("both accepted sides retain executor-side fresh-price guards",
          flipped["executor_guards"] == [True, True], str(flipped))


async def t_paper_signal_flip_restart_baseline_requires_a_later_transition():
    no_transition = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: {"11"},
        price_votes=("DOWN", "DOWN"), stop_after_vote=2,
        allow_signal_flips=True)
    check("current opposite side is not credited as a restart-time transition",
          no_transition["orders"] == 0, str(no_transition))

    recovered = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: {"11"},
        # Initial DOWN is blocked. A later observed UP establishes the restored
        # side, and only the following UP->DOWN epoch may buy the complement.
        price_votes=("DOWN",) * 2 + ("UP",) * 4 + ("DOWN",) * 4,
        stop_after_orders=2, allow_signal_flips=True, timeout=1.2)
    check("unique durable leg permits only a later verified flip",
          recovered["order_sides"] == ["UP", "DOWN"], str(recovered))

    ambiguous = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: {"11", "12"},
        price_votes=("UP", "UP"), stop_after_vote=2,
        allow_signal_flips=True)
    check("both-token restart fails closed because accepted order is ambiguous",
          ambiguous["orders"] == 0, str(ambiguous))


async def t_signal_flip_mode_requires_each_new_edge_once_both_legs_are_held():
    # UP, then verified DOWN, then another DOWN with no new edge: only two.
    no_new_edge = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN",) * 6,
        stop_after_vote=10, stop_after_orders=None,
        allow_signal_flips=True, timeout=1.0)
    check("both-held state rejects a repeat without another signal epoch",
          no_new_edge["order_sides"] == ["UP", "DOWN"], str(no_new_edge))

    # After UP->DOWN is accepted, observe UP but reject it in-flight, then a
    # later DOWN is an away-then-back epoch and may enter again.
    away_back = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN",) * 4
        + ("UP", "DOWN") + ("DOWN",) * 4,
        stop_after_orders=3, allow_signal_flips=True, timeout=1.2)
    check("away-then-back transition can authorize the accepted side again",
          away_back["order_sides"] == ["UP", "DOWN", "DOWN"], str(away_back))


async def t_signal_flip_mode_never_loosens_live_or_inflight_guards():
    live = await _drive_phase2_with_hold(
        execution_mode="LIVE", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN",) * 2,
        stop_after_vote=6, stop_after_orders=None,
        allow_signal_flips=True, timeout=0.8)
    check("PAPER flip flag cannot authorize a LIVE complement",
          live["order_sides"] == ["UP"], str(live))

    neutral = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=(None,), stop_after_vote=1,
        allow_signal_flips=True)
    check("neutral SIG PRICE never creates a signal epoch or order",
          neutral["orders"] == 0, str(neutral))

    inflight = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP", "DOWN"), stop_after_vote=2,
        allow_signal_flips=True)
    check("an in-flight price flip still rejects the whole attempt",
          inflight["orders"] == 0, str(inflight))

    stale_gap = await _drive_phase2_with_hold(
        execution_mode="PAPER", held_provider=lambda *_a: set(),
        price_votes=("UP",) * 4 + ("DOWN", None, "DOWN", "DOWN"),
        stop_after_vote=8, stop_after_orders=None,
        allow_signal_flips=True, timeout=0.8)
    check("transition followed by neutral cannot be consumed when side returns",
          stale_gap["order_sides"] == ["UP"], str(stale_gap))


async def t_live_execution_readiness_gates_both_phase_submissions():
    books = {"11": _book(0.60), "12": _book(0.40)}
    phase1_checks = {"n": 0}

    def phase1_drops(_condition):
        phase1_checks["n"] += 1
        return phase1_checks["n"] == 1

    phase1 = await _drive_phase1(
        books, timeout=0.55, held_provider=lambda *_a: set(),
        exposure_provider=lambda *_a: 0.0, execution_mode="LIVE",
        execution_ready_provider=phase1_drops)
    check("phase 1 rechecks private-stream readiness immediately before submit",
          phase1_checks["n"] >= 2 and phase1["orders"] == [],
          f"checks={phase1_checks} orders={phase1['orders']}")

    phase2_checks = {"n": 0}

    def phase2_drops(_condition):
        phase2_checks["n"] += 1
        return phase2_checks["n"] == 1

    phase2 = await _drive_phase2_with_hold(
        execution_mode="LIVE", held_provider=lambda *_a: set(),
        execution_ready_provider=phase2_drops)
    check("phase 2 rechecks readiness after validation and before submit",
          phase2_checks["n"] >= 2 and phase2["probes"] >= 1
          and phase2["orders"] == 0,
          f"checks={phase2_checks} seen={phase2}")

    paper = await _drive_phase1(
        books, held_provider=lambda *_a: set(),
        exposure_provider=lambda *_a: 0.0, execution_mode="PAPER",
        execution_ready_provider=lambda _condition: False)
    check("paper execution is independent of private user-stream readiness",
          paper["orders"] == ["DOWN"], str(paper["orders"]))


async def t_phase1_uses_the_band_for_the_window_it_is_in():
    """Same book, two windows, two answers: the bands must actually differ."""
    windows = ((300, 240, 0.35, 0.45, 12.0), (240, 180, 0.30, 0.40, 12.0))
    books = {"11": _book(0.80), "12": _book(0.44)}
    early = await _drive_phase1(books, remaining=280.0, bands=windows)
    check("0.44 is inside the opening band, so it trades",
          early["orders"] == ["DOWN"], str(early["orders"]))
    check("the order carries that window's ceiling as its cap",
          early.get("caps") == [0.45], str(early.get("caps")))
    later = await _drive_phase1(books, remaining=200.0, bands=windows, timeout=0.6)
    check("the same 0.44 is outside the next window's band, so it does not",
          later["orders"] == [], str(later["orders"]))


async def t_phase1_caps_the_order_at_the_band_ceiling():
    seen = await _drive_phase1({"11": _book(0.60), "12": _book(0.40)},
                               bands=((300, 120, 0.30, 0.40, 12.0),))
    check("a phase 1 order is capped at its band, not the account ceiling",
          seen.get("caps") == [0.40], str(seen.get("caps")))


async def t_phase1_skips_when_no_leg_is_in_the_band():
    seen = await _drive_phase1({"11": _book(0.60), "12": _book(0.55)}, timeout=0.6)
    check("no leg in band means no order", seen["orders"] == [], str(seen["orders"]))


async def t_phase1_refuses_a_pair_that_prices_as_arbitrage():
    # Both legs under 0.50 means the pair sums below $1.
    seen = await _drive_phase1({"11": _book(0.45), "12": _book(0.40)}, timeout=0.6)
    check("both legs in band is refused rather than picked at random",
          seen["orders"] == [], str(seen["orders"]))


async def t_phase1_respects_the_window():
    seen = await _drive_phase1({"11": _book(0.60), "12": _book(0.40)},
                               remaining=60.0, timeout=0.6)
    check("phase 1 does not trade after its window closes",
          seen["orders"] == [], str(seen["orders"]))


def t_submission_path_revalidates_every_signal_and_latency():
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    check("per-round unfillable blacklist was removed",
          "unfillable_sides" not in source)
    check("submission path samples fresh Binance data after blocking I/O",
          "submit_lp, _submit_lp_ts = price_ws.fresh_snapshot" in source)
    check("submission path samples fresh Chainlink data after blocking I/O",
          "submit_cl = current_chainlink_twap()" in source)
    check("submission path revalidates the book decision",
          "final_book_side = orderbook.liquidity_signal" in source)
    check("submission path refuses a changed fresh price signal",
          "if submit_price_side != side:" in source)
    check("both phases perform an immediate price-side submission gate",
          source.count("submit_price_side = price_signal(") == 2,
          str(source.count("submit_price_side = price_signal(")))
    check("submission path bounds end-to-end validation latency",
          "validation_age > validation_limit" in source)
    check("persisted exposure rejects infinity as well as NaN",
          "not math.isfinite(persisted)" in source)


# ------------------------------------------------------ per-round trade log ---
# The RECENT TRADES panel renders main_bot.session_trades.  A row left over from
# the round that just closed reads as activity in the live market, so the loop
# empties that list at the boundary - and only that list.  trade_log.csv is the
# durable journal and must keep every row.
def t_session_trade_log_restarts_each_round():
    tree = ast.parse(pathlib.Path("main_bot.py").read_text(encoding="utf-8"))
    run_bot = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_bot"), None)
    check("run_bot is still the strategy loop", run_bot is not None)
    if run_bot is None:
        return

    rollover = next((n for n in ast.walk(run_bot)
                     if isinstance(n, ast.If)
                     and ast.unparse(n.test) == "round_window != active_window"), None)
    check("the loop still detects the round boundary", rollover is not None)
    if rollover is None:
        return

    body = {ast.unparse(stmt) for stmt in rollover.body}
    check("the on-screen trade log restarts at the boundary",
          "session_trades.clear()" in body, str(sorted(body)))
    check("the closed round's strike is still dropped with it",
          "start_price = None" in body and "start_chainlink_price = None" in body)
    check("the reset happens only at the boundary",
          sum(1 for n in ast.walk(run_bot)
              if isinstance(n, ast.Call)
              and ast.unparse(n).startswith("session_trades.clear")) == 1)

    appender = next((n for n in ast.walk(tree)
                     if isinstance(n, ast.FunctionDef) and n.name == "_append_trade"), None)
    check("_append_trade still exists", appender is not None)
    src = ast.unparse(appender).replace("'", '"') if appender else ""
    check("the CSV journal still appends every row",
          'TRADE_LOG.open("a"' in src, src[:160])
    check("nothing truncates or deletes the CSV journal",
          "unlink" not in src and '"w"' not in src and '"w+"' not in src)


# ----------------------------------------------------------- phase config ---
def _reload_config(**env):
    """Re-import config under a temporary environment. Returns the error text."""
    import importlib
    import os
    import config as cfg

    saved = dict(os.environ)
    try:
        os.environ.update({k: str(v) for k, v in env.items()})
        importlib.reload(cfg)
        return None
    except ValueError as exc:
        return str(exc)
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(cfg)


def t_phase1_config_rejects_impossible_settings():
    check("a window that runs forwards in time-remaining is refused",
          "end < start" in (_reload_config(PHASE1_BANDS="100:200:0.30:0.40") or "").lower(),
          str(_reload_config(PHASE1_BANDS="100:200:0.30:0.40")))
    check("a malformed band entry is refused",
          "start:end:low:high" in (_reload_config(PHASE1_BANDS="300:240:0.30") or ""),
          str(_reload_config(PHASE1_BANDS="300:240:0.30")))
    check("an inverted band is refused",
          "low < high" in (_reload_config(PHASE1_BANDS="300:240:0.60:0.30") or ""),
          str(_reload_config(PHASE1_BANDS="300:240:0.60:0.30")))
    check("overlapping windows are refused",
          "overlap" in (_reload_config(
              PHASE1_BANDS="300:200:0.30:0.40,240:180:0.30:0.40") or ""),
          str(_reload_config(PHASE1_BANDS="300:200:0.30:0.40,240:180:0.30:0.40")))
    check("a cadence longer than the narrowest window is refused",
          "narrowest" in (_reload_config(PHASE1_INTERVAL_SECONDS="600") or ""),
          str(_reload_config(PHASE1_INTERVAL_SECONDS="600")))
    # The venue minimum is a share count, so the top of the band binds.
    check("a stake that cannot buy the venue minimum at the band top is refused",
          "venue minimum" in (_reload_config(BET_SIZE="1.00") or ""),
          str(_reload_config(BET_SIZE="1.00")))
    check("the shipped defaults are self-consistent", _reload_config() is None)


def t_paper_signal_flip_config_requires_one_non_band_phase():
    overlapping = _reload_config(
        PAPER_ALLOW_SIGNAL_FLIPS="1", PHASE1_ENABLED="1", PHASE2_ENABLED="1")
    check("signal-flip experiment rejects overlapping Phase 1 cadence",
          overlapping is not None and "PHASE1_ENABLED=0" in overlapping,
          str(overlapping))

    parked = _reload_config(
        PAPER_ALLOW_SIGNAL_FLIPS="1", PHASE1_ENABLED="0", PHASE2_ENABLED="0")
    check("signal-flip experiment requires the no-band Phase 2 path",
          parked is not None and "PHASE2_ENABLED=1" in parked, str(parked))

    check("explicit single Phase 2 signal-flip configuration loads",
          _reload_config(PAPER_ALLOW_SIGNAL_FLIPS="1", PHASE1_ENABLED="0",
                         PHASE2_ENABLED="1") is None)
    check("signal flips remain off by default",
          _reload_config(PAPER_ALLOW_SIGNAL_FLIPS="0") is None)


def t_phase1_bands_select_by_seconds_remaining():
    import config as cfg

    check("the open uses the first band", cfg.phase1_band(280)[2:4] == (0.35, 0.45),
          str(cfg.phase1_band(280)))
    check("mid-round uses the second band", cfg.phase1_band(200)[2:4] == (0.30, 0.40),
          str(cfg.phase1_band(200)))
    check("the last phase-1 window uses the third band",
          cfg.phase1_band(150)[2:4] == (0.40, 0.50), str(cfg.phase1_band(150)))
    check("a boundary belongs to the later window",
          cfg.phase1_band(240)[2:4] == (0.30, 0.40), str(cfg.phase1_band(240)))
    check("the T-120 window is now a band too, at its own cadence",
          cfg.phase1_band(120)[2:] == (0.55, 0.75, 8.0), str(cfg.phase1_band(120)))
    check("the closed final minute has no band", cfg.phase1_band(59) is None)
    check("before the round opens there is no band", cfg.phase1_band(301) is None)


def t_round_exposure_follows_the_enabled_phases():
    import importlib
    import math
    import os
    import config as cfg

    saved = dict(os.environ)
    try:
        os.environ.update({"PHASE1_ENABLED": "1", "PHASE2_ENABLED": "0",
                           "BET_SIZE": "2.50"})
        importlib.reload(cfg)
        phase1_only = cfg.MAX_ROUND_EXPOSURE
        # Bands may carry their own cadence, so the budget is the sum of each
        # window's own slot count, not one global division.
        # The budget is CASH, not a slot count: the broker sizes up to the
        # 5-share venue minimum and the fee lands on top, so each band is
        # reserved at the ceiling price it can actually fill at.
        slots = sum(math.ceil((s - e) / (cfg.PHASE1_INTERVAL_SECONDS if i is None else i))
                    for s, e, _lo, _hi, i in cfg.PHASE1_BANDS)
        expected = sum(
            math.ceil((s - e) / (cfg.PHASE1_INTERVAL_SECONDS if i is None else i))
            * cfg.entry_cost_ceiling(hi)
            for s, e, _lo, hi, i in cfg.PHASE1_BANDS)
        check("phase 1 budgets each band at its own cadence",
              abs(phase1_only - expected) < 1e-9,
              f"{phase1_only} vs {expected} over {slots} slots")
        check("a band priced above BET_SIZE/5 reserves more than the bet",
              phase1_only > slots * cfg.BET_SIZE,
              f"{phase1_only} vs {slots * cfg.BET_SIZE}")
        os.environ["PHASE2_ENABLED"] = "1"
        importlib.reload(cfg)
        check("switching phase 2 on raises the cap, it does not stay stale",
              cfg.MAX_ROUND_EXPOSURE > phase1_only, str(cfg.MAX_ROUND_EXPOSURE))
    finally:
        os.environ.clear()
        os.environ.update(saved)
        importlib.reload(cfg)


def t_trade_log_path_can_isolate_an_experiment():
    import os

    import main_bot

    check("trade journal default remains trade_log.csv beside the bot",
          main_bot._configured_trade_log_path("trade_log.csv")
          == main_bot.SOURCE_ROOT / "trade_log.csv")
    previous = os.environ.get("BOT_TRADE_LOG_PATH")
    try:
        os.environ["BOT_TRADE_LOG_PATH"] = "state/signal_flip.csv"
        check("relative experiment journal from env is rooted beside the bot",
              main_bot._configured_trade_log_path()
              == main_bot.SOURCE_ROOT / "state" / "signal_flip.csv")
    finally:
        if previous is None:
            os.environ.pop("BOT_TRADE_LOG_PATH", None)
        else:
            os.environ["BOT_TRADE_LOG_PATH"] = previous


def t_trade_log_rotates_when_the_schema_changes():
    import csv as _csv
    import tempfile
    from pathlib import Path

    import main_bot

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "paper_trade_log.csv"
        original = main_bot.TRADE_LOG
        main_bot.TRADE_LOG = log
        try:
            # an old-schema file, written before phases existed
            with log.open("w", newline="", encoding="utf-8") as fh:
                w = _csv.DictWriter(fh, fieldnames=["time_et", "side", "amount",
                                                    "price_side", "book_side",
                                                    "chainlink_side", "result"])
                w.writeheader()
                w.writerow({"time_et": "Aug 16 00:00:00 ET", "side": "UP",
                            "amount": 5.0, "price_side": "UP", "book_side": "UP",
                            "chainlink_side": "UP", "result": "paper_filled"})
            main_bot.session_trades.clear()
            main_bot._append_trade({
                "time_et": "Aug 16 00:00:12 ET", "phase": "phase1", "side": "DOWN",
                "amount": 2.5, "price_side": "", "book_side": "",
                "chainlink_side": "", "result": "paper_filled"})
            archive = log.with_name("paper_trade_log.pre-phase.csv")
            check("the old-schema log is preserved, not overwritten", archive.exists())
            rows = list(_csv.DictReader(log.open(encoding="utf-8")))
            check("the new log carries the phase column",
                  rows and rows[0].get("phase") == "phase1", str(rows[:1]))
            check("one rotation only: a second write appends",
                  (main_bot._append_trade({
                      "time_et": "Aug 16 00:00:24 ET", "phase": "phase2",
                      "side": "UP", "amount": 2.5, "price_side": "UP",
                      "book_side": "", "chainlink_side": "UP",
                      "result": "paper_filled"}) or
                   len(list(_csv.DictReader(log.open(encoding="utf-8")))) == 2),
                  "expected two rows in the rotated log")
            check("a row written without a phase still validates",
                  main_bot._append_trade({
                      "time_et": "Aug 16 00:00:36 ET", "side": "UP", "amount": 2.5,
                      "price_side": "UP", "book_side": "", "chainlink_side": "UP",
                      "result": "paper_filled"}) is None)
        finally:
            main_bot.TRADE_LOG = original
            main_bot.session_trades.clear()


def t_paper_never_invents_a_strike_from_mid_round():
    """PAPER must skip a round whose boundary observation it missed.

    The market asks whether the closing TWAP beats the OPENING one. A
    mid-round substitute measures a different question and inverts the signal
    once price has moved - it put one recorded fill $58 the wrong side of the
    true strike. LIVE always skipped; PAPER now does too.
    """
    source = pathlib.Path("main_bot.py").read_text(encoding="utf-8")
    for gone in ("PAPER mid-round Chainlink reference",
                 "PAPER mid-round Binance reference"):
        check(f"the fallback is gone: {gone!r}", gone not in source)
    # The boundary latch itself must still be there, and still be exact.
    check("the exact-boundary window is still enforced",
          "active_window * 1000 <= ts_ms < (active_window + 5) * 1000" in source)
    check("a missing boundary still skips the round loudly",
          "Opening prices are captured only" in source)


def t_strategy_unchanged_finite_price_abstains():
    """A zero move is not an UP signal, including at the round boundary."""
    import math

    import strategy

    check("unchanged positive price abstains",
          strategy.decide(64_000.0, 64_000.0) is None)
    check("unchanged zero price abstains", strategy.decide(0.0, 0.0) is None)
    check("a positive move still votes UP",
          strategy.decide(64_000.0, 64_000.01) == "UP")
    check("a negative move still votes DOWN",
          strategy.decide(64_000.0, 63_999.99) == "DOWN")
    check("missing start still abstains", strategy.decide(None, 64_000.0) is None)
    check("missing current still abstains", strategy.decide(64_000.0, None) is None)
    check("equal non-finite values retain their prior comparison behavior",
          strategy.decide(math.inf, math.inf) == "UP")


def t_signal_journal_measures_edge_against_the_price():
    """Edge = accuracy minus what the market charged for the same call.

    Getting this backwards would make a losing signal look profitable, so it
    is pinned against a fixture whose answer is known by construction: a
    signal right 70% of the time, priced at 60c, is +10 points of edge.
    """
    import csv as _csv
    import json as _json
    import pathlib as _pl
    import tempfile as _tf

    import signal_journal as sj

    saved = (sj.JOURNAL, sj.WINNERS)
    out = io.StringIO()
    try:
        with _tf.TemporaryDirectory() as tmp:
            sj.JOURNAL = _pl.Path(tmp) / "j.csv"
            sj.WINNERS = _pl.Path(tmp) / "w.json"
            with sj.JOURNAL.open("w", newline="", encoding="utf-8") as fh:
                wr = _csv.DictWriter(fh, fieldnames=sj.FIELDS)
                wr.writeheader()
                for i in range(10):
                    wr.writerow({"wall": i, "window": 1000 + i, "secs_left": 150,
                                 "cl_strike": 100, "cl_now": 101,
                                 "bn_strike": 100, "bn_now": 101,
                                 "up_ask": 0.60, "up_bid": 0.59,
                                 "dn_ask": 0.41, "dn_bid": 0.40,
                                 "up_bid_vol": 10, "up_ask_vol": 5})
            sj.WINNERS.write_text(_json.dumps(
                {str(1000 + i): ("UP" if i < 7 else "DOWN") for i in range(10)}))
            with contextlib.redirect_stdout(out):
                sj.analyze()
    finally:
        sj.JOURNAL, sj.WINNERS = saved

    text = out.getvalue()
    check("journal reports the known accuracy", "70.0%" in text, text[:200])
    check("journal reports what the market charged", "60.0%" in text, text[:200])
    check("journal reports edge as accuracy minus price", "+10.0" in text, text[:200])

    # A signal that merely matches the price has no edge, however accurate.
    sides = sj._sides({"cl_strike": "100", "cl_now": "99", "bn_strike": "100",
                       "bn_now": "101", "up_bid_vol": "5", "up_ask_vol": "9"})
    check("a falling TWAP reads DOWN", sides["chainlink"] == "DOWN", str(sides))
    check("a rising spot reads UP", sides["binance"] == "UP", str(sides))
    check("ask-heavy book reads DOWN", sides["book"] == "DOWN", str(sides))
    empty = sj._sides({"up_bid_vol": "0", "up_ask_vol": "9"})
    check("a one-sided book abstains rather than voting",
          empty["book"] is None and empty["binance"] is None, str(empty))


def t_orderbook_quiet_book_is_not_mistaken_for_a_stale_one():
    """A book the venue has not changed recently is still the current book.

    Measured against btc-updown-5m: the venue left a full 0.5/0.51 book
    untouched for 95 seconds while answering every request in under 400ms.
    The old check measured staleness from the last CHANGE, so every one of
    those reads was refused as "stale or future-dated" and the round traded
    blind.
    """
    import orderbook
    import timer

    now = timer.wall()

    def book(ts_s, unit_div=1000, asset="1"):
        return {"asset_id": asset, "timestamp": str(int(ts_s * unit_div)),
                "bids": [{"price": "0.50", "size": "10"}],
                "asks": [{"price": "0.51", "size": "10"}]}

    def accepted(**kw):
        kw.setdefault("now", now)
        try:
            orderbook.parse_orderbook(kw.pop("data"), "1", **kw)
            return True, ""
        except ValueError as exc:
            return False, str(exc)

    for quiet in (33.0, 95.0, 300.0, 840.0):
        ok, why = accepted(data=book(now - quiet))
        check(f"a book unchanged for {quiet:.0f}s is accepted", ok, why)

    ok, why = accepted(data=book(now - 1200.0))
    check("a book unchanged past the frozen-venue bound is refused", not ok)
    check("the frozen-venue refusal names the cause",
          "not changed" in why, why)

    # Freshness of the copy we hold is what actually matters.
    ok, why = accepted(data=book(now - 1.0), received_at=now - 40.0)
    check("a response held longer than the age limit is refused", not ok)
    check("the held refusal is distinct from the quiet one",
          "stale in hand" in why, why)

    # Unit detection: the same instant expressed four ways must agree.
    for div, unit in ((1, "s"), (1000, "ms"), (10**6, "us"), (10**9, "ns")):
        ok, why = accepted(data=book(now - 2.0, unit_div=div))
        check(f"a timestamp in {unit} is read at the right scale", ok, why)
        if ok:
            check(f"{unit} is reported as the detected unit",
                  orderbook.LAST_TIMESTAMP_REPORT["unit"] == unit,
                  str(orderbook.LAST_TIMESTAMP_REPORT["unit"]))

    # Future-dating is a clock or unit fault, never a real book.
    ok, _ = accepted(data=book(now + 2.0))
    check("a book inside the future tolerance is accepted", ok)
    ok, why = accepted(data=book(now + 30.0))
    check("a book dated well ahead of us is refused", not ok)
    check("the future refusal points at the clock and the unit",
          "future-dated" in why and "unit" in why, why)

    # Safety validation must not be reachable around.
    for label, kw in (("now", {"now": float("nan")}),
                      ("max_age_s", {"max_age_s": float("nan")}),
                      ("max_quiet_s", {"max_quiet_s": float("nan")}),
                      ("future_tol_s", {"future_tol_s": float("nan")}),
                      ("received_at", {"received_at": float("nan")})):
        ok, _ = accepted(data=book(now - 1.0), **kw)
        check(f"a non-finite {label} cannot bypass validation", not ok)

    for label, data in (
            ("crossed", {"asset_id": "1", "timestamp": str(int(now * 1000)),
                         "bids": [{"price": "0.60", "size": "1"}],
                         "asks": [{"price": "0.50", "size": "1"}]}),
            ("empty", {"asset_id": "1", "timestamp": str(int(now * 1000)),
                       "bids": [], "asks": []}),
            ("mismatched asset", book(now, asset="999")),
            ("zero timestamp", {"asset_id": "1", "timestamp": "0",
                                "bids": [{"price": "0.5", "size": "1"}],
                                "asks": []}),
            ("unreadable timestamp", {"asset_id": "1", "timestamp": "abc",
                                      "bids": [{"price": "0.5", "size": "1"}],
                                      "asks": []})):
        ok, _ = accepted(data=data)
        check(f"a {label} book is still refused", not ok)

    # The rejection has to carry enough to diagnose it without a rerun.
    accepted(data=book(now - 33.0))
    r = orderbook.LAST_TIMESTAMP_REPORT
    for field in ("exchange_ts_raw", "exchange_ts_s", "unit", "local_ts_s",
                  "clock_offset_s", "received_at_s", "quiet_s", "held_s",
                  "max_age_s", "max_quiet_s", "future_tol_s", "source"):
        check(f"the diagnostic report carries {field}", field in r, str(sorted(r)))


def t_ws_book_accepts_a_quiet_resubscribe_snapshot():
    """The same fault on the websocket path blocked the initial sync.

    A resubscribe snapshot carries the last-change timestamp. Bounding it by
    stale_after meant any book quiet for more than a few seconds never synced
    at all, so the token stayed unusable for the whole round.
    """
    import timer
    from feeds.book import BookState

    b = BookState(stale_after=8.0)
    now_ms = int(timer.wall() * 1000)

    check("a snapshot quiet for 60s passes the event-time gate",
          b._fresh_exchange_ts(now_ms - 60_000))
    check("a snapshot quiet for 10 min passes the event-time gate",
          b._fresh_exchange_ts(now_ms - 600_000))
    check("a snapshot older than the frozen-venue bound is refused",
          not b._fresh_exchange_ts(now_ms - 1_200_000))
    check("a future-dated event is refused",
          not b._fresh_exchange_ts(now_ms + 30_000))
    check("a missing timestamp is refused", not b._fresh_exchange_ts(None))
    check("an unreadable timestamp is refused", not b._fresh_exchange_ts("abc"))
    check("liveness is still measured from receipt, not event time",
          b.stale_after == 8.0)


def t_ctrl_c_is_a_clean_exit_not_a_traceback():
    """Ctrl+C must leave a quiet 130, not asyncio's Windows teardown dump."""
    from run_feeds import run_quietly

    async def raise_interrupt():
        await asyncio.sleep(0)
        raise KeyboardInterrupt

    async def child_still_running():
        async def linger():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                return
        leftover = asyncio.create_task(linger())
        await asyncio.sleep(0)
        raise KeyboardInterrupt
        leftover.cancel()

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        code = run_quietly(raise_interrupt())
    err = buf.getvalue()
    check("in-task Ctrl+C is exit 130", code == 130, str(code))
    check("in-task Ctrl+C prints no traceback",
          "Traceback" not in err and "KeyboardInterrupt" not in err, err)

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        code = run_quietly(child_still_running())
    err = buf.getvalue()
    check("Ctrl+C drains leftover tasks", code == 130, str(code))
    check("Ctrl+C leaves no pending-task warning",
          "Task was destroyed but it is pending" not in err, err)


def t_guard_rejection_reason_reaches_both_brokers():
    import main_bot
    import paper_trade
    import polymarket_trade
    for helper in (paper_trade._pre_submit_guard_error,
                   polymarket_trade._pre_submit_guard_error):
        reason = helper(lambda: main_bot._fresh_price_permit(
            0, None, "UP", explain=True))
        check("broker preserves explicit refusal reason",
              reason == "pre-submit guard rejected order: invalid order side or missing start price",
              str(reason))
        check("guard still requires literal True", helper(lambda: 1) is not None)
        check("valid guard still passes", helper(lambda: True) is None)


def main():
    # A crashing test must be one failure, not a suite that stops reporting.
    def run(fn, is_async=False):
        global F
        try:
            asyncio.run(fn()) if is_async else fn()
        except Exception as exc:
            F += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")

    for fn in [v for k, v in sorted(globals().items())
               if k.startswith("t_") and not asyncio.iscoroutinefunction(v)]:
        run(fn)
    for fn in [v for k, v in sorted(globals().items())
               if k.startswith("t_") and asyncio.iscoroutinefunction(v)]:
        run(fn, True)
    print(f"\n{P} passed, {F} failed")
    return 1 if F else 0


if __name__ == "__main__":
    sys.exit(main())
