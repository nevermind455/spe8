"""Wiring the feeds into the bot's existing read points.

This is the only file in the package that can change what the bot sees, so
every substitution here is behind an environment variable and the defaults are
chosen to leave the decision path byte-identical.

    BTC_FEED=ws|legacy            default ws
        `ws` runs the hardened Binance feed and writes the SAME module global
        the bot already reads (`price_ws.latest_price`), from the SAME stream
        (btcusdt@trade). The value semantics are unchanged; what changes is
        reconnect speed, heartbeat, staleness detection and session rotation.

    PRICE_STALE_POLICY=keep|none  default none
        `keep`  - retain the old value for a legacy display surface.
        `none`  - blank that display value once stale. The production decision
                  path always uses ``fresh_snapshot()`` and rejects the same
                  stale print under either setting.

    BOOK_SOURCE=ws_shadow|rest|ws default ws_shadow
        `rest`      - untouched. WS not consulted.
        `ws_shadow` - WS book is maintained and measured against the REST book
                      on every call, but REST still answers the bot. Decisions
                      byte-identical. Use this to see the disagreement rate
                      before trusting the socket.
        `ws`        - the WS book answers the bot; REST is used only when the
                      book is UNSYNCED or DISCONNECTED. This is what the brief
                      asks for, and it IS a change to a decision input:
                      `liquidity_signal` compares summed bid vs ask size, and
                      a continuously-maintained book will not always agree
                      with a point-in-time REST snapshot.

    USER_WS=on|off                default on   (purely additive: observes)
    RECONCILE=auto|off            default auto (backup for USER_WS)
"""
from __future__ import annotations

import asyncio
import math
import os
import threading
import time

from .health import LIVE
from .hub import FeedHub

_installed = False
_audit: dict = {"gen": -1, "pending": None}
_originals: dict[tuple[object, str], object] = {}
_orig_liquidity_signal = None
_orig_get_orderbook = None
_audit_lock = threading.RLock()
# Token -> timestamps of the last book this process served to a caller.
# Read by the order path through a hook so the order layer never imports
# the feed layer.
_book_meta: dict = {}
_book_meta_lock = threading.RLock()


def last_book_meta(token_id):
    """Timestamps of the most recent book served for this token, or None."""
    with _book_meta_lock:
        meta = _book_meta.get(str(token_id))
        return dict(meta) if meta else None


def _env(name: str, default: str, allowed: tuple[str, ...]) -> str:
    v = (os.environ.get(name) or default).strip().lower()
    if v not in allowed:
        raise ValueError(f"{name} must be one of {allowed}, got {v!r}")
    return v


def _positive_float_env(name: str, default: str) -> float:
    raw = os.environ.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value


class AdapterConfig:
    def __init__(self) -> None:
        self.btc_feed = _env("BTC_FEED", "ws", ("ws", "legacy"))
        self.price_stale_policy = _env("PRICE_STALE_POLICY", "none", ("keep", "none"))
        self.book_source = _env("BOOK_SOURCE", "ws_shadow", ("ws_shadow", "rest", "ws"))
        self.user_ws = _env("USER_WS", "on", ("on", "off"))
        self.reconcile = _env("RECONCILE", "auto", ("auto", "off"))
        self.book_audit = _env("BOOK_AUDIT", "on", ("on", "off"))
        self.btc_stale_after = _positive_float_env("BTC_STALE_AFTER", "3.0")
        self.book_stale_after = _positive_float_env("BOOK_STALE_AFTER", "8.0")

    @property
    def decisions_unchanged(self) -> bool:
        return self.book_source in ("rest", "ws_shadow")

    def describe(self) -> str:
        tag = "byte-identical" if self.decisions_unchanged else "DECISION INPUTS CHANGED"
        return (f"BTC_FEED={self.btc_feed} PRICE_STALE_POLICY={self.price_stale_policy} "
                f"BOOK_SOURCE={self.book_source} USER_WS={self.user_ws} "
                f"RECONCILE={self.reconcile} BOOK_AUDIT={self.book_audit}  [{tag}]")


