"""Runtime configuration with fail-fast validation."""
import math
import os
import re
import stat
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv


_ENV_PATH = Path(__file__).resolve().parent / ".env"
if _ENV_PATH.exists():
    if _ENV_PATH.is_symlink() or not _ENV_PATH.is_file():
        raise PermissionError(".env must be a regular, non-symlink file")
    # POSIX makes private-file permissions directly inspectable.  Windows
    # operators must apply the ACL documented in SECURITY.md.
    if os.name != "nt" and stat.S_IMODE(_ENV_PATH.stat().st_mode) & 0o077:
        raise PermissionError(".env contains live credentials and must have mode 0600")
load_dotenv(_ENV_PATH, override=False, encoding="utf-8")


def _env_text(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def _env_float(name: str, default: str) -> float:
    raw = _env_text(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a number") from exc


def _env_int(name: str, default: str | None = None) -> int | None:
    raw = _env_text(name, default)
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bool(name: str, default: bool | None = False) -> bool | None:
    raw = _env_text(name)
    if raw in (None, ""):
        return default
    value = raw.lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off")

SYMBOL = "BTCUSDT"
# $2.50 is not a round number by accident: the venue minimum is 5 shares, and
# at the top of the phase-1 band (0.50) that is exactly $2.50. Anything less
# and the engine sizes the order up at the most expensive price in the band.
# It is also the largest size whose bad-case drawdown fits the paper balance.
BET_SIZE = _env_float("BET_SIZE", "2.50")
# Phase 2 owns the final stretch only; phase 1 owns everything before it.
TRADE_LAST_SECONDS = _env_int("TRADE_LAST_SECONDS", "120")
if TRADE_LAST_SECONDS is None:
    raise ValueError("TRADE_LAST_SECONDS cannot be empty")
TRADE_INTERVAL_SECONDS = _env_float("TRADE_INTERVAL_SECONDS", "6")
MAX_BUY_PRICE = _env_float("MAX_BUY_PRICE", "0.90")
# Momentum gate on SIG PRICE, in basis points of the opening print. The
# Binance move from the open must be at least this large before it votes at
# all. strategy.decide has always returned a full-confidence UP/DOWN for any
# non-zero move, so a $0.01 drift on $79,000 (0.0013bps) selected a side as
# firmly as a $200 run - and paid the same ask for it. MOMENTUM on the
# dashboard has displayed that magnitude all along without anything acting
# on it; this is the knob that acts on it.
#
# 0 = off, exactly the historical behaviour. Do NOT guess a value: the
# journal (signal_journal.py analyze) measures accuracy by move size, and at
# the time of writing it held 7 rounds against the ~1,111 it says are needed
# to read a 3-point edge.
SIG_PRICE_MIN_MOVE_BPS = _env_float("SIG_PRICE_MIN_MOVE_BPS", "0")
MIN_BUY_PRICE = _env_float("MIN_BUY_PRICE", "0.20")
# ---- primary-entry price band --------------------------------------------
# MIN/MAX_BUY_PRICE are the ACCOUNT-wide bounds: the widest any order may go.
# These two narrow that band for a PRIMARY phase-2 entry only - the leg that
# follows the signal - and leave a taper hedge leg on the account bounds.
#
# Why the split. Measured over 620 settled fills (Aug 26 - Sep 09), the two
# leg types are priced completely differently for almost the same hit rate:
#
#     PRIMARY  521 fills  avg price 0.577  hit 52%  edge -0.058  -12.3%
#     HEDGE     99 fills  avg price 0.418  hit 54%  edge +0.118  +24.4%
#
# Bucketed by price paid, the hit rate tracked the price to within a couple
# of points in every bucket - the book is efficiently priced and there is no
# selection edge to harvest - so what decides the result is what gets paid.
# 0.60-0.80 alone held 52% of turnover at -0.047/-0.060 edge. A ceiling on
# the primary leg attacks that directly; a floor keeps it out of the deep
# longshots, where 4 primary fills under 0.15 returned -100%.
#
# The hedge is deliberately NOT capped or floored here: its edge is largest
# in exactly the cheap buckets a shared floor would forbid (+74.5% under
# 0.15 across 6 fills). Note this means the ACCOUNT floor must stay low -
# both brokers may only TIGHTEN these per-order bounds, never loosen them,
# so a high MIN_BUY_PRICE would silently re-impose itself on the hedge.
#
# Both default to the account bound, so this is inert until set.
PRIMARY_ENTRY_MIN_PRICE = _env_float(
    "PRIMARY_ENTRY_MIN_PRICE", str(MIN_BUY_PRICE))
PRIMARY_ENTRY_MAX_PRICE = _env_float(
    "PRIMARY_ENTRY_MAX_PRICE", str(MAX_BUY_PRICE))
BTC_STALE_AFTER = _env_float("BTC_STALE_AFTER", "3.0")
# How long the book we hold may have been in our hands. For a REST read
# this is the request round trip, so it stays near zero.
ORDERBOOK_MAX_AGE_SECONDS = _env_float("ORDERBOOK_MAX_AGE_SECONDS", "8.0")
# How long the venue may have left the book UNCHANGED before we stop
# believing it. This is not freshness: a quiet market legitimately goes
# minutes without a single change, and measuring staleness from the last
# change refused perfectly current books. Measured on btc-updown-5m, gaps
# of 33s between changes are ordinary. The bound exists only to catch a
# venue serving a frozen or cached book.
ORDERBOOK_MAX_QUIET_SECONDS = _env_float("ORDERBOOK_MAX_QUIET_SECONDS", "900.0")
# A book timestamp ahead of our clock by more than this means the clock
# or the timestamp unit is wrong, never a real book.
ORDERBOOK_FUTURE_TOLERANCE_SECONDS = _env_float(
    "ORDERBOOK_FUTURE_TOLERANCE_SECONDS", "5.0")
MAX_ALLOWED_SPREAD = _env_float("MAX_ALLOWED_SPREAD", "0.25")

# ---- HTTP transport: how venue reads survive a bad link --------------------
# Every unauthenticated venue read goes through http_pool on a kept-alive
# connection. Two failure modes cost real rounds:
#
#   1. The peer resets a pooled connection mid-request. urllib3 already
#      re-opens a connection the peer closed CLEANLY, but a reset while the
#      request is in flight surfaces as a read error and, with the stock
#      adapter's Retry(total=0), propagates straight to the caller. Measured
#      locally against a socket that RSTs the second request: ConnectionError
#      with the stock adapter, recovered in 15ms with one read retry.
#   2. The peer accepts and then goes silent. Only a timeout ends that, and a
#      scalar requests timeout is applied to the connect AND the read phase
#      separately, so `timeout=8` means up to 16s, not 8s.
#
# Retries here are transport-only. Callers that care about 429/5xx already
# implement their own backoff with Retry-After, and retrying in both places
# would multiply attempts inside a budget measured in single seconds.
HTTP_TRANSPORT_RETRIES = _env_int("HTTP_TRANSPORT_RETRIES", "1")
if HTTP_TRANSPORT_RETRIES is None or not 0 <= HTTP_TRANSPORT_RETRIES <= 3:
    raise ValueError("HTTP_TRANSPORT_RETRIES must be between 0 and 3")
HTTP_RETRY_BACKOFF_SECONDS = _env_float("HTTP_RETRY_BACKOFF_SECONDS", "0.1")
# Establishing a connection is either fast or not happening. Kept separate
# from the read budget so a caller asking for an 8s read cannot silently wait
# 16s, and so a dead route fails while the round is still tradeable.
HTTP_CONNECT_TIMEOUT_SECONDS = _env_float("HTTP_CONNECT_TIMEOUT_SECONDS", "4.0")
# Keepalive probes on idle pooled sockets. Between trade cycles a connection
# can be dropped by a NAT or load balancer without a FIN ever arriving; the
# next read then blocks for the full timeout instead of failing at once.
HTTP_KEEPALIVE_IDLE_SECONDS = _env_int("HTTP_KEEPALIVE_IDLE_SECONDS", "30")
HTTP_KEEPALIVE_INTERVAL_SECONDS = _env_int("HTTP_KEEPALIVE_INTERVAL_SECONDS", "10")
CLOCK_MAX_DRIFT_SECONDS = _env_float("CLOCK_MAX_DRIFT_SECONDS", "2.0")
PAPER_LATENCY_MS = _env_float("PAPER_LATENCY_MS", "150")
TWAP_STALE_AFTER = _env_float("TWAP_STALE_AFTER", "10.0")
# No order inside the final minute. Measured over 16 fills: 31.2% won against
# a 69.6% break-even, z = -3.29 - and it has a mechanism, not just a p-value.
# In the last minute the book goes one-sided (the winning leg keeps only bids,
# the losing leg only asks), so the fills still available are the ones the
# market is content to sell. That is adverse selection, and no signal fixes it.
# Trading T-120..T-60 was +1.4 per $100 over the same period; the damage was
# entirely in the tail.
MIN_SECONDS_TO_EXPIRY = _env_float("MIN_SECONDS_TO_EXPIRY", "60.0")
# ---- phase 1: price band, with direction authorized by fresh SIG PRICE -----
# The band controls whether the selected contract is affordable.  Binance
# opening-to-current direction controls which outcome may be submitted; a
# neutral, stale, or opposite signal now fails closed.
PHASE1_ENABLED = bool(_env_bool("PHASE1_ENABLED", True))
PHASE1_INTERVAL_SECONDS = _env_float("PHASE1_INTERVAL_SECONDS", "12")
# Bands are per sub-window: "start:end:low:high[:interval]", seconds REMAINING
# in the round, comma separated, listed from the open toward expiry. Each band
# caps its own orders, so a thin top level can never fill outside the band
# being measured - one global cap cannot bound several ranges. The optional
# fifth field sets that window's own cadence; without it the band uses
# PHASE1_INTERVAL_SECONDS.
#
# The final band covers the same T-120..T-60 interval as phase 2. Measured
# across 89 observations it won 68.5% needing 66.8% (+3.7 per $100, z=+0.66)
# - fair value.  The band and Phase 2 retain different entry conditions, but
# every order side is now authorized by the same fresh Binance SIG PRICE.
PHASE1_BANDS_RAW = _env_text(
    "PHASE1_BANDS",
    "300:240:0.35:0.45,240:180:0.30:0.40,180:120:0.40:0.50,120:60:0.55:0.75:8")


def _parse_bands(raw: str):
    bands = []
    for chunk in str(raw).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) not in (4, 5):
            raise ValueError(
                f"PHASE1_BANDS entry {chunk!r} must be start:end:low:high[:interval]")
        try:
            start, end = int(parts[0]), int(parts[1])
            low, high = float(parts[2]), float(parts[3])
            interval = float(parts[4]) if len(parts) == 5 else None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"PHASE1_BANDS entry {chunk!r} is not numeric") from exc
        if not 0 <= end < start <= 300:
            raise ValueError(
                f"PHASE1_BANDS entry {chunk!r} needs 0 <= end < start <= 300")
        if not (math.isfinite(low) and math.isfinite(high) and 0 < low < high < 1):
            raise ValueError(f"PHASE1_BANDS entry {chunk!r} needs 0 < low < high < 1")
        if interval is not None and not (
                math.isfinite(interval) and 1 <= interval <= (start - end)):
            raise ValueError(
                f"PHASE1_BANDS entry {chunk!r} needs a cadence of 1..{start - end}s")
        bands.append((start, end, low, high, interval))
    if not bands:
        raise ValueError("PHASE1_BANDS must list at least one window")
    bands.sort(key=lambda b: -b[0])
    for earlier, later in zip(bands, bands[1:]):
        if later[0] > earlier[1]:
            raise ValueError(
                f"PHASE1_BANDS windows overlap: {earlier[0]}:{earlier[1]} "
                f"and {later[0]}:{later[1]}")
    return tuple(bands)


