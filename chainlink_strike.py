"""Chainlink-computed 60-second TWAP for BTC five-minute markets.

The ordinary Chainlink crypto-price topic and the Ethereum BTC/USD aggregator
are spot feeds.  BTC five-minute Up/Down markets use the separate Chainlink
60-second TWAP product, exposed by Polymarket RTDS without credentials:

    wss://ws-live-data.polymarket.com
    topic  crypto_prices_twap_sixty
    symbol btc/usd

The window length is not a preference: the market's own resolution text names
the 60-second stream as the settlement source
(https://data.chain.link/streams/btc-usd-twap-60s-streams).  RTDS also
publishes a 30-second TWAP on `crypto_prices_twap_thirty`, and the two
disagree by around a dollar at any instant - enough to decide a market whose
outcome is "is the TWAP at or above where it started".  Reading the wrong one
models a different market.

The strike for a five-minute window is the 60-second TWAP observation whose
Chainlink timestamp is exactly that window's boundary.  RTDS publishes the
TWAP every second; accepting a boundary+1s packet as the opening price changes
the market being modelled.  It is captured only when this service was already
connected before the boundary.  RTDS provides no snapshot, history, or replay,
so a connection opened mid-window must not invent the missing strike.  It may
still provide the current TWAP for a signal once fresh updates arrive.

Prices are parsed from ``full_accuracy_value`` (signed E18) when available and
otherwise from the exact decimal representation of ``value``.  They remain
``Decimal`` objects on the decision path; conversion to binary float is only
used for the optional Binance-divergence diagnostic.
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from decimal import Decimal, InvalidOperation

import timer
import websockets

RTDS = "wss://ws-live-data.polymarket.com"
SYMBOL = "btc/usd"
WINDOW = 300                      # 5-minute markets
TWAP_WINDOW = 60
RAW_TOPIC = "crypto_prices_twap_sixty"
SDK_TOPIC = "prices.crypto.chainlink.twap"
PING_EVERY = 5.0
# How long an unanswered PING may stand before the socket is treated as
# dead. RTDS publishes every second and answers PING within one round
# trip, so silence this long is pathological, never quiet.
PONG_TIMEOUT = 5.0
STALE_AFTER = 20.0
E18 = Decimal(10) ** 18


def window_start(ts: float | None = None, window: int = WINDOW) -> int:
    # Round identity must use Unix, the same clock as discovery, market slugs,
    # and Binance trade timestamps. CLOB ``/time`` can lag; using it here
    # made the current round overrun and the next one open late.
    if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
        raise ValueError("window must be a positive integer")
    t = int(timer.unix() if ts is None else ts)
    return t - (t % window)


class ChainlinkStrike:
    """Keeps the first 60-second TWAP observation of each market window."""

    def __init__(self, url: str = RTDS, symbol: str = SYMBOL,
                 window: int = WINDOW, on_event=None,
                 stale_after: float = STALE_AFTER) -> None:
        url = str(url or "").strip()
        symbol = str(symbol or "").strip().lower()
        if not url.startswith("wss://"):
            raise ValueError("Chainlink RTDS URL must use wss://")
        if not symbol:
            raise ValueError("Chainlink symbol must not be empty")
        if not isinstance(window, int) or isinstance(window, bool) or window <= 0:
            raise ValueError("window must be a positive integer")
        if not math.isfinite(float(stale_after)) or float(stale_after) <= 0:
            raise ValueError("stale_after must be a finite positive number")
        self.url, self.symbol, self.window = url, symbol, window
        self._on_event = on_event
        self.stale_after = float(stale_after)
        self.strikes: dict[int, Decimal] = {}      # market window -> TWAP
        self._strike_ts_ms: dict[int, int] = {}
        self.value: Decimal | None = None           # latest fresh/old TWAP
        self.value_ts_ms: int | None = None
        self.value_mono: float | None = None
        self.messages = 0
        self.reconnects = 0
        self.partial_windows_skipped = 0
        self._last_partial_window: int | None = None
        self.last_error: str | None = None
        self.connected = False
        self.event_callback_errors = 0
        self.invalid_messages = 0
        self._connection_window: int | None = None
        self.pong_at: float | None = None
        self._pong_event = asyncio.Event()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------- reads
    def strike_for(self, window_ts: int | None = None) -> Decimal | None:
        """The Price To Beat for a window, or None if we were not listening."""
        return self.strikes.get(window_ts if window_ts is not None else window_start())

    def current_value(self) -> Decimal | None:
        """Return only a live TWAP; stale data must never drive a signal."""
        age = self.age_ms
        observed_age = self.observation_age_ms
        limit = self.stale_after * 1000.0
        if (self.value is None or age is None or observed_age is None
                or age > limit or observed_age > limit or observed_age < -5_000):
            return None
        return self.value

    @property
    def age_ms(self) -> float | None:
        m = self.value_mono
        return None if m is None else (time.monotonic() - m) * 1000.0

    @property
    def observation_age_ms(self) -> float | None:
        ts = self.value_ts_ms
        return None if ts is None else timer.exchange_age_s(ts) * 1000.0

    def divergence(self, binance_price: float | None,
                   window_ts: int | None = None) -> dict:
        """How wrong was the Binance strike?

        This is the number that says whether switching feeds mattered. Run it
        for a day before trusting any conclusion about the strategy.
        """
        s = self.strike_for(window_ts)
        if s is None or binance_price is None:
            return {"strike": s, "binance": binance_price, "diff": None}
        b = Decimal(str(binance_price))
        d = b - s
        return {"strike": s, "binance": binance_price, "diff": float(d),
                "diff_bps": float((d / s) * Decimal(10_000)) if s else None}

    def health(self) -> dict:
        status = ("LIVE" if self.current_value() is not None else
                  "STALE" if self.connected else "DISCONNECTED")
        return {"status": status, "connected": self.connected,
                "value": self.value, "value_timestamp_ms": self.value_ts_ms,
                "age_ms": self.age_ms,
                "observation_age_ms": self.observation_age_ms,
                "topic": RAW_TOPIC, "twap_window_s": TWAP_WINDOW,
                "windows_captured": len(self.strikes),
                "partial_windows_skipped": self.partial_windows_skipped,
                "invalid_messages": self.invalid_messages,
                "messages": self.messages, "reconnects": self.reconnects,
                "last_error": self.last_error}

    # --------------------------------------------------------------- run
    def start(self) -> asyncio.Task:
        # ``start`` is intentionally idempotent.  Lifecycle retries must never
        # create two sockets consuming the same topic into one mutable state.
        if self._task is not None and not self._task.done():
            return self._task
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="chainlink_strike")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                self.last_error = f"stop failed: {type(exc).__name__}: {exc}"[:160]
                self._event(self.last_error, "bad")

    def _event(self, text, level="info"):
        if self._on_event:
            try:
                self._on_event("strike", text, level)
            except Exception as exc:
                self.event_callback_errors += 1
                self.last_error = f"event callback failed: {type(exc).__name__}"[:160]

    async def _run(self) -> None:
        delay = 0.25
        while not self._stop.is_set():
            try:
                await self._session()
                delay = 0.25
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not getattr(exc, "_chainlink_reported", False):
                    self.last_error = f"{type(exc).__name__}: {exc}"[:160]
                    self._event(self.last_error, "bad")
            if self._stop.is_set():
                break
            self.reconnects += 1
            sleep_for = delay
            delay = min(8.0, delay * 2)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                continue

    async def _session(self) -> None:
        # Protocol-level ping, answered automatically by any conformant
        # WebSocket peer. This used to be ping_interval=None plus an
        # application-level "PING" text frame, but RTDS has no such
        # application heartbeat: it publishes ~1/s and never replies "PONG",
        # so the custom heartbeat timed out PING_EVERY+PONG_TIMEOUT after
        # connecting and closed the socket with 1011 - a 10s connect/die loop
        # that left SIG CHAINLINK missing most of the time. Verified on the
        # wire: a raw subscriber that sends nothing stays up indefinitely and
        # receives ~1 frame/s. The half-open detection the custom heartbeat
        # existed for is preserved, and now uses the mechanism the peer
        # actually implements.
        async with websockets.connect(self.url,
                                      ping_interval=PING_EVERY,
                                      ping_timeout=PONG_TIMEOUT,
                                      open_timeout=10, close_timeout=2) as ws:
            # A connection opened inside a window cannot reconstruct that
            # window's first print: RTDS has no snapshot or replay.
            self._connection_window = window_start()
            self.connected = True
            self.last_error = None
            # A disconnect between PING and PONG must not poison the next
            # socket into failing its first heartbeat.
            self.pong_at = time.monotonic()
            self._pong_event.set()
            await ws.send(json.dumps({
                "action": "subscribe",
                "subscriptions": [
                    {"topic": RAW_TOPIC, "type": "update",
                     "filters": json.dumps(
                         {"symbol": self.symbol}, separators=(",", ":"))},
                ],
            }))
            self._event("connected to Chainlink 60s TWAP RTDS", "good")
            try:
                while not self._stop.is_set():
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    self._handle(raw)
            except websockets.exceptions.ConnectionClosed as exc:
                # The protocol-level ping/pong keepalive (ping_interval /
                # ping_timeout above) detects a dead socket for us, including
                # a half-open one where our own sends fail silently - but
                # without this, that just looked like every other closure in
                # the logs. Recording the close code/reason here is what the
                # old application-level heartbeat's distinct "heartbeat send
                # failed" / "heartbeat close failed" messages used to give:
                # a keepalive timeout (no PONG in time) is now told apart
                # from a normal server-initiated close.
                self.last_error = (
                    f"RTDS connection closed: code={exc.code} "
                    f"reason={exc.reason or '(none)'}")[:160]
                self._event(self.last_error, "warn")
                # Still re-raised, so _run()'s backoff (it only resets delay
                # on a clean return from _session()) is unaffected - just
                # tagged so _run()'s generic handler does not immediately
                # overwrite this more specific message with a duplicate,
                # less informative one.
                exc._chainlink_reported = True
                raise
            finally:
                self._connection_window = None
                self.connected = False
                # Retain the last value for diagnostics, but a disconnected
                # socket must not continue supplying a decision input.
                self.value_mono = None

    def _reject(self, reason: str) -> None:
        """Record malformed data without throwing the whole socket away."""
        self.invalid_messages += 1
        self.last_error = f"invalid RTDS message: {reason}"[:160]

    def _handle(self, raw) -> None:
        """Parse and assign only. No I/O on the receive path."""
        self.messages += 1
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        if not isinstance(raw, str):
            self._reject(f"non-text frame ({type(raw).__name__})")
            return
        if raw.strip().upper() == "PONG":
            # The heartbeat's only evidence that the peer is still there.
            self.pong_at = time.monotonic()
            self._pong_event.set()
            return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            self._reject(type(exc).__name__)
            return
        for m in (msg if isinstance(msg, list) else [msg]):
            if not isinstance(m, dict):
                self._reject(f"non-object item ({type(m).__name__})")
                continue
            topic = str(m.get("topic") or "")
            if topic not in (RAW_TOPIC, SDK_TOPIC):
                continue
            if str(m.get("type") or "").lower() != "update":
                continue
            p = m.get("payload") or m
            if not isinstance(p, dict):
                self._reject(f"non-object payload ({type(p).__name__})")
                continue
            if str(p.get("symbol") or "").lower() != self.symbol:
                continue
            try:
                twap_window = int(p.get("window_s", p.get(
                    "windowSeconds", p.get("window_seconds", 0))))
                ts_ms = int(p.get("timestamp") or 0)
            except (TypeError, ValueError):
                self._reject("invalid window or timestamp")
                continue
            if twap_window != TWAP_WINDOW or ts_ms <= 0:
                self._reject("unexpected window or non-positive timestamp")
                continue
            observation_age_ms = timer.exchange_age_s(ts_ms) * 1000.0
            if observation_age_ms < -5_000:
                self._reject("future-dated observation")
                continue
            value = _price(p)
            if value is None or value <= 0:
                self._reject("invalid price")
                continue

            # A later trusted update means any prior malformed-frame health
            # error has recovered.  Callback failures below can set it again.
            self.last_error = None

            # Ignore a late packet for the live value.  The packet can still
            # improve the stored boundary observation below if its Chainlink
            # timestamp is earlier than the one already received.
            if self.value_ts_ms is None or ts_ms >= self.value_ts_ms:
                self.value = value
                self.value_ts_ms = ts_ms
                self.value_mono = time.monotonic()

            w = window_start(ts_ms / 1000.0, self.window)
            if (self._connection_window is not None
                    and w <= self._connection_window
                    and w not in self.strikes):
                if w != self._last_partial_window:
                    self.partial_windows_skipped += 1
                    self._last_partial_window = w
                continue
            # The market's opening value is the observation AT the boundary.
            # Updates are one-second observations, so boundary+1 is a
            # different value, not a tolerable delivery delay. Delivery may
            # itself be late; compare the payload timestamp, not arrival time.
            if ts_ms // 1000 != w:
                if w != self._last_partial_window:
                    self.partial_windows_skipped += 1
                    self._last_partial_window = w
                continue
            previous_ts = self._strike_ts_ms.get(w)
            if previous_ts is None or ts_ms < previous_ts:
                self.strikes[w] = value
                self._strike_ts_ms[w] = ts_ms
                self._event(f"60s TWAP strike {w} = ${value:,.2f}", "good")
                if len(self.strikes) > 200:
                    for k in sorted(self.strikes)[:100]:
                        del self.strikes[k]
                        self._strike_ts_ms.pop(k, None)


def _price(payload: dict) -> Decimal | None:
    """Parse the exact signed E18 value first, then the decimal display value."""
    full = payload.get("full_accuracy_value")
    try:
        if full not in (None, ""):
            value = Decimal(str(full)) / E18
        else:
            raw = payload.get("value")
            if raw in (None, ""):
                return None
            value = Decimal(str(raw))
        return value if value.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None
