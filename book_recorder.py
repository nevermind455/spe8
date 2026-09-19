#!/usr/bin/env python3
"""Record the real order book, full depth, both legs, every tick.

    python3 book_recorder.py record            # append snapshots, Ctrl+C to stop
    python3 book_recorder.py resolve           # fill in who actually won
    python3 book_recorder.py stats             # what the tape holds

Why this exists
---------------
`signal_journal.py` records a book as four numbers per leg: best bid, best
ask, and the total size stacked on each side. That is enough to ask "was the
top of book cheap", and it is NOT enough to ask "would my order have filled".
A $2.50 order does not trade at the best ask. It walks the ladder: some
shares at the best ask, the rest at whatever sits behind it, and it fills
nothing at all if the whole ladder above it is thinner than the venue
minimum. A total-volume column cannot answer that, because it has already
thrown away the shape that decides it.

So this records the ladder itself. Every level, every price, both sides,
both legs, stamped with the venue's own book timestamp and with how many
seconds were left in the round. A backtest run against this file is run
against the depth that was actually sitting there at the second the edge
existed - which is the only version of the question worth asking.

Nobody sells this history, so it only exists if you record it. Start the
recorder before you need the data, not after.

The file is JSONL: one self-describing object per snapshot, so a schema
change appends new keys instead of silently misaligning old rows the way a
CSV header does. Snapshots with no round, no book, or a book the freshness
checks in `orderbook.py` refused are not written - a gap is honest, an
invented level is not.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time as _t

ROOT = pathlib.Path(__file__).parent
sys.path.insert(0, str(ROOT))

import market_discovery                       # noqa: E402
import orderbook                              # noqa: E402
import timer                                  # noqa: E402
from accounting import resolution as res      # noqa: E402
from chainlink_strike import ChainlinkStrike   # noqa: E402

TAPE = ROOT / "book_tape.jsonl"
WINNERS = ROOT / "book_tape_winners.json"
BINANCE_SPOT = "https://api.binance.com/api/v3/ticker/price"
WINDOW_SECONDS = 300
# Resolution is not instant. A round younger than this is simply not ready.
SETTLE_GRACE_SECONDS = 300
# A sample may only claim to be a round's strike if it landed this close to
# the boundary. Same rule, and same reason, as signal_journal.py.
BOUNDARY_GRACE = 5.0
SCHEMA = 1


# --------------------------------------------------------------- recording ---
def _spot(session) -> float | None:
    try:
        r = session.get(BINANCE_SPOT, params={"symbol": "BTCUSDT"}, timeout=6)
        r.raise_for_status()
        return float(r.json()["price"])
    except Exception:
        return None


def _depth(token) -> dict:
    """Both ladders in full, plus the venue timestamp that licensed them.

    Raises whatever `orderbook` raises. A stale, crossed, empty or
    future-dated book is a book we would have refused to trade on, so it is
    refused here too rather than recorded as if it were tradeable.
    """
    bids, asks = orderbook.get_orderbook(token)
    report = orderbook.LAST_TIMESTAMP_REPORT or {}
    return {
        "token": str(token),
        # [price, size] pairs, best first, exactly as the fill would walk
        # them. Strings, because these are decimal prices and a float round
        # trip is a silent edit of recorded data.
        "bids": [[lvl["price"], lvl["size"]] for lvl in bids],
        "asks": [[lvl["price"], lvl["size"]] for lvl in asks],
        "book_ts": report.get("exchange_ts_s"),
        "quiet_s": report.get("quiet_s"),
        "held_s": report.get("held_s"),
    }


async def record(interval: float, path: pathlib.Path = TAPE) -> int:
    import requests

    session = requests.Session()
    strike = ChainlinkStrike()
    strike.start()
    print(f"[TAPE] recording full depth every {interval:.1f}s into {path.name}")
    print("[TAPE] the first round is skipped: a boundary TWAP needs a "
          "connection that predates the boundary")

    bn_strike: dict[int, float] = {}
    written = skipped = 0
    try:
        with path.open("a", encoding="utf-8") as fh:
            while True:
                now = timer.unix()
                window = timer.window_start(now)
                secs_left = window + WINDOW_SECONDS - now
                try:
                    tokens = market_discovery.get_tokens_for_current_round(window)
                except Exception as exc:
                    print(f"[TAPE] discovery skipped "
                          f"({type(exc).__name__}: {str(exc)[:70]})")
                    tokens = None
                try:
                    spot = await asyncio.to_thread(_spot, session)
                except Exception:
                    spot = None
                if spot is not None and secs_left >= WINDOW_SECONDS - BOUNDARY_GRACE:
                    bn_strike.setdefault(window, spot)
                for stale in [w for w in bn_strike if w < window - 3600]:
                    bn_strike.pop(stale, None)

                if not tokens:
                    skipped += 1
                    await asyncio.sleep(max(0.5, interval))
                    continue
                try:
                    up = await asyncio.to_thread(_depth, tokens["up_token_id"])
                    dn = await asyncio.to_thread(_depth, tokens["down_token_id"])
                except Exception as exc:
                    # One unreadable tick costs one snapshot, never the run.
                    skipped += 1
                    print(f"[TAPE] book skipped "
                          f"({type(exc).__name__}: {str(exc)[:70]})")
                    await asyncio.sleep(max(0.5, interval))
                    continue

                fh.write(json.dumps({
                    "schema": SCHEMA,
                    "wall": round(now, 3),
                    "window": window,
                    "secs_left": round(secs_left, 2),
                    "condition_id": tokens.get("condition_id"),
                    "cl_strike": strike.strike_for(window),
                    "cl_now": strike.current_value(),
                    "bn_strike": bn_strike.get(window),
                    "bn_now": spot,
                    "up": up,
                    "down": dn,
                }, separators=(",", ":")) + "\n")
                fh.flush()
                written += 1
                if written % 50 == 0:
                    print(f"[TAPE] {written} snapshots, {skipped} gaps")
                await asyncio.sleep(max(0.5, interval))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"\n[TAPE] stopped: {written} snapshots written, {skipped} gaps")
    finally:
        await strike.stop()
    return 0


# ----------------------------------------------------------------- reading ---
def snapshots(path: pathlib.Path = TAPE) -> list[dict]:
    """Every snapshot on the tape, oldest first.

    A truncated final line - the recorder was killed mid-write - is dropped,
    not raised on. Anything else malformed is dropped too: a backtest that
    silently mixes in half a record is worse than one that is short by one.
    """
    if not path.exists():
        raise SystemExit(f"no tape at {path} - run `book_recorder.py record` first")
    out = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and "window" in row and "up" in row:
                out.append(row)
    out.sort(key=lambda r: (int(r["window"]), float(r.get("wall") or 0)))
    return out


def winners(path: pathlib.Path = WINNERS) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# --------------------------------------------------------------- resolving ---
def resolve(tape: pathlib.Path = TAPE, out: pathlib.Path = WINNERS,
            dry_run: bool = False) -> int:
    """Ask Polymarket who actually won each recorded round.

    The real resolution of every window is half the point of the tape. A
    backtest that scores its fills against a guess about the outcome is a
    daydream with extra steps, however real the book was.
    """
    import journal_resolve

    have = winners(out)
    windows = sorted({int(r["window"]) for r in snapshots(tape)})
    now = timer.unix()
    todo = [w for w in windows if str(w) not in have]
    ready = [w for w in todo if now - (w + WINDOW_SECONDS) >= SETTLE_GRACE_SECONDS]
    young = len(todo) - len(ready)

    print(f"tape holds {len(windows)} rounds, {len(have)} already resolved, "
          f"{len(ready)} ready, {young} too young")
    if dry_run:
        for w in ready[:20]:
            print(f"  would fetch btc-updown-5m-{w}")
        if len(ready) > 20:
            print(f"  ... and {len(ready) - 20} more")
        return 0

    resolved = unresolved = missing = 0
    for window in ready:
        tokens = journal_resolve.tokens_for_past_window(window)
        if not tokens:
            missing += 1
            continue
        outcome = res.fetch(tokens["condition_id"])
        if not outcome.resolved:
            unresolved += 1
            continue
        up = outcome.payout(tokens["up_token_id"])
        if up == 1.0:
            have[str(window)] = "UP"
        elif up == 0.0:
            have[str(window)] = "DOWN"
        else:
            # Neither 1 nor 0 is a split or a bad parse. Naming it keeps it
            # out of the accuracy numbers instead of corrupting them.
            have[str(window)] = "SPLIT"
        resolved += 1
        print(f"  {window} -> {have[str(window)]}")

    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(have, indent=1), encoding="utf-8")
    tmp.replace(out)
    print(f"\nresolved {resolved} this run, {len(have)} total in {out.name}")
    if unresolved:
        print(f"  {unresolved} closed but not resolved on-chain yet - retry later")
    if missing:
        print(f"  {missing} could not be found on Gamma at all")
    return 0


# ------------------------------------------------------------------- stats ---
def stats(tape: pathlib.Path = TAPE, out: pathlib.Path = WINNERS) -> int:
    rows = snapshots(tape)
    if not rows:
        print("tape is empty")
        return 0
    have = winners(out)
    by_window: dict[int, int] = {}
    levels = 0
    for r in rows:
        by_window[int(r["window"])] = by_window.get(int(r["window"]), 0) + 1
        for leg in ("up", "down"):
            book = r.get(leg) or {}
            levels += len(book.get("bids") or ()) + len(book.get("asks") or ())
    windows = sorted(by_window)
    span = (rows[-1]["wall"] - rows[0]["wall"]) / 3600.0
    resolved = sum(1 for w in windows if str(w) in have)
    print(f"{len(rows)} snapshots over {len(windows)} rounds ({span:.1f}h)")
    print(f"{levels} book levels recorded "
          f"({levels / max(1, len(rows)):.1f} per snapshot, both legs)")
    print(f"{min(by_window.values())}-{max(by_window.values())} snapshots per round, "
          f"median {sorted(by_window.values())[len(by_window) // 2]}")
    print(f"{resolved}/{len(windows)} rounds resolved "
          f"({len(windows) - resolved} still need `resolve`)")
    first = _t.strftime('%Y-%m-%d %H:%M', _t.gmtime(rows[0]["wall"]))
    last = _t.strftime('%Y-%m-%d %H:%M', _t.gmtime(rows[-1]["wall"]))
    print(f"covering {first} to {last} UTC")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    rec = sub.add_parser("record", help="append full-depth snapshots")
    rec.add_argument("--interval", type=float, default=2.0,
                     help="seconds between snapshots (default 2)")
    rs = sub.add_parser("resolve", help="fetch the real winner of each round")
    rs.add_argument("--dry-run", action="store_true")
    sub.add_parser("stats", help="summarise the tape")
    args = ap.parse_args(argv)

    if args.cmd == "record":
        return asyncio.run(record(args.interval))
    if args.cmd == "resolve":
        return resolve(dry_run=args.dry_run)
    return stats()


if __name__ == "__main__":
    raise SystemExit(main())