PHASE1_BANDS = _parse_bands(PHASE1_BANDS_RAW)
PHASE1_START_SECONDS = PHASE1_BANDS[0][0]
PHASE1_END_SECONDS = PHASE1_BANDS[-1][1]
# Kept for callers that want the overall reach of phase 1 rather than a
# specific window's band.
PHASE1_MIN_PRICE = min(b[2] for b in PHASE1_BANDS)
PHASE1_MAX_PRICE = max(b[3] for b in PHASE1_BANDS)


def phase1_band(seconds_left: float):
    """The band governing this instant, or None outside every window.

    Returns (start, end, low, high, interval) with the cadence resolved, so
    callers never have to know whether a band set its own.
    """
    for start, end, low, high, interval in PHASE1_BANDS:
        if end < seconds_left <= start:
            return start, end, low, high, (
                PHASE1_INTERVAL_SECONDS if interval is None else interval)
    return None


# Phase 2 keeps book and Chainlink votes as diagnostics, while fresh Binance
# SIG PRICE is the sole order-side authority.  It remains off by default;
# enable it explicitly to run the experimental path.
PHASE2_ENABLED = bool(_env_bool("PHASE2_ENABLED", False))
# PAPER may deliberately follow a later, verified SIG PRICE reversal even
# after the first outcome token has filled.  LIVE keeps the complement-leg
# block unconditionally: two independent venue orders are not an atomic pair.
# Off by default so existing paper runs preserve their one-leg-per-round risk
# contract unless the experiment is selected explicitly.
PAPER_ALLOW_SIGNAL_FLIPS = bool(_env_bool("PAPER_ALLOW_SIGNAL_FLIPS", False))
if PAPER_ALLOW_SIGNAL_FLIPS and (PHASE1_ENABLED or not PHASE2_ENABLED):
    raise ValueError(
        "PAPER_ALLOW_SIGNAL_FLIPS requires PHASE1_ENABLED=0 and "
        "PHASE2_ENABLED=1 so band and signal cadences cannot overlap")

