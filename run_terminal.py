#!/usr/bin/env python3
"""Run the BTC 5-min bot with the quant terminal attached.

    python run_terminal.py            # bot + dashboard
    python run_terminal.py --selftest # layout only, no bot, no network
    python main_bot.py                # bot with the same 60s TWAP service

The bot runs exactly as it does under main_bot.py: the same run_bot()
coroutine, the same price_ws.stream_price(), the same decisions. This file
adds three things and nothing else — read-only probes, a stdout sink, and a
render task.

Keys:  Ctrl+C or q quit   r force repaint
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dashboard import TerminalState, build, glyphs, snapshot  # noqa: E402
from dashboard.renderer import PlainRenderer  # noqa: E402

def _positive_env(name: str, default: str) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be a positive number") from exc
    if value <= 0 or value == float("inf") or value != value:
        raise RuntimeError(f"{name} must be a finite positive number")
    return value


HZ = _positive_env("TERM_REFRESH_HZ", "6")
SLOW_FRAME_MS = _positive_env("TERM_SLOW_FRAME_MS", "45")


# --------------------------------------------------------------- keyboard ---
class Keys:
    """Non-blocking key reader. Never calls input() — that would fight the
    bot's own kill-switch thread and block the loop."""

    def __init__(self) -> None:
        self.q: list[str] = []
        self._q_lock = threading.Lock()
        self._stop = threading.Event()
        self._t: threading.Thread | None = None
        self._restore = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._t is not None and self._t.is_alive():
            return
        if not sys.stdin.isatty():
            return
        if sys.platform == "win32":
            self._t = threading.Thread(target=self._win, daemon=True)
        else:
            try:
                import termios
                import tty
                fd = sys.stdin.fileno()
                self._restore = (fd, termios.tcgetattr(fd))
                tty.setcbreak(fd)
            except Exception:
                return
            self._t = threading.Thread(target=self._posix, daemon=True)
        self._t.start()

    def _posix(self) -> None:
        import select
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([sys.stdin], [], [], 0.2)
                if r:
                    with self._q_lock:
                        self.q.append(sys.stdin.read(1))
            except Exception as exc:
                self.last_error = f"keyboard reader failed: {type(exc).__name__}: {exc}"[:200]
                return

    def _win(self) -> None:
        import msvcrt
        while not self._stop.is_set():
            try:
                if msvcrt.kbhit():
                    with self._q_lock:
                        self.q.append(msvcrt.getwch())
                else:
                    time.sleep(0.05)
            except Exception as exc:
                self.last_error = f"keyboard reader failed: {type(exc).__name__}: {exc}"[:200]
                return

    def pop(self) -> list[str]:
        with self._q_lock:
            out, self.q = self.q, []
            return out

    def stop(self) -> None:
        self._stop.set()
        thread = self._t
        self._t = None
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1.0)
            if thread.is_alive() and self.last_error is None:
                self.last_error = "keyboard reader did not stop within 1s"
        if self._restore:
            try:
                import termios
                fd, old = self._restore
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            except Exception as exc:
                self.last_error = f"terminal restore failed: {type(exc).__name__}"


# ------------------------------------------------------------------ tasks ---
async def price_poller(state: TerminalState, stop: threading.Event) -> None:
    """Sample price_ws.latest_price. The module has no message timestamp
    (audit M8), so freshness is measured from the last CHANGE — labelled as
    such on the panel rather than presented as a socket heartbeat."""
    import price_ws
    while not stop.is_set():
        state.push_spot(price_ws.latest_snapshot()[0])
        await asyncio.sleep(0.05)


async def clock(state: TerminalState, stop: threading.Event) -> None:
    import timer
    while not stop.is_set():
        try:
            state.round_label = timer.current_round_window_et()
        except Exception as exc:
            state.event("DASH", f"round clock failed: {type(exc).__name__}", "warn")
        await asyncio.sleep(1.0)


