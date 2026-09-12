"""Regression tests for the 2026-09-12 hardening patch.

Covers, in the order the work was done:

  A1  phase 2 cannot re-enter faster than TRADE_INTERVAL_SECONDS, on ANY
      exit path - success, skip, risk refusal, feed outage, REST failure.
  A2  no network runs inside the paper broker's state lock, so a slow
      order-book read cannot block cash_balance()/the dashboard.
  A3  an unreadable order book is never turned into a SIG BOOK vote.
  B3  shutdown clears every strategy provider, including the one it missed.

These are behavioural tests wherever a behaviour can be driven, plus a few
structural ones where the guarantee is "no code path anywhere does X" - that
claim cannot be proved by sampling a few paths.

    python tests_hardening.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import io
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

P = F = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global P, F
    if cond:
        P += 1
        print(f"  pass  {name}")
    else:
        F += 1
        print(f"  FAIL  {name} {detail}")


# --------------------------------------------------------------- harness ---
ALIGNED_WINDOW = 1_699_999_800          # a real 300s boundary: % 300 == 0


def _book(ask: float, *, bids=True, asks=True):
    b = [{"price": f"{ask - 0.01:.2f}", "size": "400"}] if bids else []
    a = [{"price": f"{ask:.2f}", "size": "400"}] if asks else []
    return (b, a)


class _Strike:
    def strike_for(self, _w): return 100.0
    def current_value(self): return 100.0
    def divergence(self, *_a): return {"diff": None}


async def _drive_phase2(*, seconds=5.0, remaining=100.0, fresh_price=100.0,
                        execution_mode="PAPER", execution_ready=None,
                        get_orderbook=None, trade_interval=6.0):
    """Run the real run_bot phase-2 loop against stubbed feeds.

    Returns (book_read_count, stdout). Everything is local; no network.
    """
    import main_bot

    calls = {"book": 0}
    books = {"11": _book(0.50), "12": _book(0.50)}

    def default_book(token, *_a, **_k):
        calls["book"] += 1
        return books[str(token)]

    fetch = get_orderbook or default_book

    def counting(token, *a, **k):
        calls["book"] += 1
        return fetch(token, *a, **k)

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    buf = io.StringIO()
    try:
        replace(main_bot, "execution_mode", execution_mode)
        replace(main_bot, "_paper_broker",
                object() if execution_mode == "PAPER" else None)
        replace(main_bot, "_execution_ready_provider", execution_ready)
        replace(main_bot, "_strike", _Strike())
        replace(main_bot, "get_balance_allowance",
                lambda: {"balance": 1000.0, "allowance": 1000.0})
        replace(main_bot, "place_trade", lambda *a, **k: False)
        replace(main_bot, "_append_trade", lambda row: None)
        replace(main_bot.polymarket_trade, "live_execution_disabled",
                lambda: execution_mode == "PAPER")
        replace(main_bot.market_discovery, "get_tokens_for_current_round",
                lambda _w: {"window_start": ALIGNED_WINDOW,
                            "window_end": ALIGNED_WINDOW + 300,
                            "up_token_id": "11", "down_token_id": "12",
                            "orderbook_token_id": "11",
                            "condition_id": "0x" + "a" * 64})
        replace(main_bot.orderbook, "get_orderbook",
                get_orderbook if get_orderbook else default_book)
        if get_orderbook is not None:
            setattr(main_bot.orderbook, "get_orderbook", counting)
        replace(main_bot.orderbook, "validate_buy_liquidity",
                lambda *a, **k: books["11"])
        replace(main_bot.price_ws, "latest_snapshot",
                lambda: (100.0, time.monotonic(), (ALIGNED_WINDOW + 1) * 1000))
        replace(main_bot.price_ws, "fresh_snapshot",
                lambda *a, **k: ((None, None) if fresh_price is None else
                                 (fresh_price, (ALIGNED_WINDOW + 100) * 1000)))
        replace(main_bot.timer, "unix",
                lambda *a, **k: ALIGNED_WINDOW + (300.0 - remaining))
        replace(main_bot.timer, "wall",
                lambda *a, **k: ALIGNED_WINDOW + (300.0 - remaining))
        replace(main_bot.timer, "check_clock", lambda *a, **k: (True, "ok", 0.0))
        replace(main_bot.config, "PHASE1_ENABLED", False)
        replace(main_bot.config, "PHASE2_ENABLED", True)
        replace(main_bot.config, "TRADE_INTERVAL_SECONDS", trade_interval)
        replace(main_bot.config, "CANCEL_OPEN_BEFORE_TRADE", False)
        replace(main_bot.config, "SKIP_JOINED_ROUND", False)
        main_bot.stop_event.clear()
        with contextlib.redirect_stdout(buf):
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(main_bot.run_bot(), timeout=seconds)
    finally:
        main_bot.stop_event.set()
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)
        main_bot.stop_event.clear()
    return calls["book"], buf.getvalue()


def _max_attempts(seconds: float, interval: float) -> int:
    """Attempts a correct cadence can make in `seconds`, plus one for entry."""
    return int(seconds / interval) + 1


# ----------------------------------------------------------------- A1 ------
async def t_a1_neutral_signal_cannot_spin_the_book_reads():
    """BEFORE: 25 book reads in 5s (5.0/s). The signal is neutral, so phase 2
    refuses - but it refused AFTER the read and re-entered 0.2s later."""
    reads, out = await _drive_phase2(seconds=5.0, fresh_price=100.0)
    check("a neutral SIG PRICE does not busy-loop the CLOB",
          reads <= _max_attempts(5.0, 6.0), f"{reads} book reads in 5s")
    check("the refusal is still reported",
          "SIG PRICE is neutral or unavailable" in out, out[-200:])
    check("the refusal is not repeated once per 0.2s",
          out.count("SIG PRICE is neutral or unavailable")
          <= _max_attempts(5.0, 6.0),
          str(out.count("SIG PRICE is neutral or unavailable")))


async def t_a1_stale_binance_print_does_not_flood():
    """BEFORE: 25 '[RISK] No order: missing ...' lines in 5s, one per 0.2s,
    with no dedup - a routine websocket reconnect flooded the console."""
    _reads, out = await _drive_phase2(seconds=5.0, fresh_price=None)
    lines = out.count("[RISK] No order: missing")
    check("a missing fresh Binance print does not flood the log",
          lines <= _max_attempts(5.0, 6.0), f"{lines} lines in 5s")
    check("the outage is still reported at least once", lines >= 1, str(lines))


async def t_a1_execution_not_ready_respects_cadence():
    """LIVE with the private fill stream down. BEFORE: 5 iterations/second
    for the whole outage, each redoing discovery and the durable refresh."""
    _reads, out = await _drive_phase2(
        seconds=5.0, execution_mode="LIVE", execution_ready=lambda _c: False)
    lines = out.count("private fill stream is not")
    check("an unready execution feed does not busy-loop",
          lines <= _max_attempts(5.0, 6.0), f"{lines} lines in 5s")


async def t_a1_cadence_scales_with_the_configured_interval():
    """The gate must read TRADE_INTERVAL_SECONDS, not a hardcoded number."""
    reads_fast, _ = await _drive_phase2(seconds=4.0, trade_interval=1.0)
    reads_slow, _ = await _drive_phase2(seconds=4.0, trade_interval=6.0)
    check("a 1s interval allows more attempts than a 6s one",
          reads_fast > reads_slow, f"{reads_fast} vs {reads_slow}")
    check("a 1s interval still respects its own cadence",
          reads_fast <= _max_attempts(4.0, 1.0), str(reads_fast))
    check("a 6s interval allows at most one attempt in 4s",
          reads_slow <= 1, str(reads_slow))


def _phase2_block(run_bot_node):
    for node in ast.walk(run_bot_node):
        if not isinstance(node, ast.If):
            continue
        names = {n.attr for n in ast.walk(node.test) if isinstance(n, ast.Attribute)}
        if "PHASE2_ENABLED" in names:
            return node
    return None


def _arms_gate(stmt) -> bool:
    if not isinstance(stmt, ast.Assign):
        return False
    return any(getattr(t, "id", "") == "phase2_gate_until" for t in stmt.targets)


def t_a1_every_phase2_exit_path_arms_the_cadence_gate():
    """Structural proof for requirement 8: cadence cannot be bypassed.

    Sampling exit paths cannot prove a negative. This walks EVERY statement
    list inside the phase-2 block and asserts that every `continue` is
    immediately preceded by an assignment to phase2_gate_until.
    """
    tree = ast.parse((ROOT / "main_bot.py").read_text(encoding="utf-8"))
    run_bot = next((n for n in ast.walk(tree)
                    if isinstance(n, ast.AsyncFunctionDef) and n.name == "run_bot"), None)
    check("run_bot is still the strategy loop", run_bot is not None)
    if run_bot is None:
        return
    block = _phase2_block(run_bot)
    check("the phase-2 block is still identifiable", block is not None)
    if block is None:
        return

    unguarded, total, inner = [], 0, 0

    def scan(body, in_inner_loop):
        """`in_inner_loop` marks a `continue` that targets a nested for/while
        (the multi-signal leg loop), NOT the strategy loop. Those advance to
        the next signal leg within the same phase-2 attempt, so they are not
        re-entry points and must not arm the gate - arming it there would
        cut the attempt short instead of delaying the next one."""
        nonlocal total, inner
        for i, stmt in enumerate(body):
            if isinstance(stmt, ast.Continue):
                if in_inner_loop:
                    inner += 1
                else:
                    total += 1
                    if not (i and _arms_gate(body[i - 1])):
                        unguarded.append(stmt.lineno)
            nested = in_inner_loop or isinstance(stmt, (ast.For, ast.While,
                                                        ast.AsyncFor))
            for field in ("body", "orelse", "finalbody"):
                sub = getattr(stmt, field, None)
                if isinstance(sub, list):
                    scan(sub, nested)
            for handler in getattr(stmt, "handlers", []) or []:
                scan(handler.body, nested)

    scan(block.body, False)
    check("the phase-2 block still has exit paths to check", total >= 20, str(total))
    check("the multi-signal leg loop is recognised separately", inner >= 1, str(inner))
    check("every phase-2 `continue` arms the cadence gate first",
          not unguarded, f"{len(unguarded)} unguarded at lines {unguarded}")


def t_a1_entry_is_gated_and_reset_at_a_round_boundary():
    src = (ROOT / "main_bot.py").read_text(encoding="utf-8")
    check("the phase-2 entry condition consults the gate",
          "and time.monotonic() >= phase2_gate_until):" in src)
    check("the gate is armed before any phase-2 work",
          "phase2_gate_until = _phase2_deadline()\n" in src)
    check("a round boundary re-arms the gate so a new round is not delayed",
          src.count("phase2_gate_until = 0.0") == 2, src.count("phase2_gate_until = 0.0"))
    check("the blocking _cooldown helper is gone",
          "async def _cooldown" not in src)


def t_a1_deadline_never_shortens_the_interval():
    import config
    import main_bot
    saved = config.TRADE_INTERVAL_SECONDS
    try:
        config.TRADE_INTERVAL_SECONDS = 6.0
        now = time.monotonic()
        check("a shorter request is clamped up to the interval",
              main_bot._phase2_deadline(0.2) - now >= 5.9,
              str(main_bot._phase2_deadline(0.2) - now))
        check("a longer request is honoured",
              main_bot._phase2_deadline(30.0) - now >= 29.9)
        check("the default is the interval",
              5.9 <= main_bot._phase2_deadline() - now <= 6.1)
        check("a malformed request falls back to the interval",
              5.9 <= main_bot._phase2_deadline("nonsense") - now <= 6.1)
    finally:
        config.TRADE_INTERVAL_SECONDS = saved


# ----------------------------------------------------------------- A2 ------
def t_a2_guards_contain_no_orderbook_fetch():
    """Structural: neither pre-submit guard may reach the network at all."""
    tree = ast.parse((ROOT / "main_bot.py").read_text(encoding="utf-8"))
    for name in ("_fresh_price_permit", "_fresh_signal_permit"):
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == name), None)
        check(f"{name} still exists", fn is not None)
        if fn is None:
            continue
        calls = {ast.unparse(c.func) for c in ast.walk(fn) if isinstance(c, ast.Call)}
        network = {c for c in calls if "get_orderbook" in c or "http" in c.lower()}
        check(f"{name} performs no order-book fetch", not network, str(network))


def t_a2_guard_never_calls_the_provider_even_when_it_is_available():
    """Behavioural counterpart: explode if the guard so much as tries."""
    import main_bot

    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def exploding(*_a, **_k):
        raise AssertionError("the pre-submit guard performed network I/O")

    round_key = ALIGNED_WINDOW
    try:
        replace(main_bot.orderbook, "get_orderbook", exploding)
        replace(main_bot.timer, "unix", lambda *_a, **_k: round_key + 100.0)
        replace(main_bot.price_ws, "fresh_snapshot",
                lambda *_a, **_k: (101.0, (round_key + 100) * 1000))
        replace(main_bot, "current_chainlink_twap", lambda: 101.0)
        replace(main_bot.config, "SIGNAL_DECISION_RULE", "final")
        snapshot = main_bot._freeze_book(_book(0.50))
        with contextlib.redirect_stdout(io.StringIO()):
            allowed = main_bot._fresh_price_permit(
                round_key, 100.0, "UP", book_token="11",
                book_snapshot=snapshot, chainlink_start=100.0)
        check("the guard reaches a decision without any network call",
              allowed in (True, False) or hasattr(allowed, "reason"), str(allowed))
    except AssertionError as exc:
        check("the guard reaches a decision without any network call",
              False, str(exc))
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def t_a2_slow_orderbook_does_not_block_the_broker_lock():
    """A deliberately slow order-book provider must not stall the broker.

    BEFORE: _fresh_price_permit did an 8s-timeout, once-retried REST read
    (so up to ~16s) from inside PaperBroker._lock.

    Be precise about what that blocked. `cash_balance()` and `summary()` take
    `ledger._lock`, NOT `broker._lock`, so the dashboard's cash figure was
    never held up by the guard - this test asserts that separately so the
    claim stays honest. What `broker._lock` really guards is `_rules` (the
    venue market-rules cache every entry AND every stop-loss exit needs),
    `sell_shares`'s durable write, and `_reject`. A 16s guard under that lock
    stalls a stop-loss exit, which is worse than a stale display number.

    The control case runs the OLD shape deliberately, so this test is proven
    able to detect the regression rather than passing vacuously.
    """
    import tempfile
    import main_bot
    import paper_trade
    from tests_paper import _broker

    SLOW = 0.6

    def slow_orderbook(*_a, **_k):
        time.sleep(SLOW)
        return _book(0.50)

    def worst_waits(broker, work):
        """Run `work`; return (worst broker-lock wait, worst cash_balance)."""
        lock_worst, cash_worst = 0.0, 0.0
        done = threading.Event()

        def run():
            try:
                work()
            finally:
                done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        time.sleep(0.05)
        while not done.is_set():
            t0 = time.monotonic()
            with broker._lock:
                pass
            lock_worst = max(lock_worst, time.monotonic() - t0)
            t1 = time.monotonic()
            broker.cash_balance()
            cash_worst = max(cash_worst, time.monotonic() - t1)
        t.join(timeout=5)
        return lock_worst, cash_worst

    with tempfile.TemporaryDirectory() as tmp:
        broker = _broker(tmp)

        # CONTROL: the old shape - a slow REST read inside the broker lock.
        def old_shape():
            with broker._lock:
                slow_orderbook()

        control_lock, control_cash = worst_waits(broker, old_shape)
        check("control: a slow read INSIDE the broker lock does stall it",
              control_lock >= SLOW * 0.5,
              f"worst broker-lock wait {control_lock:.3f}s")
        check("control: cash_balance was never on that lock anyway",
              control_cash < 0.1, f"{control_cash:.3f}s")

        # ACTUAL: the guard the broker now runs under its lock is local-only,
        # and the read it needs happens outside.
        snapshot = main_bot._freeze_book(_book(0.50))
        saved_rule = main_bot.config.SIGNAL_DECISION_RULE
        real_get = main_bot.orderbook.get_orderbook
        calls = {"n": 0}

        def counted(*a, **k):
            calls["n"] += 1
            return slow_orderbook(*a, **k)

        main_bot.orderbook.get_orderbook = counted
        try:
            main_bot.config.SIGNAL_DECISION_RULE = "final"

            def new_shape():
                # main_bot pre-fetches off the lock...
                counted("11")
                # ...then the broker runs a purely local guard under it.
                with broker._lock:
                    paper_trade._pre_submit_guard_error(
                        lambda: main_bot._fresh_price_permit(
                            ALIGNED_WINDOW, 100.0, "UP", explain=True,
                            book_token="11", book_snapshot=snapshot,
                            chainlink_start=100.0))

            with contextlib.redirect_stdout(io.StringIO()):
                actual_lock, actual_cash = worst_waits(broker, new_shape)
            check("the guard itself issues no order-book read",
                  calls["n"] == 1, f"{calls['n']} reads (1 = the pre-fetch)")
            check("a slow order-book read no longer stalls the broker lock",
                  actual_lock < SLOW * 0.5,
                  f"worst {actual_lock:.3f}s vs control {control_lock:.3f}s")
            check("cash_balance stays free throughout",
                  actual_cash < 0.1, f"{actual_cash:.3f}s")
        finally:
            main_bot.orderbook.get_orderbook = real_get
            main_bot.config.SIGNAL_DECISION_RULE = saved_rule


def t_a2_main_bot_prefetches_the_guard_book_off_the_loop():
    src = (ROOT / "main_bot.py").read_text(encoding="utf-8")
    check("the guard snapshot is read on a worker thread before submitting",
          "guard_book = _freeze_book(await asyncio.to_thread(" in src)
    check("the snapshot is handed to the guard",
          "book_snapshot=guard_book," in src)
    check("a snapshot that cannot be read refuses the order",
          "could not read a SIG BOOK " in src
          and "snapshot for the pre-submit guard" in src)


def t_a2_frozen_book_is_immutable():
    import main_bot
    bids, asks = _book(0.50)
    frozen = main_bot._freeze_book((bids, asks))
    check("a frozen book is a tuple pair", isinstance(frozen, tuple)
          and all(isinstance(side, tuple) for side in frozen))
    bids.append({"price": "0.99", "size": "1"})
    check("mutating the source list cannot change the frozen snapshot",
          len(frozen[0]) == 1, str(frozen[0]))
    check("an unreadable book freezes to None",
          main_bot._freeze_book(None) is None
          and main_bot._freeze_book("nonsense") is None)


# ----------------------------------------------------------------- A3 ------
def t_a3_book_vote_classifies_every_state():
    import main_bot
    cases = [
        ("valid two-sided book", _book(0.50), main_bot.BOOK_OK),
        ("empty book", ((), ()), main_bot.BOOK_EMPTY),
        ("bids only (one-sided)", _book(0.50, asks=False), main_bot.BOOK_ONE_SIDED),
        ("asks only (one-sided)", _book(0.50, bids=False), main_bot.BOOK_ONE_SIDED),
        ("no read at all", None, main_bot.BOOK_UNAVAILABLE),
        ("malformed read", "nonsense", main_bot.BOOK_UNAVAILABLE),
    ]
    for name, book, expected in cases:
        vote, state = main_bot._book_vote(book)
        check(f"{name} -> {expected}", state == expected, f"got {state}")
        if expected != main_bot.BOOK_OK:
            check(f"{name} casts no vote", vote is None, str(vote))
    vote, _ = main_bot._book_vote(_book(0.50))
    check("a valid book still votes a side", vote in ("UP", "DOWN"), str(vote))


def t_a3_empty_and_unavailable_are_never_treated_as_an_abstention():
    import main_bot
    check("empty and unavailable are both classed as 'no read'",
          main_bot.BOOK_EMPTY in main_bot.BOOK_NO_READ
          and main_bot.BOOK_UNAVAILABLE in main_bot.BOOK_NO_READ)
    check("a genuinely one-sided book is NOT 'no read' - it really abstains",
          main_bot.BOOK_ONE_SIDED not in main_bot.BOOK_NO_READ)
    check("a valid book is not 'no read'",
          main_bot.BOOK_OK not in main_bot.BOOK_NO_READ)


def t_a3_stale_or_failed_refresh_is_unavailable_not_empty():
    """A stale book raises out of orderbook.get_orderbook, so the caller
    holds no book at all. That must classify as `unavailable`, never as an
    empty book that quietly votes None."""
    import main_bot

    def stale(*_a, **_k):
        raise ValueError("CLOB book response is stale in hand")

    book = None
    try:
        book = stale()
    except ValueError:
        book = None
    vote, state = main_bot._book_vote(book)
    check("a stale/raising read classifies as unavailable",
          state == main_bot.BOOK_UNAVAILABLE, state)
    check("a stale read casts no vote", vote is None, str(vote))


def t_a3_guard_refuses_an_unread_snapshot_rather_than_abstaining():
    import main_bot
    saved = []

    def replace(obj, name, value):
        saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    round_key = ALIGNED_WINDOW
    try:
        replace(main_bot.timer, "unix", lambda *_a, **_k: round_key + 100.0)
        replace(main_bot.price_ws, "fresh_snapshot",
                lambda *_a, **_k: (101.0, (round_key + 100) * 1000))
        replace(main_bot, "current_chainlink_twap", lambda: 101.0)
        replace(main_bot.config, "SIGNAL_DECISION_RULE", "final")
        for label, snapshot in (("missing", None), ("empty", ((), ()))):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                allowed = main_bot._fresh_price_permit(
                    round_key, 100.0, "UP", book_token="11",
                    book_snapshot=snapshot, chainlink_start=100.0,
                    explain=True)
            reason = getattr(allowed, "reason", allowed)
            check(f"a {label} snapshot fails closed",
                  allowed is not True, str(allowed))
            check(f"a {label} snapshot names SIG BOOK as the reason",
                  "SIG BOOK" in str(reason), str(reason))
    finally:
        for obj, name, value in reversed(saved):
            setattr(obj, name, value)


def t_a3_signal_permit_book_source_needs_a_real_snapshot():
    import main_bot
    for label, snapshot, expected in (
            ("valid", _book(0.50), True),
            ("empty", ((), ()), False),
            ("missing", None, False)):
        vote, _state = main_bot._book_vote(snapshot)
        got = main_bot._fresh_signal_permit(
            "book", vote or "UP", round_key=ALIGNED_WINDOW,
            book_snapshot=snapshot)
        check(f"_fresh_signal_permit('book') with a {label} snapshot -> {expected}",
              got is expected, str(got))


def t_a3_submit_gate_prefers_a_refresh_but_falls_back_to_the_last_read():
    src = (ROOT / "main_bot.py").read_text(encoding="utf-8")
    check("a failed taper probe records no book rather than an empty one",
          "selected_bids, selected_asks = None, None" in src)
    check("the submit gate classifies the refreshed read",
          "submit_book_side, submit_book_state = _book_vote(" in src)
    check("the submit gate falls back to the last valid read",
          "submit_book_side, submit_book_state = (\n                        final_book_side, final_book_state)" in src
          or "final_book_side, final_book_state)" in src)
    check("the submit gate refuses when nothing was ever read",
          "no usable order book for this " in src and "leg at submission" in src)
    check("liquidity_signal is no longer called on the raw selected book",
          "orderbook.liquidity_signal(selected_bids, selected_asks)" not in src)


# ----------------------------------------------------------------- B3 ------
def t_b3_shutdown_clears_every_provider():
    """Setup zeroes a set of providers; teardown must clear the same set."""
    tree = ast.parse((ROOT / "run_feeds.py").read_text(encoding="utf-8"))
    assigned_none = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if node.value.value is not None:
            continue
        for t in node.targets:
            if (isinstance(t, ast.Attribute) and t.attr.startswith("_")
                    and getattr(t.value, "id", "") == "main_bot"):
                assigned_none.setdefault(t.attr, []).append(node.lineno)

    providers = {n for n in assigned_none if n.endswith("_provider")}
    check("run_feeds still manages the strategy providers",
          len(providers) >= 5, str(sorted(providers)))
    once_only = sorted(n for n in providers if len(assigned_none[n]) < 2)
    check("every provider is cleared on BOTH setup and shutdown",
          not once_only, f"cleared only once: {once_only}")
    check("the provider that shutdown used to miss is now cleared",
          len(assigned_none.get("_unsettled_exposure_provider", [])) >= 2,
          str(assigned_none.get("_unsettled_exposure_provider")))


def t_b3_cleared_provider_leaves_the_unsettled_guard_closed():
    """With no provider installed the guard is simply off; with a broken one
    it must refuse to open new risk rather than assume zero exposure."""
    import main_bot
    saved_provider = main_bot._unsettled_exposure_provider
    saved_cap = main_bot.config.MAX_UNSETTLED_EXPOSURE
    try:
        main_bot.config.MAX_UNSETTLED_EXPOSURE = 50.0
        main_bot._unsettled_exposure_provider = None
        check("a cleared provider disables the cap rather than inventing one",
              main_bot._unsettled_exposure_block() is None)

        def broken():
            raise RuntimeError("ledger is closed")

        main_bot._unsettled_exposure_provider = broken
        blocked = main_bot._unsettled_exposure_block()
        check("a stale/raising provider fails CLOSED",
              blocked is not None and "refusing to commit more" in blocked,
              str(blocked))
    finally:
        main_bot._unsettled_exposure_provider = saved_provider
        main_bot.config.MAX_UNSETTLED_EXPOSURE = saved_cap


# ---------------------------------------------------------------- runner ---
def main() -> int:
    def run(fn, is_async=False):
        global F
        try:
            asyncio.run(fn()) if is_async else fn()
        except Exception as exc:
            F += 1
            print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")

    print("hardening regression suite\n")
    for fn in [v for k, v in sorted(globals().items())
               if k.startswith("t_") and not asyncio.iscoroutinefunction(v)]:
        run(fn)
    for fn in [v for k, v in sorted(globals().items())
               if k.startswith("t_") and asyncio.iscoroutinefunction(v)]:
        run(fn, True)
    print(f"\n{P} passed, {F} failed")
    return 1 if F else 0


if __name__ == "__main__":
    raise SystemExit(main())