# Permit the complement leg in LIVE after a verified signal reversal, the way
# PAPER_ALLOW_SIGNAL_FLIPS does for paper. This is a risk decision, not a bug
# fix: two independent venue orders are not an atomic pair, so a reversal can
# leave the account holding one leg at a price the second leg never matched -
# in PAPER that costs nothing, in LIVE it is real money on an unhedged side.
# Off by default; enabling it is choosing that exposure knowingly.
LIVE_ALLOW_SIGNAL_FLIPS = bool(_env_bool("LIVE_ALLOW_SIGNAL_FLIPS", False))
if LIVE_ALLOW_SIGNAL_FLIPS and (PHASE1_ENABLED or not PHASE2_ENABLED):
    raise ValueError(
        "LIVE_ALLOW_SIGNAL_FLIPS requires PHASE1_ENABLED=0 and PHASE2_ENABLED=1")

# Give SIG BOOK and SIG CHAINLINK their own orders instead of leaving them as
# diagnostics. Each non-neutral signal trades its own side, so a round where
# they disagree buys BOTH legs on purpose. Measured on 1,957 logged decisions
# that is 26.1% of them, and a simultaneous pair costs the overround: at the
# 1.0100 sum observed live, ~-$0.22 per $5 pair whichever way BTC settles.
# The complement guard exists to prevent exactly that, so this switch stands
# it down for signal-driven legs and cannot be combined with a lock that would
# refuse them. Off by default; PAPER only.
# How early the NEXT round's books are discovered and pre-subscribed. The
# websocket needs time to subscribe and receive a first snapshot before the
# boundary, or the opening seconds of the new round trade against an empty
# book. Raising this costs one extra gamma-api call per round, no more.
ROUND_PREPARE_LEAD_SECONDS = _env_float("ROUND_PREPARE_LEAD_SECONDS", "30")
if not 5.0 <= ROUND_PREPARE_LEAD_SECONDS <= 280.0:
    raise ValueError("ROUND_PREPARE_LEAD_SECONDS must be between 5 and 280")
# Rotation poll interval away from a boundary. Near one the loop polls every
# second regardless: the opening print may only be latched in the first 5s of
# a round, so a rotation that lands 6s late costs the entire round. Mid-round
# there is nothing to gain and gamma-api rate-limits, hence the slower default.
ROUND_POLL_SECONDS = _env_float("ROUND_POLL_SECONDS", "5")
if not 0.5 <= ROUND_POLL_SECONDS <= 30.0:
    raise ValueError("ROUND_POLL_SECONDS must be between 0.5 and 30")

# Phase 2 normally refuses to trade a round unless all four boundary inputs are
# present: both Binance values and both Chainlink values. But SIG PRICE alone
# owns the order side, and SIG CHAINLINK is either a diagnostic or - under
# PHASE2_MULTI_SIGNAL - a leg of its own that can simply abstain. Requiring its
# inputs to trade cancels rounds that SIG PRICE could have handled: one missed
# one-second TWAP observation kills five minutes of trading. With this on, only
# missing BINANCE inputs cancel the round; Chainlink abstains like SIG BOOK
# already does on a one-sided book. Off by default so the stricter original
# contract is what you get unless the looser one is chosen deliberately.
# Polymarket enables a taker matching delay on these markets (`itode: true` on
# /clob-markets) but the endpoint states only THAT a delay exists, never how
# long. An order submitted without knowing it can match after the round has
# already resolved, so the live path refuses outright by default.
#
# Set this to the delay you are willing to assume, in seconds, and live orders
# are permitted outside that many seconds from the round end - refused inside
# it, where an unknown delay could span resolution. 0 keeps the hard refusal.
#
# Choose generously: if the real delay exceeds this, orders sent just outside
# the window can still match after expiry, which is exactly the failure the
# refusal exists to prevent. It costs only the tail of a 300s round.
ASSUMED_MATCH_DELAY_SECONDS = _env_float("ASSUMED_MATCH_DELAY_SECONDS", "0")
if (not math.isfinite(ASSUMED_MATCH_DELAY_SECONDS)
        or not 0 <= ASSUMED_MATCH_DELAY_SECONDS <= 120):
    raise ValueError("ASSUMED_MATCH_DELAY_SECONDS must be between 0 and 120")

# The opening print is latched from a websocket trade stamped in the first 5
# seconds of the round. A socket mid-reconnect across the boundary never
# receives it and the whole round is lost - measured at about one round in
# five. After this many seconds the bot asks Binance REST for a trade from the
# SAME 5-second interval, which recovers the identical value rather than
# substituting a later price. Give the socket a real chance first; retrying too
# early just spends an API call on a print that was about to arrive.
BOUNDARY_BACKFILL_AFTER = _env_float("BOUNDARY_BACKFILL_AFTER", "15")
# How many times the REST recovery may be tried, and how long to wait
# between attempts. It used to run exactly ONCE per round: the "already
# tried" flag was set BEFORE the call, so a single timeout or 429 cost the
# whole round - no opening print means price_signal returns None, every
# attempt is refused, and the round produces nothing at all. Measured over 89
# rounds the bot was actually up for, 10% produced no trade whatsoever.
#
# Retrying is safe because _recover_boundary_print queries the SAME
# [window, window+5) interval every time and refuses a response stamped
# outside it. A later attempt therefore returns the same opening print the
# socket would have latched, never a mid-round price standing in for it -
# which is the substitution the strike logic exists to prevent.
BOUNDARY_BACKFILL_RETRIES = int(_env_float("BOUNDARY_BACKFILL_RETRIES", "3"))
if not 1 <= BOUNDARY_BACKFILL_RETRIES <= 20:
    raise ValueError("BOUNDARY_BACKFILL_RETRIES must be between 1 and 20")