async def render_loop(state: TerminalState, stop: threading.Event, keys: Keys,
                      renderer) -> None:
    import main_bot
    g = glyphs()
    hz = HZ
    slow_streak = 0
    seen_repaints = 0
    with renderer:
        while not stop.is_set():
            t0 = time.perf_counter()
            for ch in keys.pop():
                if ch in ("r", "R", "\x0c"):
                    renderer.repaint()
                elif ch in ("q", "Q", "\x03"):
                    # A software-level quit independent of OS signal delivery.
                    # Ctrl+C alone is not always enough: some SSH/tmux setups
                    # and supervised processes can consume the raw \x03 byte
                    # here without ever raising SIGINT, leaving no other way
                    # to stop the bot from inside the dashboard.
                    stop.set()
                    main_bot.stop_event.set()

            # A reconnect (or anything else that may have disturbed the
            # terminal) asks for one clean full redraw here, on the render
            # loop's own thread. The renderer paints only changed rows, so a
            # row corrupted from outside matches _prev and would never be
            # rewritten on its own. Reading a counter keeps the network side
            # free of any renderer reference: it raises a number, and the
            # single render loop decides when to act on it. Geometry is
            # untouched - only the paint is forced.
            with state.lock():
                pending = state.repaint_requests
            if pending != seen_repaints:
                seen_repaints = pending
                renderer.repaint()

            snap = snapshot(state, session_trades=main_bot.session_trades)
            if isinstance(renderer, PlainRenderer):
                renderer.status(snap)
            else:
                renderer.cols, renderer.rows = renderer.size()
                frame = build(snap, renderer.cols, renderer.rows, g)
                renderer.draw(frame)

            ms = (time.perf_counter() - t0) * 1000.0
            with state.lock():
                state.render_ms.append(ms)
                state.frames += 1
            # Self-protecting: the engine has priority. If frames get slow,
            # slow the dashboard down rather than steal loop time.
            if ms > SLOW_FRAME_MS:
                slow_streak += 1
                if slow_streak >= 5 and hz > 1.0:
                    hz = max(1.0, hz / 2)
                    state.event("DASH", f"frame {ms:.0f}ms - refresh reduced to {hz:.1f}Hz", "warn")
                    slow_streak = 0
            else:
                slow_streak = 0
            await asyncio.sleep(max(0.0, 1.0 / hz - (time.perf_counter() - t0)))


# ------------------------------------------------------------------- main ---
async def _legacy_run() -> None:
    """Compatibility alias; the old bypass path is intentionally removed."""
    await run(paper=True)


async def run(*, paper: bool = True) -> None:
    """Use the same hardened runner/accounting as ``run_feeds.py``."""
    from run_feeds import run as run_hardened
    await run_hardened(dash=True, paper=paper)


def selftest() -> None:
    """Render one frame at many sizes with an EMPTY state.

    Deliberately not a demo mode: nothing is populated, so every field shows
    `--`. It proves geometry, not plausibility.
    """
    from dashboard.renderer import render_row
    from dashboard.theme import enable_utf8_output

    # The self-test writes its preview directly instead of constructing a
    # Renderer, so it must perform the same output initialization itself.
    # Pass the exact stream used below: redirected Windows stdout commonly
    # starts as cp1252 even when the terminal renderer normally uses UTF-8.
    enable_utf8_output(sys.stdout)
    state = TerminalState()
    state.round_label = "--"
    snap = snapshot(state, session_trades=[])
    g = glyphs()
    bad = 0
    for cols, rows in [(200, 60), (160, 50), (120, 40), (110, 34), (100, 30),
                       (90, 28), (84, 24), (80, 24), (72, 20), (60, 18), (40, 12)]:
        frame = build(snap, cols, rows, g)
        if len(frame) != rows:
            print(f"  FAIL {cols}x{rows}: {len(frame)} rows"); bad += 1; continue
        for i, row in enumerate(frame):
            w = sum(len(t) for t, _ in row)
            if w != cols:
                print(f"  FAIL {cols}x{rows} row {i}: width {w}"); bad += 1; break
        else:
            print(f"  ok {cols}x{rows}")
    if not bad:
        frame = build(snap, *(int(x) for x in os.environ.get("TERM_PREVIEW", "120x40").split("x")), g)
        sys.stdout.write("\n".join(render_row(r) for r in frame) + "\n")
    print("selftest:", "PASS" if not bad else f"{bad} FAILURES")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true",
                    help="render the layout with no bot and no data")
    ap.add_argument("--live", action="store_true",
                    help="explicitly enable live-wallet order submission")
    args = ap.parse_args()
    if args.selftest:
        selftest()
    from run_feeds import run_quietly
    raise SystemExit(run_quietly(run(paper=not args.live)))
