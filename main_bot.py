"""BTC 5-min Polymarket bot - A.5 snapshot (Mar 11 2026 ~03:14 ET)."""
import asyncio
import csv
import math
import os
import threading
import time
from pathlib import Path

import config
import market_discovery
import http_pool
import orderbook
import price_ws
import strategy
import polymarket_trade
import timer
from polymarket_trade import cancel_all_open_orders, get_balance_allowance, place_trade
from timer import current_round_window_et, now_et, seconds_left

SOURCE_ROOT = Path(__file__).resolve().parent


def _configured_trade_log_path(raw: str | None = None) -> Path:
    """Resolve an optional experiment journal without changing the default."""
    configured = (os.environ.get("BOT_TRADE_LOG_PATH", "trade_log.csv")
                  if raw is None else raw)
    configured = str(configured or "").strip() or "trade_log.csv"
    candidate = Path(configured)
    return candidate if candidate.is_absolute() else SOURCE_ROOT / candidate


TRADE_LOG = _configured_trade_log_path()
session_trades = []

# run_feeds.py replaces only the execution functions when --paper is chosen.
# The strategy and its timing loop stay identical.
execution_mode = "LIVE"
_paper_broker = None
_accounting_enabled = False
# How old the final-validation book may be before the depth/spread probe
# refuses to reuse it and fetches its own. Normally the two are microseconds
# apart - only local work separates them - so anything beyond this means a
# blocking branch ran and the book is no longer the one that was validated.
_FINAL_BOOK_REUSE_SECONDS = 1.0

_round_exposure_provider = None
_round_held_tokens_provider = None
# Returns (entry_price, fee_per_share) for one open leg, or None. Only the
# pair-lock guard reads it; without it that guard stays closed.
_round_leg_basis_provider = None
_execution_ready_provider = None

# Set by run_feeds / run_terminal when the RTDS 60-second TWAP feed is running.
# The direct main_bot.py entrypoint creates the same service itself.
_strike = None
_strike_read_error = None


def timer_window_start(window: int = 300) -> int:
    return timer.window_start(window=window)


def chainlink_twap_for_round(window_ts: int | None = None):
    """Return the captured 60-second TWAP strike, never a spot substitute."""
    service = _strike
    if service is None:
        return None
    try:
        return service.strike_for(
            timer_window_start() if window_ts is None else window_ts)
    except Exception as exc:
        _record_strike_read_error(exc)
        return None


def current_chainlink_twap():
    """Return only a fresh 60-second TWAP from the RTDS service."""
    service = _strike
    if service is None:
        return None
    try:
        return service.current_value()
    except Exception as exc:
        _record_strike_read_error(exc)
        return None


BINANCE_AGG_TRADES = "https://api.binance.com/api/v3/aggTrades"