BOUNDARY_BACKFILL_RETRY_GAP = _env_float("BOUNDARY_BACKFILL_RETRY_GAP", "10")
if (not math.isfinite(BOUNDARY_BACKFILL_RETRY_GAP)
        or not 1 <= BOUNDARY_BACKFILL_RETRY_GAP <= 120):
    raise ValueError("BOUNDARY_BACKFILL_RETRY_GAP must be between 1 and 120 seconds")
if not math.isfinite(BOUNDARY_BACKFILL_AFTER) or not 5 <= BOUNDARY_BACKFILL_AFTER <= 120:
    raise ValueError("BOUNDARY_BACKFILL_AFTER must be between 5 and 120 seconds")

# A round already underway when the bot starts is a degraded round: its
# opening print may only be recoverable from REST, the book has moved, and
# part of its trading window is already gone. With this on the bot observes
# that round without trading it and begins at the next clean boundary.
SKIP_JOINED_ROUND = bool(_env_bool("SKIP_JOINED_ROUND", False))

PHASE2_PARTIAL_SIGNALS = bool(_env_bool("PHASE2_PARTIAL_SIGNALS", False))

# Order side follows the DISSENTING signal when the three disagree, instead of
# SIG PRICE unconditionally. Measured over 275 archived rounds this scored -5.83
# per $100 against -1.90 for SIG PRICE alone, so it is off by default and is
# selected deliberately. Requires PHASE2_MULTI_SIGNAL off: the minority rule
# picks ONE side, and multi-signal exists to buy several.
SIGNAL_MINORITY_RULE = bool(_env_bool("SIGNAL_MINORITY_RULE", False))

# WHICH signal actually picks the order side.
#
#   "price"    - SIG PRICE alone. BOOK and CHAINLINK stay diagnostics.
#   "minority" - follow the dissenting signal (strategy.minority_decision).
#   "final"    - strategy.final_decision, the line the dashboard has always
#                shown as "diagnostic": PRICE and BOOK agreeing wins; failing
#                that CHAINLINK breaks the tie by siding with one of them;
#                failing that, whichever signal is present at all. This is a
#                CONFIRMATION rule - it trades the side two feeds agree on,
#                where "price" trades one feed's opinion unconfirmed.
#
# main_bot._authority_side is the single place this is read, and every
# re-validation gate asks it the same question. That matters more than the
# rule itself: when the chooser and the gates disagree, the gates reject
# nearly every order the chooser makes.
#
# Defaults to the SIGNAL_MINORITY_RULE setting, so an existing .env keeps the
# behaviour it already had.
SIGNAL_DECISION_RULE = (
    _env_text("SIGNAL_DECISION_RULE",
              "minority" if SIGNAL_MINORITY_RULE else "price") or "").strip().lower()
if SIGNAL_DECISION_RULE not in {"price", "minority", "final"}:
    raise ValueError(
        "SIGNAL_DECISION_RULE must be one of: price, minority, final")

# Refuse a phase-2 entry unless every signal that voted agrees. A signal that
# abstained does not count against unanimity - silence is not dissent.
#
# Measured over 894 settled fills, split by whether any signal dissented:
#
#     UNANIMOUS  648 fills  $2104.89  -5.7%  60% of positions won
#     CONTESTED  246 fills  $ 692.92  -6.7%  46% of positions won
#
# Filtering to unanimous only would have moved total P&L -165.66 -> -119.01
# (+46.65) and cut capital at risk by 25%. Read that honestly: BOTH groups
# lose, and the return improves only 1.0 point. Most of the gain is from
# trading less, not from finding a winning subset - so this is a turnover
# brake, not an edge. It is off by default for that reason.
#
# Phase 1 is unaffected: the bands are price-only by design and never read
# BOOK or CHAINLINK, so there is no unanimity to test there.
REQUIRE_SIGNAL_UNANIMITY = bool(_env_bool("REQUIRE_SIGNAL_UNANIMITY", False))

PHASE2_MULTI_SIGNAL = bool(_env_bool("PHASE2_MULTI_SIGNAL", False))
if PHASE2_MULTI_SIGNAL and not PHASE2_ENABLED:
    raise ValueError("PHASE2_MULTI_SIGNAL requires PHASE2_ENABLED=1")
if SIGNAL_DECISION_RULE == "minority" and PHASE2_MULTI_SIGNAL:
    raise ValueError(
        "SIGNAL_MINORITY_RULE selects a single dissenting side; it cannot be "
        "combined with PHASE2_MULTI_SIGNAL, which buys one leg per signal")
if PHASE2_MULTI_SIGNAL and PAPER_ALLOW_SIGNAL_FLIPS:
    raise ValueError(
        "PHASE2_MULTI_SIGNAL and PAPER_ALLOW_SIGNAL_FLIPS both relax the "
        "complement guard by different rules; enable exactly one")
if PHASE2_MULTI_SIGNAL and LIVE_ALLOW_SIGNAL_FLIPS:
    raise ValueError(
        "PHASE2_MULTI_SIGNAL and LIVE_ALLOW_SIGNAL_FLIPS both relax the "
        "complement guard by different rules; enable exactly one")

# The order path refuses any submission outside the round's execution
# interval. That interval has to reach back to the earliest second ANY enabled
# phase can trade: sizing it from TRADE_LAST_SECONDS alone silently refuses
# every phase-1 order as "outside the current round execution interval",
# because phase 1 runs entirely before phase 2's window opens.
EXECUTION_WINDOW_SECONDS = max(
    TRADE_LAST_SECONDS,
    PHASE1_START_SECONDS if PHASE1_ENABLED else 0,
)


# Polymarket takes shares * theta * p * (1-p) from a taker. For a fixed
# notional that is at most notional * theta, which is all a cap needs.
TAKER_FEE_RATE = 0.07
VENUE_MIN_SHARES = 5.0

# ---- paired-leg profit lock ------------------------------------------------
# Holding both legs of one market redeems for exactly $1.00 per matched pair at
# settlement, whichever way BTC goes. Buying the complement is therefore worth
# doing only when both entry prices plus both fees come to less than $1.00;
# above that the pair is a guaranteed loss, which is what the complement guard
# normally exists to prevent. Off by default: it deliberately relaxes that
# guard, so it has to be switched on knowingly.
PAIR_LOCK_ENABLED = bool(_env_bool("PAIR_LOCK_ENABLED", False))
# Headroom held back from $1.00. A quote is not a fill: the book can move a
# tick between the check and the FOK, the venue can change theta, and the
# broker rounds up to VENUE_MIN_SHARES. Without a margin, a pair measured at
# exactly break-even settles as a small loss.
PAIR_LOCK_MIN_EDGE = _env_float("PAIR_LOCK_MIN_EDGE", "0.02")
if not math.isfinite(PAIR_LOCK_MIN_EDGE) or not 0.0 <= PAIR_LOCK_MIN_EDGE < 1.0:
    raise ValueError("PAIR_LOCK_MIN_EDGE must be in [0, 1)")