class BookAgreement:
    """How often the WS book and the REST book imply the same side.

    This is the number that decides whether BOOK_SOURCE=ws is safe, so it is
    measured rather than assumed.

    A REST reply lands some milliseconds after the WS view it is compared
    against, and in a moving book that skew alone causes disagreement. So the
    WS view is read a second time when the REST reply arrives:

        agree_rate          rest vs the WS view at decision time
        agree_rate_timed    rest vs the WS view at REST-arrival time

    If `agree_rate_timed` is high while `agree_rate` is not, the books are not
    diverging - they are moving, and you are seeing the round trip. If both
    are low, the socket book is genuinely wrong.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.compared = 0
        self.agree = 0
        self.agree_timed = 0
        self.ws_unavailable = 0
        self.samples = 0
        self.skew_ms: list[float] = []
        self.last: tuple = (None, None, None)

    def record(self, rest_side, ws_side, ws_side_at_rest=None,
               skew_ms: float | None = None) -> None:
        with self._lock:
            self.last = (rest_side, ws_side, ws_side_at_rest)
            if skew_ms is not None and math.isfinite(skew_ms):
                self.skew_ms.append(skew_ms)
                if len(self.skew_ms) > 500:
                    del self.skew_ms[:250]
            if ws_side is None:
                self.ws_unavailable += 1
                return
            self.compared += 1
            if rest_side == ws_side:
                self.agree += 1
            if rest_side == (ws_side_at_rest if ws_side_at_rest is not None else ws_side):
                self.agree_timed += 1

    def summary(self) -> dict:
        with self._lock:
            n = self.compared
            skew = sorted(self.skew_ms)
            return {"compared": n, "agree": self.agree,
                    "agree_rate": (self.agree / n) if n else None,
                    "agree_rate_timed": (self.agree_timed / n) if n else None,
                    "ws_unavailable": self.ws_unavailable,
                    "audit_samples": self.samples,
                    "median_skew_ms": skew[len(skew) // 2] if skew else None,
                    "last_rest": self.last[0], "last_ws": self.last[1],
                    "last_ws_at_rest": self.last[2]}


def _patch(obj, name, factory) -> None:
    original = getattr(obj, name)
    _originals[(obj, name)] = original
    setattr(obj, name, factory(original))


def install(hub: FeedHub, cfg: AdapterConfig | None = None, *, on_event=None):
    """Install the adapters. Returns (cfg, agreement).

    Must be installed BEFORE the dashboard probes, so the shadow comparison
    calls the bot's real `liquidity_signal` and not an instrumented copy.
    """
    global _installed, _orig_liquidity_signal, _orig_get_orderbook
    if _installed:
        raise RuntimeError("adapters already installed")
    cfg = cfg or AdapterConfig()

    import market_discovery
    import orderbook
    import price_ws

    _orig_liquidity_signal = orderbook.liquidity_signal
    _orig_get_orderbook = orderbook.get_orderbook
    agreement = BookAgreement()

    # ---- market discovery: drive rotation, return value untouched --------
    def wrap_tokens(orig):
        def inner(*a, **kw):
            out = orig(*a, **kw)
            try:
                if out:
                    # This function is also used by the prewarmer for the NEXT
                    # window.  Only a discovery result for the actual current
                    # window may be promoted into the strategy snapshot.
                    import timer
                    discovered = int(out.get("window_start"))
                    current = timer.window_start()
                    if discovered == current:
                        hub.set_round(out.get("up_token_id"), out.get("down_token_id"),
                                      out.get("condition_id"), discovered,
                                      out.get("window_end"))
                    elif discovered < current and on_event:
                        on_event("rotation",
                                 f"ignored stale discovery window={discovered} current={current}",
                                 "warn")
            except Exception as exc:
                if on_event:
                    on_event("rotation", f"adapter rotation failed: "
                             f"{type(exc).__name__}: {exc}",
                             "warn")
            return out
        return inner
    _patch(market_discovery, "get_tokens_for_current_round", wrap_tokens)

    # ---- book source ------------------------------------------------------
    if cfg.book_source != "rest":
        def _remember_book(token, view, source):
            """Record the served book's own timestamps for the order path.

            as_rest() throws away everything except prices and sizes, so the
            order layer could not say how old the book it priced against
            actually was. Ages must be computed from updated_mono against
            another monotonic reading only - the exchange stamp is kept raw
            and never subtracted from a local clock.
            """
            meta = {
                "source": source,
                "read_mono": time.monotonic(),
                "exchange_ts_ms": None,
                "updated_mono": None,
                "generation": None,
                "updates": None,
            }
            if view is not None:
                meta["exchange_ts_ms"] = getattr(view, "exchange_ts_ms", None)
                meta["updated_mono"] = getattr(view, "updated_mono", None)
                meta["generation"] = getattr(view, "generation", None)
                meta["updates"] = getattr(view, "updates", None)
            with _book_meta_lock:
                _book_meta[str(token)] = meta

        def wrap_book(orig):
            def inner(token_id, *a, **kw):
                token = str(token_id)
                view = hub.book.view(token)
                usable = view.status in (LIVE,) and (view.bids or view.asks)

                if cfg.book_source == "ws" and usable:
                    # Arm ONE audit sample per round. This is an O(1) tuple
                    # assignment; the REST call happens later on the sampler
                    # task, so the order path never waits on a round trip.
                    if cfg.book_audit == "on" and hub.generation != _audit["gen"]:
                        with _audit_lock:
                            if hub.generation != _audit["gen"]:
                                _audit["gen"] = hub.generation
                                _audit["pending"] = (token, view, time.monotonic())
                    _remember_book(token, view, "ws")
                    return view.as_rest()

                # REST: startup, recovery and fallback only.
                rest = orig(token_id, *a, **kw)
                # A REST read has no view timestamps: parse_orderbook checked
                # its exchange stamp but does not return it, so the age here
                # is genuinely unknown rather than zero.
                _remember_book(token, None, "rest")
                if cfg.book_source == "ws":
                    hub.rest_fallbacks += 1
                    if on_event:
                        on_event("book", f"REST fallback ({view.status})", "warn")
                    # A REST snapshot is also a valid resync for the local book.
                    try:
                        if view.status != LIVE:
                            hub.book.apply_snapshot(
                                token, rest[0], rest[1], only_if_unsynced=True,
                                expected_generation=view.generation)
                    except Exception as exc:
                        if on_event:
                            on_event("book", f"REST resync failed: {type(exc).__name__}",
                                     "warn")
                    return rest

                # ws_shadow: REST answers, WS is scored against it.
                try:
                    rest_side = _orig_liquidity_signal(rest[0], rest[1])
                    ws_side = None
                    if usable:
                        wb, wa = view.as_rest()
                        ws_side = _orig_liquidity_signal(wb, wa)
                    agreement.record(rest_side, ws_side, ws_side)
                    if on_event and ws_side is not None and ws_side != rest_side:
                        on_event("book", f"shadow disagree rest={rest_side} ws={ws_side}",
                                 "warn")
                except Exception as exc:
                    if on_event:
                        on_event("book", f"shadow comparison failed: "
                                          f"{type(exc).__name__}", "warn")
                return rest
            return inner
        _patch(orderbook, "get_orderbook", wrap_book)

    # ---- BTC price --------------------------------------------------------
    if cfg.btc_feed == "ws":
        def on_price(price, mono, exchange_ts_ms=None):
            price_ws.publish_price(price, mono, exchange_ts_ms)
        hub.binance._on_price = on_price

        # Neutralise the legacy reconnect loop if anything still calls it.
        async def _disabled():
            while True:
                await asyncio.sleep(3600)
        _patch(price_ws, "stream_price", lambda orig: _disabled)

    _installed = True
    return cfg, agreement


async def agreement_sampler(hub: FeedHub, cfg: AdapterConfig, agreement: BookAgreement,
                            stop, on_event=None) -> None:
    """Score the socket book against REST once per round, in `ws` mode.

    Monitoring, not polling: one request per 5-minute round, never in the
    order path, and the result is never fed back into a decision. It exists
    so `BOOK_SOURCE=ws` is not flying blind - in `ws_shadow` every call is
    already compared, so this task is idle there.
    """
    if cfg.book_source != "ws" or cfg.book_audit != "on":
        return
    while not stop.is_set():
        await asyncio.sleep(0.2)
        with _audit_lock:
            pending = _audit["pending"]
            _audit["pending"] = None
        if pending is None:
            continue
        token, view_at_decision, t0 = pending
        try:
            rest = await asyncio.to_thread(_orig_get_orderbook, token)
        except Exception as exc:
            if on_event:
                on_event("audit", f"REST sample failed: {type(exc).__name__}", "warn")
            continue
        skew_ms = (time.monotonic() - t0) * 1000.0
        try:
            rest_side = _orig_liquidity_signal(rest[0], rest[1])
            wb, wa = view_at_decision.as_rest()
            ws_side = _orig_liquidity_signal(wb, wa)
            now_view = hub.book.view(token)
            ws_now = (_orig_liquidity_signal(*now_view.as_rest())
                      if now_view.status == LIVE else None)
            with agreement._lock:
                agreement.samples += 1
            agreement.record(rest_side, ws_side, ws_now, skew_ms)
            if on_event and rest_side != ws_side:
                on_event("audit", f"book audit disagree rest={rest_side} "
                                  f"ws={ws_side} ws_now={ws_now} skew={skew_ms:.0f}ms",
                         "warn")
        except Exception as exc:
            if on_event:
                on_event("audit", f"book sample invalid: {type(exc).__name__}",
                         "warn")


async def price_staleness_watchdog(hub: FeedHub, cfg: AdapterConfig, stop,
                                   on_event=None) -> None:
    """Enforce PRICE_STALE_POLICY=none.

    Only runs when the policy is `none`. Blanking `latest_price` is a
    deliberate behaviour change and is announced when it fires.
    """
    if cfg.price_stale_policy != "none":
        return
    import price_ws
    blanked = False
    while not stop.is_set():
        await asyncio.sleep(0.2)
        fresh = (hub.binance.fresh_price() if cfg.btc_feed == "ws" else
                 price_ws.fresh_snapshot(cfg.btc_stale_after)[0])
        if fresh is None:
            price, observed, _ = price_ws.latest_snapshot()
            if not blanked and price is not None and price_ws.clear_if_observation(observed):
                blanked = True
                if on_event:
                    on_event("binance", "price STALE - latest_price blanked "
                                        "(PRICE_STALE_POLICY=none)", "bad")
        elif blanked:
            blanked = False
            if on_event:
                on_event("binance", "price fresh again", "good")


def uninstall() -> None:
    global _installed, _orig_liquidity_signal, _orig_get_orderbook
    with _audit_lock:
        _audit["gen"] = -1
        _audit["pending"] = None
    for (obj, name), original in list(_originals.items()):
        setattr(obj, name, original)
    _originals.clear()
    _orig_liquidity_signal = None
    _orig_get_orderbook = None
    _installed = False