def _recover_boundary_print(window_start: int, timeout: float = 6.0):
    """Fetch the round's opening print that the websocket failed to latch.

    The latch needs a trade stamped inside the first 5 seconds of the round.
    If the socket is mid-reconnect across the boundary that trade is never
    delivered, and the whole round is skipped - measured at roughly one round
    in five.

    This is recovery, not substitution: aggTrades is queried for the SAME
    [window, window+5) interval, so the value returned is the one the socket
    would have latched, not a later price standing in for it. A response whose
    timestamp falls outside that interval is refused, because a trade from
    later in the round answers a different question - the market asks whether
    the close beats the OPEN.
    """
    try:
        response = http_pool.get(
            BINANCE_AGG_TRADES,
            params={"symbol": config.SYMBOL, "startTime": int(window_start) * 1000,
                    "endTime": (int(window_start) + 5) * 1000, "limit": 1},
            timeout=timeout)
        response.raise_for_status()
        rows = response.json()
    except Exception:
        return None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        return None
    try:
        stamped = int(rows[0]["T"])
        price = float(rows[0]["p"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (int(window_start) * 1000 <= stamped < (int(window_start) + 5) * 1000):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    return price


def price_signal(round_key: int, start_price, current_price):
    """Round-tagged Binance direction used by execution and dashboard telemetry."""
    del round_key  # Identity is consumed by the telemetry wrapper.
    # The momentum gate belongs to the Binance signal alone. Chainlink is a
    # 60s TWAP of a different series; a bps threshold tuned to spot would not
    # mean the same thing there.
    return strategy.decide(start_price, current_price,
                           config.SIG_PRICE_MIN_MOVE_BPS)


def chainlink_signal(round_key: int, start_price, current_price):
    """Round-tagged Chainlink direction kept as a diagnostic signal."""
    del round_key  # Identity is consumed by the telemetry wrapper.
    return strategy.decide(start_price, current_price)


def _fresh_price_permit(round_key: int, start_price, expected_side: str, *,
                        signal_observer=None, book_token: str | None = None,
                        chainlink_start=None, explain=False):
    """Authorize one irreversible order step against the latest signal.

    The executor calls this after its own blocking work (and again for each
    live retry), so an order cannot outlive the signal that selected its
    side.  A missing/stale print, a round rollover, equality, or a flipped
    side all fail closed.

    Unless SIGNAL_DECISION_RULE is "price", the order side is chosen from all
    three signals (see `_authority_side`), not SIG PRICE alone. When the
    caller supplies `book_token` and `chainlink_start`, this permit recomputes
    that same decision from freshly-sampled signals instead of comparing raw
    SIG PRICE against `expected_side` - otherwise a genuine order would almost
    always fail this check (SIG PRICE alone rarely equals the chosen side),
    and a stale pick could slip through whenever SIG PRICE alone happened to
    coincide with it.  Callers that never pass `book_token` (phase 1 bands,
    which are price-only by design) keep the SIG-PRICE-only behavior.
    """
    def reject(reason):
        return polymarket_trade.GuardRejection(reason) if explain else False

    if expected_side not in ("UP", "DOWN") or start_price is None:
        print(f"{_ts()} [GUARD] pre-submit refused: expected_side="
              f"{expected_side!r} invalid or start_price missing.")
        return reject("invalid order side or missing start price")
    try:
        sampled_wall = timer.unix()
        if timer.window_start(sampled_wall) != round_key:
            if signal_observer is not None:
                signal_observer(None)
            print(f"{_ts()} [GUARD] pre-submit refused: round rolled over "
                  f"before submission.")
            return reject("round rolled over before submission")
        current_price, current_ts_ms = price_ws.fresh_snapshot(
            config.BTC_STALE_AFTER)
        if current_price is None or current_ts_ms is None:
            if signal_observer is not None:
                signal_observer(None)
            print(f"{_ts()} [GUARD] pre-submit refused: BTC price feed is "
                  f"stale (no sample within {config.BTC_STALE_AFTER}s).")
            return reject("BTC price feed is stale")
        if timer.window_start(float(current_ts_ms) / 1000.0) != round_key:
            if signal_observer is not None:
                signal_observer(None)
            print(f"{_ts()} [GUARD] pre-submit refused: freshest price "
                  f"sample belongs to a different round than the order.")
            return reject("price sample belongs to a different round")
        sampled_side = price_signal(round_key, start_price, current_price)
        if config.SIGNAL_DECISION_RULE != "price" and book_token is not None:
            book_side = None
            try:
                bids, asks = orderbook.get_orderbook(book_token)
                book_side = orderbook.liquidity_signal(bids, asks)
            except Exception:
                book_side = None
            chainlink_side = chainlink_signal(
                round_key, chainlink_start, current_chainlink_twap())
            authority_side = _authority_side(sampled_side, book_side, chainlink_side)
            if signal_observer is not None:
                signal_observer(authority_side)
            if authority_side != expected_side:
                print(f"{_ts()} [GUARD] pre-submit refused: deciding signal "
                      f"is now {authority_side or 'neutral/tied'}, order "
                      f"wanted {expected_side} (price={sampled_side or 'n/a'} "
                      f"book={book_side or 'n/a'} chainlink={chainlink_side or 'n/a'}).")
            return (True if authority_side == expected_side else reject(
                f"deciding signal is now {authority_side or 'neutral/tied'}, order wanted {expected_side}"))
        if signal_observer is not None:
            signal_observer(sampled_side)
        if sampled_side != expected_side:
            print(f"{_ts()} [GUARD] pre-submit refused: SIG PRICE is now "
                  f"{sampled_side or 'neutral'}, order wanted {expected_side}.")
        return (True if sampled_side == expected_side else reject(
            f"SIG PRICE is now {sampled_side or 'neutral'}, order wanted {expected_side}"))
    except (TypeError, ValueError, OverflowError) as exc:
        if signal_observer is not None:
            try:
                signal_observer(None)
            except Exception:
                pass
        print(f"{_ts()} [GUARD] pre-submit refused: {type(exc).__name__}: {exc}")
        return reject(f"signal validation failed: {type(exc).__name__}")


def _fresh_signal_permit(source: str, expected_side: str, *, round_key: int,
                         chainlink_start=None, book_token: str | None = None,
                         ) -> bool:
    """Re-check the signal that selected a multi-signal leg, before the fill.

    The price path has ``_fresh_price_permit`` for this; SIG BOOK and SIG
    CHAINLINK need the same contract, because an order must not outlive the
    signal that chose its side. Runs inside the broker's pre-submit callback,
    after the modeled latency, so it re-reads live state rather than reusing
    the value that opened the attempt. Anything unreadable fails closed.
    """
    if expected_side not in ("UP", "DOWN"):
        return False
    try:
        if source == "chainlink":
            return chainlink_signal(
                round_key, chainlink_start, current_chainlink_twap()
            ) == expected_side
        if source == "book":
            if not book_token:
                return False
            bids, asks = orderbook.get_orderbook(book_token)
            return orderbook.liquidity_signal(bids, asks) == expected_side
    except Exception:
        return False
    return False


class _RoundSignalEpoch:
    """Track accepted direction against observed non-neutral SIG PRICE runs.

    The epoch advances on the first usable side and on every later UP/DOWN
    transition.  Neutral or missing samples never manufacture a transition.
    This state is deliberately round-local: carrying it across binary markets
    would let a move in one condition authorize the complement of another.
    """

    __slots__ = (
        "observed_side", "epoch", "accepted_side", "accepted_epoch",
        "ambiguous_restart",
    )

    def __init__(self):
        self.observed_side = None
        self.epoch = 0
        self.accepted_side = None
        self.accepted_epoch = None
        self.ambiguous_restart = False

    def observe(self, side: str | None) -> int:
        if side not in ("UP", "DOWN"):
            # Revoke an unconsumed edge without inventing a direction.  The
            # same side returning after an unknown-price gap must first move
            # away and back before it can authorize a held complement.
            if (self.accepted_epoch is not None
                    and self.epoch > self.accepted_epoch):
                self.accepted_epoch = self.epoch
            return self.epoch
        if side != self.observed_side:
            self.observed_side = side
            self.epoch += 1
        return self.epoch

    def initialize_from_durable(self, held_tokens: set[str],
                                up_token: str, down_token: str) -> None:
        """Restore a conservative last-side baseline after a restart.

        One durable leg identifies the last possible accepted direction, but
        it is anchored to the *current* epoch.  Therefore a current opposite
        signal cannot be mistaken for a transition observed by this process;
        a later non-neutral UP/DOWN transition is required.  With both legs,
        order is unknowable from the set, so PAPER flip mode remains blocked.
        """
        if self.accepted_side is not None or self.ambiguous_restart:
            return
        held = {str(token) for token in held_tokens}
        up_token, down_token = str(up_token), str(down_token)
        known = held.intersection((up_token, down_token))
        if held.difference((up_token, down_token)) or len(known) > 1:
            self.ambiguous_restart = True
            return
        if known == {up_token}:
            self.accepted_side = "UP"
            self.accepted_epoch = self.epoch
        elif known == {down_token}:
            self.accepted_side = "DOWN"
            self.accepted_epoch = self.epoch

    def record_accepted(self, side: str) -> None:
        if side not in ("UP", "DOWN"):
            return
        self.accepted_side = side
        self.accepted_epoch = self.epoch

    def paper_flip_permit(self, side: str) -> tuple[bool, str]:
        """Allow a held complement only after a post-accept transition."""
        if self.ambiguous_restart:
            return False, (
                "durable holdings contain both/unknown legs; "
                "last accepted side is ambiguous")
        if self.accepted_side is None or self.accepted_epoch is None:
            return False, "last accepted side is unavailable"
        if self.observed_side != side or self.epoch <= self.accepted_epoch:
            return False, ("no later non-neutral transition of the deciding "
                           "signal was observed")
        return True, "verified transition of the deciding signal"


def _authority_side(price_side, book_side, chainlink_side):
    """The signal that actually decides the order side under this config.

    This is the ONLY implementation of that rule. The phase-2 chooser calls
    it to pick the side, and the three re-validation gates (final, pre-submit
    and immediately-before-submit) call it again to confirm the side still
    holds. That shared identity is load-bearing, not tidiness: when a gate
    evaluates a different rule from the chooser, it rejects nearly every
    order the chooser makes - which is exactly what happened when the gates
    still compared raw SIG PRICE against a minority pick.

    The round epoch also tracks this, because under any rule but "price" the
    decision can flip because BOOK or CHAINLINK moved while SIG PRICE stood
    still; an epoch watching SIG PRICE alone would call that "no transition"
    and refuse the very complement the flip was supposed to buy.
    """
    rule = config.SIGNAL_DECISION_RULE
    if rule == "minority":
        return strategy.minority_decision(price_side, book_side, chainlink_side)
    if rule == "final":
        return strategy.final_decision(price_side, book_side, chainlink_side)
    return price_side


stop_event = threading.Event()


def _ts():
    return now_et().strftime("[%b %d %H:%M:%S ET]")


def _record_strike_read_error(exc) -> None:
    """Fail closed and surface a broken RTDS service object once per error type."""
    global _strike_read_error
    detail = f"{type(exc).__name__}: {exc}"[:160]
    if detail != _strike_read_error:
        _strike_read_error = detail
        print(f"{_ts()} [TWAP] Read failed: {detail}")


TRADE_LOG_FIELDS = ["time_et", "phase", "side", "amount", "price_side",
                    "book_side", "chainlink_side", "result"]


def _rotate_trade_log_if_stale() -> bool:
    """Move an old-schema log aside once, rather than mixing column counts.

    A file whose header lacks `phase` cannot hold the new rows: csv readers
    key off the header, so the extra value would be silently dropped and the
    analysis would read phase 1 and phase 2 as one undifferentiated blob.
    """
    if not TRADE_LOG.exists():
        return False
    try:
        with TRADE_LOG.open(newline="", encoding="utf-8") as f:
            header = next(csv.reader(f), [])
    except OSError:
        return False
    if header == TRADE_LOG_FIELDS:
        return False
    archive = TRADE_LOG.with_name(f"{TRADE_LOG.stem}.pre-phase{TRADE_LOG.suffix}")
    try:
        TRADE_LOG.replace(archive)
    except OSError as exc:
        print(f"{_ts()} [LOG] could not archive the old trade log: {type(exc).__name__}")
        return False
    print(f"{_ts()} [LOG] trade log schema changed; previous rows kept in {archive.name}")
    return True


# Which log path has already been checked for an old schema. Rotation can
# only ever happen once per file, but the check itself opened and read the
# CSV header on EVERY appended row - synchronous file I/O on the asyncio loop,
# repeated for a decision that cannot change. Keyed by path rather than a bare
# flag so redirecting TRADE_LOG still re-evaluates the new file.
_rotation_checked: set = set()


def _append_trade(row):
    row.setdefault("phase", "")
    session_trades.append(row)
    try:
        if TRADE_LOG not in _rotation_checked:
            _rotate_trade_log_if_stale()
            _rotation_checked.add(TRADE_LOG)
        write_header = not TRADE_LOG.exists()
        with TRADE_LOG.open("a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_LOG_FIELDS)
            if write_header:
                w.writeheader()
            w.writerow(row)
            f.flush()
    except OSError as exc:
        # A display journal failure must not relabel an already-submitted live
        # order as failed or crash the state machine. The persistent fill
        # ledger remains authoritative.
        print(f"{_ts()} [LOG] Trade CSV write failed: {type(exc).__name__}")


async def _cooldown(seconds: float | None = None) -> None:
    """Wait out the trade interval, but never across a round boundary.

    BUGFIX: this used to be a flat TRADE_INTERVAL_SECONDS sleep that only woke
    for shutdown. Round rotation and the opening-print latch both live at the
    TOP of the strategy loop, so a cooldown beginning a second or two before a
    boundary held the loop for the rest of its 12s - and the new round was not
    detected until ~10s in. By then the opening print, which is only latchable
    from a trade stamped in the first 5 seconds, was already unreachable and
    the round was lost. Returning at the boundary costs nothing: the loop
    re-enters, sees the new window, and the interval restarts naturally.
    """
    gap = config.TRADE_INTERVAL_SECONDS if seconds is None else seconds
    deadline = time.monotonic() + gap
    entry_window = timer.window_start()
    while time.monotonic() < deadline and not stop_event.is_set():
        if timer.window_start() != entry_window:
            return
        await asyncio.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def _pair_lock_permit(condition_id: str | None, other_token: str,
                      ask) -> tuple[bool, str]:
    """Permit a complement leg only when the finished pair cannot lose.

    Every failure path returns False. This relaxes the guard that normally
    stops the bot owning both legs, so anything it cannot positively verify -
    a disabled switch, a missing provider, an unreadable basis, a raising
    ledger - has to read as "refuse", exactly as if the lock were off.
    """
    if not config.PAIR_LOCK_ENABLED:
        return False, "pair-lock is disabled"
    if _round_leg_basis_provider is None:
        return False, "no ledger basis provider is installed"
    try:
        basis = _round_leg_basis_provider(condition_id, other_token)
    except Exception as exc:
        return False, f"ledger basis unavailable ({type(exc).__name__})"
    if not basis:
        return False, "the held leg has no readable cost basis"
    entry, entry_fee = basis
    permitted, locked = config.pair_lock_permits(entry, entry_fee, ask)
    if not permitted:
        return False, (
            f"pair would cost ${entry + entry_fee + float(ask):.4f} all-in "
            f"for a $1.00 payout (needs {config.PAIR_LOCK_MIN_EDGE:.4f} edge)"
        )
    return True, (
        f"locks ${locked:+.4f}/pair: held {entry:.3f}+{entry_fee:.4f}fee "
        f"plus this leg at {float(ask):.3f}"
    )


def _refresh_durable_round_state(window_start: int, condition_id: str | None,
                                 exposure: float,
                                 held_tokens: set[str]) -> tuple[float, set[str]]:
    """Merge persisted exposure and owned/accepted legs into loop state."""
    current = float(exposure)
    if not math.isfinite(current) or current < 0:
        raise RuntimeError("in-memory round exposure is invalid")
    if _round_exposure_provider is not None:
        persisted = float(_round_exposure_provider(window_start, condition_id))
        if not math.isfinite(persisted) or persisted < 0:
            raise RuntimeError("persisted round exposure is invalid")
        current = max(current, persisted)

    merged = set(held_tokens)
    if _round_held_tokens_provider is not None:
        durable = _round_held_tokens_provider(window_start, condition_id)
        if durable is None:
            durable = ()
        if isinstance(durable, (str, bytes, dict)):
            raise RuntimeError("persisted held-token state is invalid")
        try:
            for raw in durable:
                token = str(raw or "")
                if (not token or len(token) > 256 or not token.isprintable()
                        or any(ch.isspace() for ch in token)):
                    raise RuntimeError("persisted held-token state is invalid")
                merged.add(token)
        except TypeError as exc:
            raise RuntimeError("persisted held-token state is invalid") from exc
    return current, merged


def _execution_ready(mode: str, condition_id: str | None) -> bool:
    """Paper is self-accounting; LIVE requires its private fill stream."""
    if mode != "LIVE":
        return True
    if _execution_ready_provider is None:
        return False
    try:
        # Require a literal boolean so a malformed provider cannot fail open.
        return _execution_ready_provider(condition_id) is True
    except Exception as exc:
        raise RuntimeError("live execution-readiness check failed") from exc


def _kill_switch():
    print("[KILL] Press Enter in this terminal to stop the bot...")
    try:
        input()
    except EOFError:
        return
    stop_event.set()
    print(f"{_ts()} [BOT] Kill switch triggered.")


async def run_bot():
    start_price = None
    joined_window = None
    boundary_backfilled = False
    start_chainlink_price = None
    active_window = None
    round_exposure = 0.0
    last_status = 0.0
    skip_logged_window = None
    held_tokens: set[str] = set()
    # Defined before the loop so the reuse test can never raise NameError on a
    # path that reaches it without a final read. The sentinel token matches no
    # real token id, so the fallback is always a fresh fetch.
    final_book_token, final_book_mono = None, 0.0
    last_phase1 = 0.0
    signal_epoch = _RoundSignalEpoch()

    mode = str(execution_mode or "LIVE").upper()
    if mode not in {"LIVE", "PAPER"}:
        raise RuntimeError(f"invalid execution mode {mode!r}")
    if mode == "PAPER":
        if _paper_broker is None or not polymarket_trade.live_execution_disabled():
            raise RuntimeError("paper mode firewall was not installed")
    elif _paper_broker is not None or polymarket_trade.live_execution_disabled():
        raise RuntimeError("live mode cannot start in a paper-disabled process")

    print(f"{_ts()} [BOT] ========== BTC 5-min Polymarket bot started ({mode}) ==========")
    if mode == "PAPER":
        print(f"{_ts()} [PAPER] LIVE ORDERS DISABLED | no signer, wallet auth, or order endpoint.")
        print(f"{_ts()} [PAPER] Fills use the live public book; settlement uses Polymarket resolution.")
    print(f"{_ts()} [BOT] Timezone: Eastern (ET). Rounds every 5 min. Trade from open, every {config.TRADE_INTERVAL_SECONDS:.0f}s.")
    print(
        f"{_ts()} [BOT] First prints latch the strike; then trade for "
        f"{config.TRADE_LAST_SECONDS}s of the round, every {config.TRADE_INTERVAL_SECONDS:.0f}s."
    )
    print(
        f"{_ts()} [BOT] Time check: now {now_et().strftime('%b %d %H:%M:%S ET')} | "
        f"current round {current_round_window_et()} (compare with Polymarket)"
    )
    # Phase-1 band sizing notes describe orders phase 1 would place. Printing
    # them while phase 1 is off says the bot will do something it will not,
    # and sends anyone reading the log looking at the wrong price range.
    for s, e, lo, hi, need_lo, need_hi in (
            getattr(config, "PHASE1_STAKE_NOTES", ())
            if config.PHASE1_ENABLED else ()):
        print(
            f"{_ts()} [BOT] NOTE: band {lo:.2f}-{hi:.2f} (T-{s}..T-{e}) exceeds "
            f"BET_SIZE/5, so the venue's 5-share minimum will size those orders "
            f"to ${need_lo:.2f}-${need_hi:.2f} instead of ${config.BET_SIZE:.2f}."
        )
    print(f"{_ts()} [BOT] Kill switch: press Enter in this terminal to stop safely.")

    bal = await asyncio.to_thread(get_balance_allowance)
    if bal:
        if mode == "PAPER":
            print(f"{_ts()} [PAPER] Simulated cash balance: ${bal['balance']:.2f}")
        else:
            print(f"{_ts()} [BOT] Polymarket USDC balance: ${bal['balance']:.2f} | allowance: ${bal['allowance']:.2f}")
    else:
        message = "Could not read balance/allowance at startup"
        if mode == "LIVE":
            raise RuntimeError(f"{message}; live mode is fail-closed")
        print(f"{_ts()} [BOT] WARN: {message}.")

    clock_ok, clock_detail, drift = await asyncio.to_thread(
        timer.check_clock, config.CLOB_HOST, config.CLOCK_MAX_DRIFT_SECONDS)
    if drift is not None:
        print(
            f"{_ts()} [CLOCK] {clock_detail}; "
            f"round windows use Unix time, CLOB offset {timer.clock_offset():+.3f}s "
            f"applies only to book timestamps"
        )
    else:
        print(f"{_ts()} [CLOCK] {clock_detail}")
    if not clock_ok:
        if mode == "LIVE":
            raise RuntimeError(
                "CLOB clock synchronization could not be verified; "
                f"timing is unsafe ({clock_detail}). Sync this computer's clock "
                "to internet time, then restart. Windows: start the Windows Time "
                "service and run `w32tm /resync`.")
        if drift is not None:
            print(
                f"{_ts()} [CLOCK] WARN: local clock is past the "
                f"{config.CLOCK_MAX_DRIFT_SECONDS:.3f}s live limit; PAPER continues "
                "and keeps Unix 5-minute windows so rounds match Polymarket slugs."
            )
        else:
            print(
                f"{_ts()} [CLOCK] WARN: CLOB clock could not be verified; "
                "PAPER continues on the local clock."
            )

    print(f"{_ts()} [BOT] Strategy loop started. Waiting for price feed and next round (ET)...")

    while not stop_event.is_set():
        sampled_wall = timer.unix()
        remain = seconds_left(sampled_wall)
        round_window = timer.window_start(sampled_wall)
        round_end = round_window + 300
        exact_remaining = round_end - sampled_wall
        if round_window != active_window:
            # The first window this process sees was already underway when it
            # started: its open is behind us, the book has moved, and only part
            # of the trading window remains. Note it so the round can be
            # observed but not traded, and begin at the next clean boundary.
            if joined_window is None:
                joined_window = round_window
            # Never carry a prior round's strike into the next market.  The
            # old loop only overwrote these when a feed read succeeded.
            active_window = round_window
            start_price = None
            boundary_backfilled = False
            start_chainlink_price = None
            # The on-screen trade log is a per-round view. Rows from the round
            # that just closed would read as activity in this market, so the
            # log restarts at the boundary. trade_log.csv keeps every row.
            session_trades.clear()
            # Per-round phase-1 state: what we already own (so we never buy
            # both legs of the same market) and when we last attempted.
            held_tokens = set()
            last_phase1 = 0.0
            # How many phase-2 entries have filled this round, regardless of
            # side. TAPER_HEDGE_ENABLED reads this to decide whether the next
            # confirmation still grows the primary side or now funds a hedge.
            taper_count = 0
            # The side entry 1 actually bought. Later slots in the SAME
            # cycle anchor to this rather than to the live signal; a flip in
            # the signal retires the cycle and re-anchors here instead.
            taper_primary_side = None
            # Discovery answers the same question all round, so ask once.
            # Re-fetching per attempt cost 8 gamma calls a round, and
            # _fetch_slug retries twice at a 10s timeout: one bad call could
            # burn ~21s of a 30s cadence slot, and last_phase1 is stamped
            # BEFORE the fetch, so that attempt was simply lost.
            round_tokens = None
            signal_epoch = _RoundSignalEpoch()
            # LIVE authorizations are keyed by the known five-minute window,
            # so they can be restored before discovery. PAPER inventory is
            # keyed by condition and is refreshed immediately after discovery.
            round_exposure, held_tokens = _refresh_durable_round_state(
                active_window, None, 0.0, held_tokens)

        display_price, _display_mono, display_ts = price_ws.latest_snapshot()
        lp, lp_ts_ms = price_ws.fresh_snapshot(config.BTC_STALE_AFTER)

        if start_chainlink_price is None:
            twap_start = chainlink_twap_for_round(active_window)
            if twap_start is not None:
                start_chainlink_price = twap_start
                print(
                    f"{_ts()} [ROUND] Chainlink 60s TWAP "
                    f"start_price=${start_chainlink_price:,.2f}"
                )
                if lp is not None and _strike is not None:
                    d = _strike.divergence(lp, active_window)
                    if d.get("diff") is not None:
                        print(
                            f"{_ts()} [TWAP] Binance is "
                            f"{d['diff']:+.2f} / {d['diff_bps']:+.1f}bps "
                            f"from the 60s TWAP strike"
                        )
            # PAPER used to substitute a mid-round TWAP here when the boundary
            # observation was missed. It cannot: the market asks whether the
            # closing TWAP beats the OPENING one, so a mid-round reference
            # measures a different question and inverts the signal whenever
            # price has already moved. Measured at 4.9% of phase-2 fills, one
            # of them $58 the wrong side of the true strike. Both modes now
            # skip the round instead, which is what LIVE always did.

        # Latch the first print whose exchange timestamp is in the opening
        # 5 seconds. Never invent a strike from a later print: see above.
        if start_price is None:
            for px, ts_ms in ((lp, lp_ts_ms), (display_price, display_ts)):
                if (px is not None and ts_ms is not None
                        and active_window * 1000 <= ts_ms < (active_window + 5) * 1000):
                    start_price = px
                    print(
                        f"{_ts()} [ROUND] New round started "
                        f"(Binance start_price=${start_price:,.2f})"
                    )
                    break
            if (start_price is None and not boundary_backfilled
                    and exact_remaining <= 300 - config.BOUNDARY_BACKFILL_AFTER):
                # The socket has had its chance; ask REST for the same trade.
                boundary_backfilled = True
                recovered = await asyncio.to_thread(
                    _recover_boundary_print, active_window)
                if recovered is not None:
                    start_price = recovered
                    print(f"{_ts()} [ROUND] Opening print recovered from REST "
                          f"(Binance start_price=${start_price:,.2f})")
                else:
                    print(f"{_ts()} [ROUND] Opening print could not be recovered; "
                          f"this round has no Binance reference.")

        now = asyncio.get_running_loop().time()
        if now - last_status >= 30:
            last_status = now
            price_txt = (f"${display_price:,.2f}" if display_price is not None
                         else "waiting for price...")
            status_band = (config.phase1_band(exact_remaining)
                           if config.PHASE1_ENABLED else None)
            if status_band is not None:
                phase = (f"PHASE 1 band {status_band[2]:.2f}-{status_band[3]:.2f} "
                         f"every {status_band[4]:.0f}s")
            elif not config.PHASE2_ENABLED:
                phase = "idle | phase 2 parked"
            elif exact_remaining > config.TRADE_LAST_SECONDS:
                wait_s = exact_remaining - config.TRADE_LAST_SECONDS
                phase = f"analysis | first trade in {wait_s:.0f}s"
            elif exact_remaining >= config.MIN_SECONDS_TO_EXPIRY:
                phase = "TRADE WINDOW"
            else:
                phase = "round ending"
            print(
                f"{_ts()} [BOT] Running | round {current_round_window_et()} | "
                f"ends in {remain}s | {phase} | {price_txt}"
            )

        # BUGFIX: this guard used to sit BETWEEN phase 1 and phase 2, so only
        # phase 2 was ever reached by it - phase 1 had already `continue`d.
        # SKIP_JOINED_ROUND then meant "phase 2 waits for a clean boundary
        # while the bands trade the joined round anyway", which is not what
        # either the switch or the rotation comment above says. It is not
        # unreachable in practice: the round is joined with no opening print,
        # but BOUNDARY_BACKFILL_AFTER recovers that print from REST a few
        # seconds later, and from then on SIG PRICE is live and the bands
        # trade. Reproduced with the phase-1 harness at 200s remaining: with
        # the switch ON, phase 2 placed 0 orders and phase 1 still placed one.
        #
        # It belongs ahead of BOTH phases. Everything the round still needs
        # while it is only being observed - the strike latch, the opening
        # print, the status line - has already run above.
        if (config.SKIP_JOINED_ROUND and joined_window is not None
                and active_window == joined_window):
            if skip_logged_window != active_window:
                skip_logged_window = active_window
                nxt = now_et(round_end).strftime("%I:%M%p ET").lstrip("0")
                print(f"{_ts()} [ROUND] Joined this round in progress "
                      f"({exact_remaining:.0f}s left); waiting for the next "
                      f"market at {nxt}.")
            await asyncio.sleep(0.2)
            continue

        # ---- phase 1: price-band entry gated by SIG PRICE -----------------
        # The band still decides whether the selected contract is affordable;
        # fresh Binance direction is the sole authority for the order side.
        band = config.phase1_band(exact_remaining) if config.PHASE1_ENABLED else None
        if (band is not None
                and sampled_wall - last_phase1 >= band[4]):
            last_phase1 = sampled_wall
            _band_start, _band_end, band_lo, band_hi, band_gap = band
            initial_price_side = price_signal(active_window, start_price, lp)
            signal_epoch.observe(initial_price_side)
            if initial_price_side is None:
                print(f"{_ts()} [RISK] phase1 skip: fresh SIG PRICE is neutral or unavailable.")
                await asyncio.sleep(0.2)
                continue
            # BUGFIX: charging BET_SIZE here under-counted real cash by 22%
            # on a measured run, because the broker sizes up to the 5-share
            # venue minimum and the fee lands on top. A cap must reserve the
            # most the entry can cost, which is what entry_cost_ceiling gives.
            entry_ceiling = config.entry_cost_ceiling(band_hi)
            if round_tokens is None:
                fetched = await asyncio.to_thread(
                    market_discovery.get_tokens_for_current_round, active_window)
                # Only a result matching this exact window is worth keeping.
                # Caching a failure would take the whole round dark, so a bad
                # call is simply retried on the next attempt.
                if (fetched and fetched.get("window_start") == active_window
                        and fetched.get("window_end") == round_end):
                    round_tokens = fetched
            tokens = round_tokens
            if (not tokens or tokens.get("window_start") != active_window
                    or tokens.get("window_end") != round_end):
                await asyncio.sleep(0.2)
                continue
            up_id, down_id = tokens["up_token_id"], tokens["down_token_id"]

            # A restart begins with no process-local held set. Refresh both
            # durable risk dimensions as soon as the condition/token mapping
            # is known, before either the cap or complement-leg gate runs.
            round_exposure, held_tokens = _refresh_durable_round_state(
                active_window, tokens["condition_id"],
                round_exposure, held_tokens)
            signal_epoch.initialize_from_durable(held_tokens, up_id, down_id)
            if not _execution_ready(mode, tokens["condition_id"]):
                print(f"{_ts()} [RISK] phase1 skip: private fill stream is not "
                      "LIVE and subscribed to this market.")
                await asyncio.sleep(0.2)
                continue
            if round_exposure + entry_ceiling > config.MAX_ROUND_EXPOSURE + 1e-9:
                await asyncio.sleep(0.2)
                continue

            # Two independent reads of two different tokens, and the band test
            # needs both before it can choose. Run them together: sequentially
            # they cost two full round trips, and the second one's answer is
            # already a round trip staler than the first by the time it lands.
            legs = (("UP", up_id), ("DOWN", down_id))
            books = await asyncio.gather(
                *(asyncio.to_thread(orderbook.get_orderbook, token)
                  for _candidate, token in legs),
                return_exceptions=True)
            for book in books:
                if isinstance(book, asyncio.CancelledError):
                    raise book          # shutdown, not a failed read
            in_band = []
            for (candidate, token), book in zip(legs, books):
                if isinstance(book, BaseException):
                    print(f"{_ts()} [MARKET] phase1 book read failed "
                          f"({candidate}): {type(book).__name__}")
                    continue
                _bids, asks = book
                if not asks:
                    continue
                ask = float(asks[0]["price"])
                if band_lo <= ask <= band_hi:
                    in_band.append((candidate, token, ask))

            if not in_band:
                await asyncio.sleep(0.2)
                continue
            if len(in_band) == 2:
                # Both legs inside the band means the pair sums under $1.
                # That is an arbitrage, not a signal, and taking one leg of it
                # at random is not what this experiment is measuring.
                print(f"{_ts()} [RISK] phase1 skip: both legs in band "
                      f"({in_band[0][2]:.3f} / {in_band[1][2]:.3f}); priced as arbitrage")
                await asyncio.sleep(0.2)
                continue

            side, token, ask = in_band[0]
            if side != initial_price_side:
                print(
                    f"{_ts()} [RISK] phase1 skip: band selected {side} but "
                    f"fresh SIG PRICE is {initial_price_side}."
                )
                await asyncio.sleep(0.2)
                continue
            other_token = down_id if side == "UP" else up_id
            if other_token in held_tokens:
                lock_ok, lock_detail = _pair_lock_permit(
                    tokens["condition_id"], other_token, ask)
                if not lock_ok:
                    print(f"{_ts()} [RISK] phase1 skip: already hold the other "
                          f"leg this round; {lock_detail}")
                    await asyncio.sleep(0.2)
                    continue
                print(f"{_ts()} [PAIR] phase1 completing the pair: {lock_detail}")

            try:
                await asyncio.to_thread(
                    orderbook.validate_buy_liquidity, token, config.BET_SIZE,
                    band_hi, config.MAX_ALLOWED_SPREAD, min_price=band_lo)
            except ValueError as exc:
                print(f"{_ts()} [RISK] phase1 skip: {side} not buyable - {exc}.")
                _append_trade({
                    "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                    "phase": "phase1", "side": side, "amount": config.BET_SIZE,
                    "price_side": "", "book_side": "", "chainlink_side": "",
                    "result": "skipped_unfillable",
                })
                await asyncio.sleep(0.2)
                continue
            except Exception as exc:
                print(f"{_ts()} [MARKET] phase1 probe failed: {type(exc).__name__}: {exc}")
                await asyncio.sleep(0.2)
                continue

            final_lp, _final_lp_ts = price_ws.fresh_snapshot(config.BTC_STALE_AFTER)
            final_price_side = price_signal(active_window, start_price, final_lp)
            signal_epoch.observe(final_price_side)
            if final_price_side is None:
                print(f"{_ts()} [RISK] phase1 skip: SIG PRICE became neutral or stale during validation.")
                await asyncio.sleep(0.2)
                continue
            if final_price_side != side:
                print(
                    f"{_ts()} [RISK] phase1 skip: SIG PRICE changed during validation "
                    f"({side} -> {final_price_side})."
                )
                await asyncio.sleep(0.2)
                continue

            clock_ok, clock_detail, drift = await asyncio.to_thread(
                timer.check_clock, config.CLOB_HOST, config.CLOCK_MAX_DRIFT_SECONDS)
            if mode == "LIVE" and not clock_ok:
                print(f"{_ts()} [RISK] phase1 skip: {clock_detail}.")
                await asyncio.sleep(0.5)
                continue

            # Discovery and two book reads take time; re-sample the boundary
            # immediately before submitting, exactly as the signal path does.
            action_wall = timer.unix()
            if (timer.window_start(action_wall) != active_window
                    or action_wall >= round_end - config.MIN_SECONDS_TO_EXPIRY):
                print(f"{_ts()} [RISK] phase1 skip: round changed during validation.")
                await asyncio.sleep(0.2)
                continue
            if not _execution_ready(mode, tokens["condition_id"]):
                print(f"{_ts()} [RISK] phase1 skip: private fill stream lost "
                      "readiness before submission.")
                await asyncio.sleep(0.2)
                continue

            submit_lp, _submit_lp_ts = price_ws.fresh_snapshot(config.BTC_STALE_AFTER)
            submit_price_side = price_signal(active_window, start_price, submit_lp)
            signal_epoch.observe(submit_price_side)
            if submit_price_side is None:
                print(f"{_ts()} [RISK] phase1 skip: SIG PRICE is neutral or stale before submission.")
                await asyncio.sleep(0.2)
                continue
            if submit_price_side != side:
                print(
                    f"{_ts()} [RISK] phase1 skip: SIG PRICE changed immediately before "
                    f"submission ({side} -> {submit_price_side})."
                )
                await asyncio.sleep(0.2)
                continue

            verb = "Simulating live-book FOK" if mode == "PAPER" else "Placing trade"
            print(f"{_ts()} [BOT] phase1 {verb}: {side} ${config.BET_SIZE} "
                  f"@ ask {ask:.3f} (T-{exact_remaining:.0f}s, window "
                  f"{_band_start}-{_band_end}s, band {band_lo:.2f}-{band_hi:.2f})")
            # The band bounds this order at BOTH ends: without the ceiling a
            # thin best level walks the book past the band's top, and without
            # the floor the walk can fill all the way down to MIN_BUY_PRICE.
            #
            # BUGFIX: only band_hi used to be passed. The floor existed solely
            # in the validate_buy_liquidity check above, and the book moves
            # between that check and the fill - so a leg quoted inside the band
            # could fill below it. Verified against the paper fill engine with
            # the 0.55-0.65 band: an ask of 0.54 and one of 0.40 both filled.
            # Cheaper is not better here; the price fell because the market
            # moved against the side being bought, and the fill lands outside
            # the range the band experiment is measuring.
            ok = await asyncio.to_thread(
                place_trade, side, config.BET_SIZE, up_id, down_id,
                tokens["condition_id"], round_end, band_hi, min_price=band_lo,
                pre_submit_guard=lambda: _fresh_price_permit(
                    active_window, start_price, side, explain=True,
                    signal_observer=signal_epoch.observe))
            if ok:
                round_exposure += entry_ceiling
                held_tokens.add(token)
                signal_epoch.record_accepted(side)
                result = ("paper_filled" if mode == "PAPER" else
                          (polymarket_trade.last_order_status or
                           "accepted_pending_confirmation").lower())
            else:
                result = "rejected_or_unsubmitted"
                reason = polymarket_trade.last_order_error or "unknown"
                print(f"{_ts()} [BOT] phase1 order was NOT placed - reason: {reason}")
            _append_trade({
                "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                "phase": "phase1", "side": side, "amount": config.BET_SIZE,
                "price_side": submit_price_side, "book_side": "", "chainlink_side": "",
                "result": result,
            })
            await asyncio.sleep(0.2)
            continue

        if (config.PHASE2_ENABLED
                and 0 < exact_remaining <= config.TRADE_LAST_SECONDS
                and exact_remaining >= config.MIN_SECONDS_TO_EXPIRY):
            # Keep each signal on one source: Binance start vs Binance now,
            # Chainlink 60s TWAP start vs Chainlink 60s TWAP now.  Mixing a
            # TWAP strike with a spot current value silently flips close calls.
            current_cl = current_chainlink_twap()
            missing = []
            if start_price is None:
                missing.append("Binance boundary print")
            if lp is None:
                missing.append("fresh Binance print")
            if start_chainlink_price is None:
                missing.append("Chainlink boundary TWAP")
            if current_cl is None:
                missing.append("fresh Chainlink TWAP")
            # SIG PRICE owns the order side, so only its inputs can genuinely
            # cancel a round. Chainlink's absence used to cancel one too, which
            # meant a single dropped one-second TWAP observation cost five
            # minutes of trading even though SIG PRICE was ready the whole time.
            blocking = missing
            if config.PHASE2_PARTIAL_SIGNALS:
                blocking = [item for item in missing if "Binance" in item]
                abstaining = [item for item in missing if item not in blocking]
                if abstaining and not blocking and skip_logged_window != active_window:
                    skip_logged_window = active_window
                    print(f"{_ts()} [RISK] SIG CHAINLINK abstains this round "
                          f"(missing {', '.join(abstaining)}); trading continues "
                          f"on the signals that are ready.")
            if blocking:
                missing = blocking
                structural = [item for item in missing if "boundary" in item]
                if structural:
                    if skip_logged_window != active_window:
                        skip_logged_window = active_window
                        next_open = now_et(round_end).strftime("%I:%M%p ET").lstrip("0")
                        print(
                            f"{_ts()} [RISK] No order this round: missing "
                            f"{', '.join(missing)}."
                        )
                        print(
                            f"{_ts()} [ROUND] Opening prices are captured only "
                            f"in the first 5s after the 5-minute boundary. Keep "
                            f"the bot running through {next_open}; the next "
                            f"open is the first trade chance."
                        )
                else:
                    print(f"{_ts()} [RISK] No order: missing {', '.join(missing)}.")
                await asyncio.sleep(0.2)
                continue

            print(f"{_ts()} [BOT] Trade window ({exact_remaining:.2f}s left) - validating live state...")

            if round_tokens is None:
                fetched = await asyncio.to_thread(
                    market_discovery.get_tokens_for_current_round, active_window)
                if (fetched and fetched.get("window_start") == active_window
                        and fetched.get("window_end") == round_end):
                    round_tokens = fetched
            tokens = round_tokens
            if not tokens:
                print(f"{_ts()} [BOT] WARN: No market tokens - cannot place order.")
                await _cooldown(1.0)
                continue
            if (tokens.get("window_start") != active_window
                    or tokens.get("window_end") != round_end):
                print(f"{_ts()} [RISK] No order: discovered market does not match sampled round.")
                await _cooldown(1.0)
                continue

            up_id = tokens["up_token_id"]
            down_id = tokens["down_token_id"]
            ob_id = tokens.get("orderbook_token_id") or up_id
            # Accepted live submissions and confirmed paper fills survive a
            # restart. Restore both the cap and complement guard before any
            # phase-2 decision can reach submission.
            round_exposure, held_tokens = _refresh_durable_round_state(
                active_window, tokens["condition_id"],
                round_exposure, held_tokens)
            if not _execution_ready(mode, tokens["condition_id"]):
                print(f"{_ts()} [RISK] No order: private fill stream is not "
                      "LIVE and subscribed to this market.")
                await asyncio.sleep(0.2)
                continue

            print(f"{_ts()} [BOT] Market tokens found for this round.")

            running = current_cl
            ptb = start_chainlink_price
            ptb_s = f"${ptb:,.2f}" if ptb is not None else "N/A"
            run_s = f"${running:,.2f}" if running is not None else "N/A"
            print(
                f"{_ts()} [BOT] Price-to-beat (round start) = {ptb_s} | "
                f"Running price = {run_s} "
                f"(official Chainlink 60s TWAP source)"
            )

            price_side = price_signal(active_window, start_price, lp)
            if config.SIGNAL_DECISION_RULE == "price":
                signal_epoch.observe(price_side)
            signal_epoch.initialize_from_durable(held_tokens, up_id, down_id)
            book_side = None
            chainlink_side = chainlink_signal(
                active_window, start_chainlink_price, current_cl)
            try:
                bids, asks = await asyncio.to_thread(orderbook.get_orderbook, ob_id)
                book_side = orderbook.liquidity_signal(bids, asks)
            except Exception as exc:
                print(f"{_ts()} [MARKET] Orderbook rejected: {type(exc).__name__}: {exc}")
                await _cooldown(1.0)
                continue

            diagnostic_side = strategy.final_decision(
                price_side, book_side, chainlink_side)
            if price_side is None:
                print(f"{_ts()} [RISK] No order: fresh SIG PRICE is neutral or unavailable.")
                await asyncio.sleep(0.2)
                continue
            # Which signal picks the side is SIGNAL_DECISION_RULE's job, and
            # _authority_side is the single place it is read - the same call
            # the three re-validation gates below make. Under "price" the
            # book and Chainlink stay diagnostics and never override SIG
            # PRICE; under "minority" the order follows whichever side is
            # outvoted; under "final" it follows the confirmed side (PRICE
            # and BOOK agreeing, else CHAINLINK siding with one of them).
            #
            # The fresh-SIG-PRICE gate above still runs first under every
            # rule, so a stale or neutral price feed refuses the round either
            # way - only the choice of side moves here, never the decision
            # about whether it is safe to trade at all.
            side = _authority_side(price_side, book_side, chainlink_side)
            # Track the decision, not SIG PRICE: this is what a later flip
            # has to differ from for the complement to be permitted.
            if config.SIGNAL_DECISION_RULE != "price":
                signal_epoch.observe(side)
            if side is None:
                print(f"{_ts()} [RISK] No order: the "
                      f"{config.SIGNAL_DECISION_RULE} rule names no side "
                      f"(signals tied or unanimous-neutral).")
                await asyncio.sleep(0.2)
                continue

            print(
                f"{_ts()} [SIGNAL] price={price_side} book={book_side or 'n/a'} "
                f"chainlink={chainlink_side} -> diagnostic={diagnostic_side or 'n/a'} "
                f"ORDER SIDE={side}"
            )

            # A repeating 3-confirmation cycle: two grow the primary side
            # (tapering down, as one more agreement with an already-priced-in
            # signal is weaker evidence than the first), the third buys the
            # complement instead, funded by what would otherwise have kept
            # piling onto an increasingly uncertain position - then the cycle
            # restarts. Backtested against 111 real settled rounds before
            # being wired in: capping the hedge at 1-in-3 instead of letting
            # it run unbounded for the rest of the round turned +$1.50 total
            # (the unbounded version) into +$71.10, because an unbounded
            # hedge quietly inverts a long, correctly-held round into a loss.
            #
            # Every slot in the cycle - not just the hedge one - anchors to
            # taper_primary_side (the side entry 1 actually bought) rather
            # than to the live `side`, which is recomputed fresh from the
            # signal every loop. Without that anchor the slots inside one
            # cycle would each grow or hedge whatever the signal happened to
            # say at that instant, instead of consistently building on the
            # position already established.
            #
            # The anchor holds a cycle together; it does not outlive the
            # signal that created it. The signal is what selects the trade,
            # so when it flips away from the anchored side the cycle is
            # retired and a new one starts from entry 1 on the new side -
            # see the flip check below.
            entry_amount = config.BET_SIZE
            entry_side = side
            is_taper_hedge = False
            taper_anchor_side = None
            taper_active = config.TAPER_HEDGE_ENABLED and mode == "PAPER"
            if taper_active:
                # Follow the signal. A cycle is a bet on one side; once the
                # signal has left that side, continuing to grow it - or to
                # buy its complement as a "hedge" for a position the bot no
                # longer believes in - is acting on a decision that has
                # already been withdrawn. So a flip RE-ANCHORS the cycle to
                # the side the signal now names.
                #
                # It does NOT restart the count. An earlier version reset
                # taper_count to 0 here, which looked harmless and quietly
                # destroyed the cadence: the counter only advances on a FILL,
                # so every flip sent the cycle back to slot 0 and slot 2 -
                # the opposite-side buy - was rarely reached at all. Measured
                # over 915 filled entries that produced 4.7 signal-side buys
                # per opposite-side buy instead of the intended 2. Keeping
                # the position makes the cadence hold at 2:1 whatever the
                # signal does. Shares already bought are left alone either
                # way: this changes what happens next, not what filled.
                if taper_primary_side is not None and side != taper_primary_side:
                    print(f"{_ts()} [TAPER] Signal flipped "
                          f"{taper_primary_side} -> {side} at cycle slot "
                          f"{taper_count % 3 + 1}/3; re-anchoring, cadence kept.")
                    taper_primary_side = side
                taper_anchor_side = taper_primary_side or side
                cycle_pos = taper_count % 3
                if cycle_pos == 0:
                    entry_amount = config.TAPER_ENTRY1_USD
                    entry_side = taper_anchor_side
                elif cycle_pos == 1:
                    entry_amount = config.TAPER_ENTRY2_USD
                    entry_side = taper_anchor_side
                else:
                    entry_amount = config.TAPER_HEDGE_INCREMENT_USD
                    entry_side = "DOWN" if taper_anchor_side == "UP" else "UP"
                    is_taper_hedge = True

            # The price band this particular order may fill in. A PRIMARY
            # entry - the leg that follows the signal - may be held to a
            # tighter band than the account allows; a taper hedge leg keeps
            # the account bounds. Measured over 620 settled fills the two
            # behave nothing alike: primary paid 0.577 for a 52% hit rate
            # (edge -0.058), the hedge paid 0.418 for 54% (edge +0.118), and
            # the 0.60-0.80 band alone carried 52% of turnover at -0.05 edge.
            # See config.PRIMARY_ENTRY_MIN_PRICE for the full numbers.
            #
            # Both brokers may only TIGHTEN these per-order bounds against
            # MIN/MAX_BUY_PRICE, never loosen them, so the hedge's band is
            # exactly the account band - it cannot be widened from here.
            entry_max_price = (config.MAX_BUY_PRICE if is_taper_hedge
                               else config.PRIMARY_ENTRY_MAX_PRICE)
            entry_min_price = (config.MIN_BUY_PRICE if is_taper_hedge
                               else config.PRIMARY_ENTRY_MIN_PRICE)

            entry_ceiling = config.entry_cost_ceiling(config.MAX_BUY_PRICE)
            if round_exposure + entry_ceiling > config.MAX_ROUND_EXPOSURE + 1e-9:
                print(
                    f"{_ts()} [RISK] Round exposure cap reached "
                    f"(${round_exposure:.2f}/${config.MAX_ROUND_EXPOSURE:.2f})."
                )
                await _cooldown()
                continue

            clock_ok, clock_detail, drift = await asyncio.to_thread(
                timer.check_clock, config.CLOB_HOST, config.CLOCK_MAX_DRIFT_SECONDS)
            if mode == "LIVE":
                if not clock_ok:
                    print(f"{_ts()} [RISK] No order: {clock_detail}.")
                    await asyncio.sleep(0.5)
                    continue
            elif not clock_ok and drift is None and not timer.clock_measured():
                print(f"{_ts()} [RISK] No order: {clock_detail}.")
                await asyncio.sleep(0.5)
                continue

            # Discovery, book reads and clock I/O take time. Re-sample the
            # boundary immediately before any authenticated action.
            action_wall = timer.unix()
            if (timer.window_start(action_wall) != active_window
                    or action_wall >= round_end - config.MIN_SECONDS_TO_EXPIRY):
                print(f"{_ts()} [RISK] No order: round changed during validation.")
                await asyncio.sleep(0.2)
                continue

            if config.CANCEL_OPEN_BEFORE_TRADE:
                action = "Clearing simulated orders" if mode == "PAPER" else "Closing any open orders first"
                print(f"{_ts()} [BOT] {action} (so new order can go through)...")
                cancelled = await asyncio.to_thread(cancel_all_open_orders)
                if not cancelled:
                    reason = polymarket_trade.last_order_error or "cancel-all failed"
                    print(f"{_ts()} [RISK] No order because cancellation failed: {reason}")
                    await asyncio.sleep(0.5)
                    continue

            action_wall = timer.unix()
            if (timer.window_start(action_wall) != active_window
                    or action_wall >= round_end - config.MIN_SECONDS_TO_EXPIRY):
                print(f"{_ts()} [RISK] No order: round changed before submission.")
                await asyncio.sleep(0.2)
                continue

            # Discovery and clock/cancellation I/O can take several seconds.
            # Re-sample every decision input after that I/O, then bound the
            # validation-to-submit interval.  Never place an order using a
            # signal that was fresh only at the beginning of the pipeline.
            validation_started = time.monotonic()
            final_lp, _final_lp_ts = price_ws.fresh_snapshot(config.BTC_STALE_AFTER)
            final_cl = current_chainlink_twap()
            if final_lp is None or final_cl is None:
                print(f"{_ts()} [RISK] No order: a price feed became stale during validation.")
                await asyncio.sleep(0.2)
                continue
            try:
                final_bids, final_asks = await asyncio.to_thread(
                    orderbook.get_orderbook, ob_id)
                # Stamp it: the depth/spread probe below can reuse this book
                # instead of paying a second round trip for the same leg, but
                # only while it is provably the same token and still fresh.
                final_book_token, final_book_mono = ob_id, time.monotonic()
                final_book_side = orderbook.liquidity_signal(final_bids, final_asks)
            except Exception as exc:
                print(
                    f"{_ts()} [MARKET] Final orderbook validation failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                await _cooldown(1.0)
                continue
            final_price_side = price_signal(active_window, start_price, final_lp)
            final_chainlink_side = chainlink_signal(
                active_window, start_chainlink_price, final_cl)
            final_authority_side = _authority_side(
                final_price_side, final_book_side, final_chainlink_side)
            signal_epoch.observe(final_authority_side)
            _final_diagnostic_side = strategy.final_decision(
                final_price_side, final_book_side, final_chainlink_side)
            if final_price_side is None:
                print(f"{_ts()} [RISK] No order: SIG PRICE became neutral during validation.")
                await asyncio.sleep(0.2)
                continue
            # Compare against the AUTHORITY side, not raw SIG PRICE: unless
            # SIGNAL_DECISION_RULE is "price", `side` was chosen from all
            # three signals, and comparing it to SIG PRICE alone rejects
            # nearly every genuine order (SIG PRICE alone rarely equals the
            # chosen side) while letting a stale pick through whenever SIG
            # PRICE alone happens to coincide with it.
            if final_authority_side != side:
                print(
                    f"{_ts()} [RISK] No order: deciding signal changed during "
                    f"validation ({side} -> {final_authority_side})."
                )
                await asyncio.sleep(0.2)
                continue

            other_token = down_id if side == "UP" else up_id
            if taper_active:
                if is_taper_hedge:
                    # entry_side is the complement of taper_anchor_side, not
                    # of the live `side` - see where entry_side was computed.
                    # The point of the guard is that a complement bought
                    # against nothing held is not a hedge, just an unrelated
                    # naked position.
                    #
                    # It deliberately asks whether ANY leg is held this round,
                    # not whether one specific token is. Since a flip
                    # re-anchors the cycle without restarting it, the anchored
                    # side at slot 3 can be a side this round never bought -
                    # and testing that token deadlocked the cycle: the slot
                    # could never fill, so the counter could never advance
                    # past it, so the cadence stopped dead on the first flip
                    # that landed here. Slot 3 is only reachable after two
                    # fills, so this stays a genuine safety net rather than a
                    # formality, while no longer depending on WHICH side those
                    # fills were on.
                    if not held_tokens:
                        print(f"{_ts()} [RISK] No order: taper hedge has "
                              f"nothing to hedge yet (no leg filled this "
                              f"round).")
                        await _cooldown()
                        continue
                # else: this attempt grows the already-anchored primary side
                # further. Whatever else this round holds - a hedge leg on
                # the complement from an earlier cycle - is irrelevant here:
                # this is not a fresh, possibly-accidental complement
                # purchase, it is more of the same anchored position, so the
                # "already hold the other leg" guard below (built for a
                # different scenario: an unplanned pair) does not apply.
            elif other_token in held_tokens:
                flip_allowed = False
                flip_detail = "PAPER signal-flip mode is disabled"
                flips_enabled = (config.PAPER_ALLOW_SIGNAL_FLIPS if mode == "PAPER"
                                 else config.LIVE_ALLOW_SIGNAL_FLIPS)
                if flips_enabled:
                    flip_allowed, flip_detail = signal_epoch.paper_flip_permit(side)
                lock_ok = False
                lock_detail = ("pair-lock is disabled"
                               if not config.PAIR_LOCK_ENABLED
                               else "pair-lock not evaluated")
                if not flip_allowed and config.PAIR_LOCK_ENABLED:
                    # Reached only when the complement is already held, so this
                    # extra book read stays off the common path. The selected
                    # leg is not necessarily ob_id, so final_asks cannot price
                    # it: a pair checked against the wrong leg is not checked.
                    lock_asks = None
                    try:
                        _lock_bids, lock_asks = await asyncio.to_thread(
                            orderbook.get_orderbook,
                            up_id if side == "UP" else down_id)
                    except Exception as exc:
                        lock_detail = f"book read failed ({type(exc).__name__})"
                    if lock_asks:
                        lock_ok, lock_detail = _pair_lock_permit(
                            tokens["condition_id"], other_token,
                            float(lock_asks[0]["price"]))
                    elif lock_asks is not None:
                        lock_detail = "no ask on the selected leg"
                # Multi-signal rounds are expected to hold both legs, so the
                # guard cannot also be the thing that stops the next cycle
                # trading. It now runs in LIVE too, so standing the guard down
                # unconditionally is no longer acceptable: the complement is
                # allowed only where the pair-lock proves the finished pair
                # cannot lose. Unconditional pairs were measured at -$0.22 each
                # at the 1.0100 overround this book actually runs.
                # With the lock ON, a complement is allowed only where the pair
                # is provably profitable. With it OFF the operator has chosen to
                # take pairs unconditionally, so the guard stands down entirely
                # - "lock disabled" must mean no restriction, not no pairs.
                multi_allowed = config.PHASE2_MULTI_SIGNAL and (
                    lock_ok or not config.PAIR_LOCK_ENABLED)
                if not (flip_allowed or lock_ok or multi_allowed):
                    print(
                        f"{_ts()} [RISK] No order: already hold the other leg of "
                        f"this market; {flip_detail}; {lock_detail}."
                    )
                    await _cooldown()
                    continue
                if lock_ok:
                    print(f"{_ts()} [PAIR] completing the pair: {lock_detail}")
                elif multi_allowed:
                    print(f"{_ts()} [MULTI] complement guard stood down by "
                          f"PHASE2_MULTI_SIGNAL")

            # Each remaining signal trades its own side, BEFORE the price leg's
            # own probe runs: a price leg that cannot fill must not silently
            # suppress the other two. When a signal disagrees with
            # SIG PRICE this deliberately buys the complement, so the guard is
            # stood down here by explicit configuration. PAPER only: two venue
            # orders are not an atomic pair and LIVE must never hold both legs
            # by accident. The exposure cap is still enforced per leg.
            extras_started = time.monotonic()
            if config.PHASE2_MULTI_SIGNAL:
                for source, extra_side in (("book", final_book_side),
                                           ("chainlink", final_chainlink_side)):
                    if extra_side not in ("UP", "DOWN") or extra_side == side:
                        continue
                    extra_ceiling = config.entry_cost_ceiling(config.MAX_BUY_PRICE)
                    if round_exposure + extra_ceiling > config.MAX_ROUND_EXPOSURE + 1e-9:
                        print(f"{_ts()} [MULTI] {source} leg skipped: round exposure "
                              f"cap (${round_exposure:.2f}/"
                              f"${config.MAX_ROUND_EXPOSURE:.2f}).")
                        continue
                    extra_token = up_id if extra_side == "UP" else down_id
                    # This leg disagrees with the order side, so if that side is
                    # held it is the complement - and completing a pair is only
                    # worth doing when both entries plus both fees stay under
                    # the $1.00 the pair redeems for. Same rule in PAPER and
                    # LIVE so the paper run rehearses what live will do.
                    held_side_token = up_id if side == "UP" else down_id
                    if config.PAIR_LOCK_ENABLED and held_side_token in held_tokens:
                        pair_ok, pair_detail = _pair_lock_permit(
                            tokens["condition_id"], held_side_token,
                            config.MAX_BUY_PRICE)
                        if not pair_ok:
                            print(f"{_ts()} [MULTI] {source} leg skipped: would "
                                  f"complete a losing pair; {pair_detail}")
                            _append_trade({
                                "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                                "phase": f"phase2-{source}",
                                "side": extra_side,
                                "amount": config.BET_SIZE,
                                "price_side": final_price_side or "",
                                "book_side": final_book_side or "",
                                "chainlink_side": final_chainlink_side or "",
                                "result": "skipped_pair_would_lose",
                            })
                            continue
                    # SIG CHAINLINK reads from memory, so it can re-check inside
                    # the broker's pre-submit guard exactly like SIG PRICE does.
                    # SIG BOOK cannot: that guard runs while the broker holds
                    # its state lock immediately before the durable fill, and a
                    # REST read there (409ms median, up to 16s if it retries)
                    # would age the already-quoted book past
                    # ORDERBOOK_MAX_AGE_SECONDS with nothing left to revalidate
                    # it. Re-check the book here instead, off the lock.
                    extra_guard = None
                    if source == "chainlink":
                        extra_guard = (
                            lambda _e=extra_side: _fresh_signal_permit(
                                "chainlink", _e, round_key=active_window,
                                chainlink_start=start_chainlink_price))
                    else:
                        try:
                            _gb, _ga = await asyncio.to_thread(
                                orderbook.get_orderbook, ob_id)
                            still = orderbook.liquidity_signal(_gb, _ga)
                        except Exception as exc:
                            still = None
                            print(f"{_ts()} [MULTI] book leg skipped: re-check "
                                  f"failed ({type(exc).__name__}).")
                        if still != extra_side:
                            if still is not None:
                                print(f"{_ts()} [MULTI] book leg skipped: SIG BOOK "
                                      f"moved {extra_side} -> {still or 'neutral'}.")
                            _append_trade(
                                {
                                    "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                                    "phase": "phase2-book",
                                    "side": extra_side,
                                    "amount": config.BET_SIZE,
                                    "price_side": final_price_side or "",
                                    "book_side": final_book_side or "",
                                    "chainlink_side": final_chainlink_side or "",
                                    "result": "skipped_signal_moved",
                                }
                            )
                            continue
                    try:
                        await asyncio.to_thread(
                            orderbook.validate_buy_liquidity, extra_token,
                            config.BET_SIZE, config.MAX_BUY_PRICE,
                            config.MAX_ALLOWED_SPREAD,
                            min_price=config.MIN_BUY_PRICE)
                    except ValueError as exc:
                        print(f"{_ts()} [MULTI] {source} leg skipped: {extra_side} "
                              f"is not buyable - {exc}.")
                        extra_result = "skipped_unfillable"
                    except Exception as exc:
                        print(f"{_ts()} [MULTI] {source} leg probe failed: "
                              f"{type(exc).__name__}: {exc}")
                        extra_result = "skipped_unfillable"
                    else:
                        print(f"{_ts()} [MULTI] SIG {source.upper()}={extra_side} "
                              f"differs from order side {side}; placing its own "
                              f"leg (complement guard stood down)")
                        extra_ok = await asyncio.to_thread(
                            place_trade, extra_side, config.BET_SIZE,
                            up_id, down_id, tokens["condition_id"], round_end,
                            pre_submit_guard=extra_guard)
                        if extra_ok:
                            round_exposure += extra_ceiling
                            held_tokens.add(extra_token)
                            extra_result = "paper_filled"
                        else:
                            extra_result = "rejected_or_unsubmitted"
                            print(f"{_ts()} [MULTI] {source} leg NOT placed - "
                                  f"{polymarket_trade.last_order_error or 'unknown'}")
                    _append_trade(
                        {
                            "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                            "phase": f"phase2-{source}",
                            "side": extra_side,
                            "amount": config.BET_SIZE,
                            "price_side": final_price_side or "",
                            "book_side": final_book_side or "",
                            "chainlink_side": final_chainlink_side or "",
                            "result": extra_result,
                        }
                    )
            # Near expiry the selected token can lose all offers.  Book
            # liquidity is transient, so a failed probe skips this attempt
            # only; the next scheduled attempt probes the live book again.
            selected_token = up_id if side == "UP" else down_id
            # ob_id is the UP leg, so a DOWN order must still fetch its own
            # book. The age test is what makes this safe on every path: the
            # pair-lock and multi-signal branches each add a venue round trip
            # between the two reads, and any of them pushes the stamp past
            # the window, so the probe falls back to a fresh fetch by itself
            # rather than relying on anyone tracing those branches by hand.
            reuse_book = None
            if (selected_token == final_book_token
                    and time.monotonic() - final_book_mono
                    <= _FINAL_BOOK_REUSE_SECONDS):
                reuse_book = (final_bids, final_asks)
            try:
                selected_bids, selected_asks = await asyncio.to_thread(
                    orderbook.validate_buy_liquidity,
                    selected_token,
                    entry_amount, entry_max_price, config.MAX_ALLOWED_SPREAD,
                    min_price=entry_min_price, book=reuse_book)
            except ValueError as exc:
                # This checks side's own buyability - the right gate for a
                # plain entry, or a taper primary-slot one still on the live
                # side. A taper hedge leg (or a primary-slot one anchored to
                # a side the live signal has since left) targets entry_side,
                # not side, so side failing this check says nothing about
                # whether entry_side is buyable: the dedicated taper
                # liquidity probe right before submission is the real gate
                # for that order, and this failure is not fatal to it. The
                # book read below is still wanted for the SIG BOOK diagnostic
                # (unrelated to which side actually gets bought), so only a
                # relevant failure aborts the whole attempt.
                if entry_side == side:
                    print(
                        f"{_ts()} [RISK] No order this attempt: "
                        f"{side} is not buyable - {exc}."
                    )
                    _append_trade(
                        {
                            "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                            "phase": "phase2",
                            "side": entry_side,
                            "amount": entry_amount,
                            "price_side": final_price_side or "",
                            "book_side": final_book_side or "",
                            "chainlink_side": final_chainlink_side or "",
                            "result": "skipped_unfillable",
                        }
                    )
                    await _cooldown()
                    continue
                selected_bids, selected_asks = (), ()
            except Exception as exc:
                print(f"{_ts()} [MARKET] Liquidity probe failed: {type(exc).__name__}: {exc}")
                await _cooldown(1.0)
                continue

            validation_limit = min(
                config.BTC_STALE_AFTER,
                config.TWAP_STALE_AFTER,
                config.ORDERBOOK_MAX_AGE_SECONDS,
            )
            # The multi-signal legs run inside this window and each costs a
            # book read, a probe and a modeled fill - roughly 625ms apiece.
            # Left in, two of them eat 1.25s of a 3s budget and the price leg
            # loses its own trade to work done for other signals. They are
            # independent legs with their own guards, and everything the price
            # leg uses is re-read below at submission, so their time is
            # excluded rather than charged to it.
            validation_started += time.monotonic() - extras_started
            validation_age = time.monotonic() - validation_started
            if validation_age > validation_limit:
                print(
                    f"{_ts()} [RISK] No order: validation took {validation_age:.3f}s "
                    f"(limit {validation_limit:.3f}s)."
                )
                await asyncio.sleep(0.2)
                continue

            submit_lp, _submit_lp_ts = price_ws.fresh_snapshot(config.BTC_STALE_AFTER)
            submit_cl = current_chainlink_twap()
            if submit_lp is None or submit_cl is None:
                print(f"{_ts()} [RISK] No order: a price feed went stale before submission.")
                await asyncio.sleep(0.2)
                continue
            submit_book_side = (
                orderbook.liquidity_signal(selected_bids, selected_asks)
                if side == "UP" else final_book_side
            )
            submit_price_side = price_signal(active_window, start_price, submit_lp)
            submit_chainlink_side = chainlink_signal(
                active_window, start_chainlink_price, submit_cl)
            submit_authority_side = _authority_side(
                submit_price_side, submit_book_side, submit_chainlink_side)
            signal_epoch.observe(submit_authority_side)
            _submit_diagnostic_side = strategy.final_decision(
                submit_price_side, submit_book_side, submit_chainlink_side)
            if submit_price_side is None:
                print(f"{_ts()} [RISK] No order: SIG PRICE is neutral immediately before submission.")
                await asyncio.sleep(0.2)
                continue
            # See the final-validation guard above: compare the AUTHORITY
            # side, not raw SIG PRICE, so this check means the same thing
            # under every SIGNAL_DECISION_RULE.
            if submit_authority_side != side:
                print(
                    f"{_ts()} [RISK] No order: deciding signal changed immediately "
                    f"before submission ({side} -> {submit_authority_side})."
                )
                await asyncio.sleep(0.2)
                continue
            price_side = submit_price_side
            book_side = submit_book_side
            chainlink_side = submit_chainlink_side

            action_wall = timer.unix()
            if (timer.window_start(action_wall) != active_window
                    or action_wall >= round_end - config.MIN_SECONDS_TO_EXPIRY):
                print(f"{_ts()} [RISK] No order: round changed after final validation.")
                await asyncio.sleep(0.2)
                continue
            if not _execution_ready(mode, tokens["condition_id"]):
                print(f"{_ts()} [RISK] No order: private fill stream lost "
                      "readiness before submission.")
                await asyncio.sleep(0.2)
                continue

            # The cap was checked before the multi-signal legs ran, and each of
            # those spends against the same round budget. Without re-checking
            # here the price leg can push the round up to two extra entries
            # past MAX_ROUND_EXPOSURE - the exact limit this check exists to
            # enforce. Cheap to repeat, and it is the last point where it can
            # still be honoured.
            if round_exposure + entry_ceiling > config.MAX_ROUND_EXPOSURE + 1e-9:
                print(
                    f"{_ts()} [RISK] No order: round exposure cap reached after "
                    f"the multi-signal legs "
                    f"(${round_exposure:.2f}/${config.MAX_ROUND_EXPOSURE:.2f})."
                )
                await _cooldown()
                continue

            # entry_side can differ from side above - a hedge leg always
            # targets the complement, and an anchored primary-slot entry
            # (cycle 2+) stays on taper_anchor_side even if the live signal
            # has since wobbled to the other side. Either way, the earlier
            # liquidity probe validated side's own token, not necessarily
            # the one about to be submitted, so it is checked fresh here,
            # right before submission, same as any other pre-submit probe.
            if entry_side != side:
                order_token = up_id if entry_side == "UP" else down_id
                try:
                    await asyncio.to_thread(
                        orderbook.validate_buy_liquidity,
                        order_token,
                        entry_amount, entry_max_price, config.MAX_ALLOWED_SPREAD,
                        min_price=entry_min_price)
                except ValueError as exc:
                    print(
                        f"{_ts()} [RISK] No order this attempt: "
                        f"{entry_side} is not buyable - {exc}."
                    )
                    await _cooldown()
                    continue
                except Exception as exc:
                    print(f"{_ts()} [MARKET] Taper liquidity probe failed: "
                          f"{type(exc).__name__}: {exc}")
                    await _cooldown(1.0)
                    continue

            verb = "Simulating live-book FOK" if mode == "PAPER" else "Placing trade"
            tag = " [TAPER HEDGE]" if is_taper_hedge else ""
            print(f"{_ts()} [BOT]{tag} {verb}: {entry_side} ${entry_amount}")
            ok = await asyncio.to_thread(
                place_trade, entry_side, entry_amount, up_id, down_id,
                tokens["condition_id"], round_end,
                entry_max_price, entry_min_price,
                pre_submit_guard=lambda: _fresh_price_permit(
                    active_window, start_price, side, explain=True,
                    signal_observer=signal_epoch.observe,
                    # Recompute SIG BOOK from its original reference token.
                    # The execution token can be DOWN; its depth is not the
                    # UP-oriented vote that selected this order.
                    book_token=ob_id,
                    chainlink_start=start_chainlink_price))
            if ok:
                round_exposure += entry_ceiling
                if taper_active and taper_count == 0:
                    # Lock in what entry 1 actually bought - every later
                    # taper decision this round (including which side any
                    # hedge targets) anchors to this, not a live signal that
                    # can keep moving after the position is already built.
                    taper_primary_side = entry_side
                taper_count += 1
                # Shared with phase 1 and durable restart recovery. LIVE and
                # default PAPER block the complement; the explicit PAPER
                # experiment consults the accepted-side epoch above. A taper
                # hedge fill marks its own (complement) token held, same as
                # any other accepted leg.
                held_tokens.add(up_id if entry_side == "UP" else down_id)
                signal_epoch.record_accepted(side)
                if mode == "PAPER":
                    result = "paper_filled"
                else:
                    result = (polymarket_trade.last_order_status or
                              "accepted_pending_confirmation").lower()
            else:
                result = "rejected_or_unsubmitted"
            if not ok:
                # Read the live module attribute. Importing this immutable
                # string directly leaves us holding its original None value.
                reason = polymarket_trade.last_order_error or "unknown"
                hint = ""
                if reason and "not enough balance" in reason.lower():
                    hint = " - Deposit USDC on Polygon and enable trading at polymarket.com"
                elif reason and "future-dated" in reason.lower():
                    hint = (
                        " - Local clock is behind the CLOB; sync Windows Time "
                        "(start the service, then `w32tm /resync`) and restart"
                    )
                print(f"{_ts()} [BOT] Order was NOT placed - reason: {reason}{hint}")

            _append_trade(
                {
                    "time_et": now_et().strftime("%b %d %H:%M:%S ET"),
                    "phase": "phase2-hedge" if is_taper_hedge else "phase2",
                    "side": entry_side,
                    "amount": entry_amount,
                    "price_side": price_side or "",
                    "book_side": book_side or "",
                    "chainlink_side": chainlink_side or "",
                    "result": result,
                }
            )
            if (ok and mode == "LIVE"
                    and not (polymarket_trade.last_order_receipt or {}).get(
                        "accounting_journaled")):
                print(f"{_ts()} [RISK] CRITICAL: order matched but its accounting authorization was not durable; stopping.")
                stop_event.set()

            print(
                f"{_ts()} [BOT] Trade call done ({result}). Sleeping "
                f"{config.TRADE_INTERVAL_SECONDS:g}s then continuing."
            )
            await _cooldown()
            continue

        await asyncio.sleep(0.2)


async def main():
    """Canonical safe entrypoint: paper mode unless ``run_feeds --live``."""
    from run_feeds import run
    await run(dash=False, paper=True)


if __name__ == "__main__":
    from run_feeds import run_quietly
    raise SystemExit(run_quietly(main()))