def pair_lock_permits(entry_price, entry_fee_per_share,
                      ask) -> tuple[bool, float]:
    """Would buying the complement at ``ask`` lock a profit on the pair?

    Returns ``(permitted, locked_per_pair)``. The fee already paid on the held
    leg is counted deliberately: this answers "is the finished round position
    profitable", not "is this marginal order cheap". Sunk-cost reasoning would
    let the bot complete a pair that still loses money overall, and the whole
    point of the lock is that the outcome stops depending on BTC.
    """
    try:
        p1 = float(entry_price)
        f1 = float(entry_fee_per_share)
        p2 = float(ask)
    except (TypeError, ValueError):
        return False, 0.0
    if not all(math.isfinite(v) for v in (p1, f1, p2)):
        return False, 0.0
    if not 0.0 < p1 < 1.0 or not 0.0 < p2 < 1.0 or f1 < 0.0:
        return False, 0.0
    f2 = TAKER_FEE_RATE * p2 * (1.0 - p2)
    locked = 1.0 - (p1 + f1 + p2 + f2)
    return locked >= PAIR_LOCK_MIN_EDGE, locked


# ---- tapering entry + growing hedge (PAPER only) ---------------------------
# Backtested against 102 real settled rounds before being wired in: capping
# same-side pyramiding after a couple of confirmations and routing further
# confirmations into a small hedge on the complement improved both total P&L
# and max drawdown versus letting a round pyramid unbounded. PAPER only by
# runtime check in main_bot.py, not just this default - a signal that has
# already lost ~$589 once on old code gets a second, cheaper way to be wrong.
TAPER_HEDGE_ENABLED = bool(_env_bool("TAPER_HEDGE_ENABLED", False))
# How many signal-side buys per opposite-side buy. The cycle runs this many
# primary slots then one complement slot, so 2 is the original 2:1 shape and
# 6 gives 6:1. Slot 1 takes TAPER_ENTRY1_USD, the remaining primary slots take
# TAPER_ENTRY2_USD, and the last takes TAPER_HEDGE_INCREMENT_USD.
#
# Measured over 1032 settled fills on 2026-09-10, re-weighting the two legs by
# their own per-fill returns:
#
#     1/1  -5.04%    2/1  -2.25%    3/1  -1.03%    6/1  +0.42%   no hedge +2.14%
#
# Read that with the caveat it deserves: on that sample NEITHER leg's edge was
# statistically significant (primary +0.027, p=0.096; hedge -0.050, p=0.115),
# and the run before it produced the OPPOSITE ranking with both legs
# significant. Detecting a 3-point edge needs roughly 1,100 fills per leg. So
# this knob moves a number that is, so far, inside the noise band - it exists
# to make the ratio testable, not because a best value is known.
TAPER_PRIMARY_SLOTS = int(_env_float("TAPER_PRIMARY_SLOTS", "2"))
if TAPER_PRIMARY_SLOTS < 1:
    raise ValueError("TAPER_PRIMARY_SLOTS must be at least 1")

TAPER_ENTRY1_USD = _env_float("TAPER_ENTRY1_USD", "3.0")
TAPER_ENTRY2_USD = _env_float("TAPER_ENTRY2_USD", "2.0")
TAPER_HEDGE_INCREMENT_USD = _env_float("TAPER_HEDGE_INCREMENT_USD", "1.0")


def _taper_ladder(name: str) -> tuple[float, ...]:
    """Per-slot dollar amounts, e.g. "4,3,2". Empty means "not configured"."""
    raw = (_env_text(name, "") or "").strip()
    if not raw:
        return ()
    out = []
    for piece in raw.replace(";", ",").split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            value = float(piece)
        except ValueError:
            raise ValueError(f"{name} entries must be numbers, got {piece!r}")
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} entries must be finite and positive")
        out.append(value)
    if not out:
        raise ValueError(f"{name} was set but lists no amounts")
    return tuple(out)


# Explicit per-slot sizing, which supersedes TAPER_ENTRY1/2_USD,
# TAPER_HEDGE_INCREMENT_USD and TAPER_PRIMARY_SLOTS when set.
#
#     TAPER_PRIMARY_LADDER=4,3,2      three signal-side slots at $4, $3, $2
#     TAPER_HEDGE_LADDER=2            one opposite slot at $2
#
# The LENGTH of each list sets the number of slots, so the pair above is a
# 3:1 cycle. Leave both empty and the old scheme applies unchanged: slot 1 at
# TAPER_ENTRY1_USD, the remaining TAPER_PRIMARY_SLOTS-1 at TAPER_ENTRY2_USD,
# then one slot at TAPER_HEDGE_INCREMENT_USD.
#
# The venue's 5-share minimum compresses a ladder at high prices: an order
# always costs at least 5 x price, so at 0.80 every slot costs $4.00 whatever
# it asks for, and at a 0.663 average the floor is $3.32. A 4/3/2 ladder is
# therefore closer to 4/3.32/3.32 in practice - it separates only where fills
# are cheap.
TAPER_PRIMARY_LADDER = _taper_ladder("TAPER_PRIMARY_LADDER")
TAPER_HEDGE_LADDER = _taper_ladder("TAPER_HEDGE_LADDER")
if TAPER_HEDGE_LADDER and not TAPER_PRIMARY_LADDER:
    raise ValueError(
        "TAPER_HEDGE_LADDER needs TAPER_PRIMARY_LADDER; a cycle with no "
        "signal-side slot has nothing for the opposite slot to hedge")

# Advance the taper cycle after this many consecutive attempts that selected a
# slot but could not fill it. 0 keeps the old behaviour: the cycle waits on
# that slot indefinitely.
#
# Why it is needed. Slots 1 and 2 both buy the anchored side, so when that
# side prices outside MIN/MAX_BUY_PRICE the cycle cannot fill and cannot move
# - and because it never reaches slot 3, it never tries the complement, which
# in a binary market is precisely the leg that IS cheap when the anchor is
# expensive. A stable signal plus a priced-out anchor therefore costs the rest
# of the round. Measured on archived runs, 43% of phase-2 attempts already
# ended skipped_unfillable on a WIDER band than the current 0.30-0.80.
#
# The trade: the 2 signal : 1 opposite cadence is exact over FILLS only while
# nothing advances on a skip. Set this and the ratio becomes approximate -
# slots can be stepped past without ever filling. That is the point (progress
# beats precision when the book has moved away), but it is a real change to
# what the cadence guarantees.
TAPER_ADVANCE_AFTER_SKIPS = int(_env_float("TAPER_ADVANCE_AFTER_SKIPS", "0"))
if TAPER_ADVANCE_AFTER_SKIPS < 0:
    raise ValueError("TAPER_ADVANCE_AFTER_SKIPS must be zero or positive")
for _taper_name, _taper_value in (
        ("TAPER_ENTRY1_USD", TAPER_ENTRY1_USD),
        ("TAPER_ENTRY2_USD", TAPER_ENTRY2_USD),
        ("TAPER_HEDGE_INCREMENT_USD", TAPER_HEDGE_INCREMENT_USD)):
    if not math.isfinite(_taper_value) or _taper_value <= 0:
        raise ValueError(f"{_taper_name} must be finite and positive")
del _taper_name, _taper_value


def entry_cost_ceiling(cap_price: float, amount: float | None = None) -> float:
    """The most one entry can take out of the account at this price cap.

    BUGFIX: main_bot used to charge MAX_ROUND_EXPOSURE exactly BET_SIZE per
    entry. The broker sizes UP to the venue's 5-share minimum, so any fill
    above BET_SIZE/5 costs more than BET_SIZE and the fee is on top. Measured
    on a real paper run the tracker was 22% low overall and 76% low on one
    round, which made the cap nominal rather than real. A limit has to use an
    upper bound, so this returns one.

    BUGFIX 2: it also ignored what the order actually stakes, charging
    BET_SIZE however large the entry was. That was harmless while every
    entry was BET_SIZE or smaller - it merely over-charged - but a taper
    ladder stakes per slot, and a $5 slot really costs up to $5.28 against
    the $4.28 charged. The cap silently under-counts by 19% and stops
    meaning what it says. `amount` defaults to BET_SIZE so every existing
    caller (phase-1 bands, the multi-signal legs, the round budget) is
    unchanged.
    """
    stake = float(BET_SIZE if amount is None else amount)
    notional = max(stake, VENUE_MIN_SHARES * float(cap_price))
    return notional * (1.0 + TAKER_FEE_RATE)


def _round_entry_budget() -> float:
    """Worst-case CASH for one round, counting only the phases switched on.

    Deriving it from the phases themselves means parking phase 2 lowers the
    cap automatically, instead of leaving a ceiling sized for a path that no
    longer runs. Each band is budgeted at its own ceiling price, so a band
    priced above BET_SIZE/5 gets the room it will actually need.
    """
    budget = 0.0
    if PHASE1_ENABLED:
        # Each band may set its own cadence, so budget them individually.
        for start, end, _lo, hi, interval in PHASE1_BANDS:
            gap = PHASE1_INTERVAL_SECONDS if interval is None else interval
            entries = math.ceil((start - end) / max(gap, 1))
            budget += entries * entry_cost_ceiling(hi)
    if PHASE2_ENABLED:
        # Phase 2 stops at MIN_SECONDS_TO_EXPIRY, so its budget is the window
        # it can actually reach, not the whole tail of the round.
        entries = math.ceil(
            max(0.0, TRADE_LAST_SECONDS - MIN_SECONDS_TO_EXPIRY)
            / max(TRADE_INTERVAL_SECONDS, 1))
        budget += entries * entry_cost_ceiling(MAX_BUY_PRICE)
    return budget if budget > 0 else entry_cost_ceiling(MAX_BUY_PRICE)


MAX_ROUND_EXPOSURE = _env_float("MAX_ROUND_EXPOSURE", str(_round_entry_budget()))

# Ceiling on cash committed to rounds the venue has not resolved yet, across
# ALL open rounds. MAX_ROUND_EXPOSURE caps one round in isolation, which is
# blind to the case that actually empties an account: settlement stalling
# upstream while every new round passes its own budget check. Observed doing
# exactly that - 20 open rounds holding 103% of a $300 wallet, leaving $3
# tradeable, purely because the venue had not written resolutions on chain.
#
# The bot cannot settle those itself without inventing an outcome, so the only
# safe lever is to stop opening new ones. Entries are refused while unsettled
# cost is at or above this, and resume by themselves as positions settle.
#
# 0 disables it, which is the default: this changes nothing until set.
MAX_UNSETTLED_EXPOSURE = _env_float("MAX_UNSETTLED_EXPOSURE", "0")
if not math.isfinite(MAX_UNSETTLED_EXPOSURE) or MAX_UNSETTLED_EXPOSURE < 0:
    raise ValueError("MAX_UNSETTLED_EXPOSURE must be finite and non-negative")

# ---- stop loss --------------------------------------------------------------
# Sell a held leg once its BID reaches STOP_LOSS_PRICE. This is a client-side
# trigger, not an order type: the CLOB has only FAK/FOK/GTC/GTD, so nothing
# resting on the book can act as a stop. A resting sell fills when price RISES,
# which is a take-profit; a stop has to be watched and then crossed.
#
# Measured over 275 archived rounds: 14.1% of legs that eventually WON traded
# at or below 0.25 first, dipping as low as 0.04 with 143s still to run. A stop
# therefore cuts roughly one winner in seven. On both-leg rounds the exit was
# worth +$392 (t=+3.44) because it was selling a structurally dead second leg;
# on single-leg rounds the same test came out at -0.58. Whether it pays depends
# entirely on the haircut actually paid on the way out, which no archived run
# recorded, so this ships OFF and instrumented.
STOP_LOSS_ENABLED = bool(_env_bool("STOP_LOSS_ENABLED", False))
STOP_LOSS_PRICE = _env_float("STOP_LOSS_PRICE", "0.25")
# The absolute worst price the exit may accept while walking the book down.
# Setting this to the trigger price makes the stop a pure limit that simply
# does not fill in a thin book; lowering it buys certainty of exit with price.
# At a haircut beyond 0.19 the measured benefit inverts, so a floor far below
# the trigger is choosing execution over expectancy - deliberately.
STOP_LOSS_FLOOR_PRICE = _env_float("STOP_LOSS_FLOOR_PRICE", "0.05")
# Do not arm before this many seconds remain. The winners that recovered from
# under 0.25 did so at 93-209s left; a stop armed round-wide cut 13 of them
# against 5 when it only armed inside 120s.
STOP_LOSS_ARM_SECONDS = _env_float("STOP_LOSS_ARM_SECONDS", "120")
# Stop placing exits this close to expiry. The trader studied here stopped
# trading entirely at T-30 and placed nothing in the final 30 seconds.
STOP_LOSS_EXIT_CUTOFF_SECONDS = _env_float("STOP_LOSS_EXIT_CUTOFF_SECONDS", "20")
STOP_LOSS_POLL_SECONDS = _env_float("STOP_LOSS_POLL_SECONDS", "1.0")
if not 0.0 < STOP_LOSS_PRICE < 1.0:
    raise ValueError("STOP_LOSS_PRICE must be strictly between 0 and 1")
if not 0.0 < STOP_LOSS_FLOOR_PRICE <= STOP_LOSS_PRICE:
    raise ValueError(
        "STOP_LOSS_FLOOR_PRICE must be in (0, STOP_LOSS_PRICE]: a floor above "
        "the trigger could never fill")
if not 0.0 <= STOP_LOSS_EXIT_CUTOFF_SECONDS < STOP_LOSS_ARM_SECONDS <= 300.0:
    raise ValueError(
        "need 0 <= STOP_LOSS_EXIT_CUTOFF_SECONDS < STOP_LOSS_ARM_SECONDS <= 300")
if not 0.2 <= STOP_LOSS_POLL_SECONDS <= 30.0:
    raise ValueError("STOP_LOSS_POLL_SECONDS must be between 0.2 and 30")

CANCEL_OPEN_BEFORE_TRADE = bool(_env_bool("CANCEL_OPEN_BEFORE_TRADE", False))
ALLOW_GLOBAL_CANCEL_ALL = bool(_env_bool("ALLOW_GLOBAL_CANCEL_ALL", False))
ALLOW_CUSTOM_CLOB_HOST = bool(_env_bool("ALLOW_CUSTOM_CLOB_HOST", False))
CLOB_HOST = _env_text("CLOB_HOST", "https://clob.polymarket.com")
CHAIN_ID = 137
_VALID_TICKS = ("0.1", "0.01", "0.005", "0.0025", "0.001", "0.0001")
TICK_SIZE = _env_text("TICK_SIZE") or None
if TICK_SIZE is not None and TICK_SIZE not in _VALID_TICKS:
    raise ValueError(f"TICK_SIZE must be one of {_VALID_TICKS}, got {TICK_SIZE!r}")
NEG_RISK = _env_bool("NEG_RISK", None)
UP_TOKEN_ID = _env_text("UP_TOKEN_ID") or None
DOWN_TOKEN_ID = _env_text("DOWN_TOKEN_ID") or None
ORDERBOOK_TOKEN_ID = _env_text("ORDERBOOK_TOKEN_ID") or None
POLY_FUNDER = _env_text("POLY_FUNDER") or None
POLY_SIGNATURE_TYPE = _env_int("POLY_SIGNATURE_TYPE")


def _finite_positive(name, value):
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number, got {value!r}")


_finite_positive("BET_SIZE", BET_SIZE)
if Decimal(str(BET_SIZE)).quantize(Decimal("0.01")) != Decimal(str(BET_SIZE)):
    raise ValueError("BET_SIZE must be expressed in whole pUSD cents")
if not 1 <= TRADE_LAST_SECONDS <= 300:
    raise ValueError("TRADE_LAST_SECONDS must be between 1 and 300")
if (not math.isfinite(TRADE_INTERVAL_SECONDS)
        or not 1 <= TRADE_INTERVAL_SECONDS <= TRADE_LAST_SECONDS):
    raise ValueError("TRADE_INTERVAL_SECONDS must be in [1, TRADE_LAST_SECONDS]")
if not math.isfinite(MAX_BUY_PRICE) or not 0 < MAX_BUY_PRICE < 1:
    raise ValueError("MAX_BUY_PRICE must be strictly between 0 and 1")
if not math.isfinite(MIN_BUY_PRICE) or not 0 < MIN_BUY_PRICE < 1:
    raise ValueError("MIN_BUY_PRICE must be strictly between 0 and 1")
if not MIN_BUY_PRICE < MAX_BUY_PRICE:
    raise ValueError("MIN_BUY_PRICE must be below MAX_BUY_PRICE")
# The per-order bounds may only tighten the account band - both brokers
# enforce that at execution, so a value outside it would be silently ignored
# rather than applied. Refuse it here instead, where it is visible.
if (not math.isfinite(PRIMARY_ENTRY_MIN_PRICE)
        or not MIN_BUY_PRICE <= PRIMARY_ENTRY_MIN_PRICE < 1):
    raise ValueError(
        "PRIMARY_ENTRY_MIN_PRICE must be at or above MIN_BUY_PRICE and below 1")
if (not math.isfinite(PRIMARY_ENTRY_MAX_PRICE)
        or not 0 < PRIMARY_ENTRY_MAX_PRICE <= MAX_BUY_PRICE):
    raise ValueError(
        "PRIMARY_ENTRY_MAX_PRICE must be positive and at or below MAX_BUY_PRICE")
if not PRIMARY_ENTRY_MIN_PRICE < PRIMARY_ENTRY_MAX_PRICE:
    raise ValueError(
        "PRIMARY_ENTRY_MIN_PRICE must be below PRIMARY_ENTRY_MAX_PRICE")
# Window and band shapes are validated in _parse_bands; what is left is the
# cadence fitting the tightest window, and the stake clearing the venue
# minimum at the most expensive price any band can reach.
# Bands carrying their own cadence are validated in _parse_bands; the default
# only has to fit the narrowest window that relies on it.
_default_users = [b for b in PHASE1_BANDS if b[4] is None]
_narrowest = min((start - end for start, end, _l, _h, _i in _default_users),
                 default=300)
if (not math.isfinite(PHASE1_INTERVAL_SECONDS)
        or not 1 <= PHASE1_INTERVAL_SECONDS <= _narrowest):
    raise ValueError(
        f"PHASE1_INTERVAL_SECONDS must fit the narrowest band window ({_narrowest}s)")
# The venue minimum is 5 shares, so a band whose prices exceed BET_SIZE/5
# forces the engine to size the order up. That is legitimate - it is what the
# venue requires - but it must never be silent, because the stake then varies
# with price and a per-$100 comparison across bands stops being like-for-like.
PHASE1_STAKE_NOTES = tuple(
    (start, end, low, high, round(5 * low, 2), round(5 * high, 2))
    for start, end, low, high, _i in PHASE1_BANDS
    if 5 * high > BET_SIZE + 1e-9
)
if PHASE1_ENABLED and BET_SIZE < 5 * PHASE1_MIN_PRICE - 1e-9:
    raise ValueError(
        f"BET_SIZE {BET_SIZE} cannot buy the 5-share venue minimum anywhere in "
        f"the cheapest band ({PHASE1_MIN_PRICE}); raise it to "
        f"{5 * PHASE1_MIN_PRICE:.2f}")
_finite_positive("BTC_STALE_AFTER", BTC_STALE_AFTER)
_finite_positive("ORDERBOOK_MAX_AGE_SECONDS", ORDERBOOK_MAX_AGE_SECONDS)
_finite_positive("ORDERBOOK_MAX_QUIET_SECONDS", ORDERBOOK_MAX_QUIET_SECONDS)
_finite_positive("ORDERBOOK_FUTURE_TOLERANCE_SECONDS",
                 ORDERBOOK_FUTURE_TOLERANCE_SECONDS)
if ORDERBOOK_FUTURE_TOLERANCE_SECONDS < CLOCK_MAX_DRIFT_SECONDS:
    raise ValueError(
        "ORDERBOOK_FUTURE_TOLERANCE_SECONDS must be >= "
        "CLOCK_MAX_DRIFT_SECONDS, or a clock inside its allowed drift "
        "would still refuse every book as future-dated")
if not math.isfinite(MAX_ALLOWED_SPREAD) or not 0 < MAX_ALLOWED_SPREAD <= 1:
    raise ValueError("MAX_ALLOWED_SPREAD must be in (0, 1]")
_finite_positive("CLOCK_MAX_DRIFT_SECONDS", CLOCK_MAX_DRIFT_SECONDS)
if not math.isfinite(PAPER_LATENCY_MS) or PAPER_LATENCY_MS < 0:
    raise ValueError("PAPER_LATENCY_MS must be finite and non-negative")
_finite_positive("TWAP_STALE_AFTER", TWAP_STALE_AFTER)
_finite_positive("HTTP_CONNECT_TIMEOUT_SECONDS", HTTP_CONNECT_TIMEOUT_SECONDS)
if not math.isfinite(HTTP_RETRY_BACKOFF_SECONDS) or not 0 <= HTTP_RETRY_BACKOFF_SECONDS <= 2:
    raise ValueError("HTTP_RETRY_BACKOFF_SECONDS must be between 0 and 2")
if HTTP_KEEPALIVE_IDLE_SECONDS is None or not 1 <= HTTP_KEEPALIVE_IDLE_SECONDS <= 3600:
    raise ValueError("HTTP_KEEPALIVE_IDLE_SECONDS must be between 1 and 3600")
if (HTTP_KEEPALIVE_INTERVAL_SECONDS is None
        or not 1 <= HTTP_KEEPALIVE_INTERVAL_SECONDS <= 3600):
    raise ValueError("HTTP_KEEPALIVE_INTERVAL_SECONDS must be between 1 and 3600")
if (not math.isfinite(MIN_SECONDS_TO_EXPIRY)
        or not 0 <= MIN_SECONDS_TO_EXPIRY < TRADE_LAST_SECONDS):
    raise ValueError("MIN_SECONDS_TO_EXPIRY must be non-negative and below TRADE_LAST_SECONDS")
if not math.isfinite(MAX_ROUND_EXPOSURE) or MAX_ROUND_EXPOSURE < BET_SIZE:
    raise ValueError("MAX_ROUND_EXPOSURE must be finite and at least BET_SIZE")
if POLY_SIGNATURE_TYPE not in (None, 0, 1, 2, 3):
    raise ValueError("POLY_SIGNATURE_TYPE must be 0, 1, 2, or 3")
# py-clob-client-v2's L1 API-key derivation does not currently bind a type-3
# (POLY_1271 deposit-wallet) key to the funder. Upstream issue #70 remains
# open; allowing it here produces orders that the CLOB rejects as the wrong
# signer. Fail before any credential or order request instead.
if POLY_SIGNATURE_TYPE == 3:
    raise ValueError(
        "POLY_SIGNATURE_TYPE=3 is blocked: py-clob-client-v2 cannot currently "
        "derive a funder-bound POLY_1271 API key (upstream issue #70)"
    )
if POLY_SIGNATURE_TYPE in (1, 2, 3) and not POLY_FUNDER:
    raise ValueError("proxy signature types 1/2/3 require POLY_FUNDER")
if POLY_FUNDER and POLY_SIGNATURE_TYPE is None:
    raise ValueError("POLY_FUNDER requires an explicit POLY_SIGNATURE_TYPE")
if POLY_FUNDER and not re.fullmatch(r"0x[0-9A-Fa-f]{40}", POLY_FUNDER):
    raise ValueError("POLY_FUNDER must be a 20-byte 0x-prefixed Ethereum address")
for _name, _token in (
        ("UP_TOKEN_ID", UP_TOKEN_ID),
        ("DOWN_TOKEN_ID", DOWN_TOKEN_ID),
        ("ORDERBOOK_TOKEN_ID", ORDERBOOK_TOKEN_ID)):
    if _token is not None and (not re.fullmatch(r"[0-9]{1,78}", _token)
                               or int(_token) <= 0):
        raise ValueError(f"{_name} must be a positive decimal uint256 token id")
if CANCEL_OPEN_BEFORE_TRADE and not ALLOW_GLOBAL_CANCEL_ALL:
    raise ValueError(
        "CANCEL_OPEN_BEFORE_TRADE uses the wallet-wide cancel-all endpoint; "
        "set ALLOW_GLOBAL_CANCEL_ALL=1 only for a dedicated bot wallet"
    )
try:
    _clob_url = urlsplit(CLOB_HOST)
    _clob_port = _clob_url.port
except (TypeError, ValueError) as exc:
    raise ValueError("CLOB_HOST must be a valid HTTPS origin") from exc
if (_clob_url.scheme.lower() != "https" or not _clob_url.hostname
        or _clob_url.username is not None or _clob_url.password is not None
        or _clob_url.query or _clob_url.fragment
        or _clob_url.path not in ("", "/")):
    raise ValueError(
        "CLOB_HOST must be an HTTPS origin without credentials, path, query, or fragment"
    )
_official_clob = (
    _clob_url.hostname.lower() == "clob.polymarket.com"
    and _clob_port in (None, 443)
)
if not _official_clob and not ALLOW_CUSTOM_CLOB_HOST:
    raise ValueError(
        "custom CLOB_HOST is blocked because it receives authenticated requests; "
        "set ALLOW_CUSTOM_CLOB_HOST=1 only for an endpoint you control"
    )
# Store an origin without a trailing slash so every SDK endpoint is joined in a
# single, predictable way.
CLOB_HOST = f"https://{_clob_url.hostname}"
if _clob_port not in (None, 443):
    CLOB_HOST += f":{_clob_port}"
