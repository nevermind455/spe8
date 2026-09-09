"""Pure layout.

`build(snap, cols, rows, g, now)` returns exactly `rows` rows, each exactly
`cols` visible characters wide. No I/O, no clock reads, no bot imports — the
snapshot carries everything. That is what lets the tests assert on geometry
at 200 terminal sizes without a TTY.
"""
from __future__ import annotations

import copy
import math
import time
from collections.abc import Mapping
from typing import Any

from .state import MISSING, TerminalState
from .safety import terminal_text
from .theme import Glyphs, Style, pnl_style, state_fg, state_style
from .widgets import (DIM, FAINT, PAPER, RULE, Row, blank,
                      candles, chip, fit, giant_digits,
                      histogram, hsplit, join, kv, meter, pad, panel,
                      sparkline, table, trunc)

# ---------------------------------------------------------------- snapshot ---


def snapshot(st: TerminalState, session_trades: list | None = None) -> dict[str, Any]:
    """Copy everything the renderer needs under one lock acquisition."""
    with st.lock():
        now_wall = time.time()
        now_mono = time.monotonic()
        book = st.best_book()
        down_book = st.best_down_book()
        balance = copy.deepcopy(st.balance.value) if isinstance(st.balance.value, Mapping) else None
        tokens = copy.deepcopy(st.tokens.value) if isinstance(st.tokens.value, Mapping) else None
        last_order = (copy.deepcopy(st.last_order.value)
                      if isinstance(st.last_order.value, Mapping) else None)
        if last_order is not None:
            last_order = {
                "side": terminal_text(last_order.get("side"), 16),
                "amount": _finite(last_order.get("amount")),
                "ok": bool(last_order.get("ok", False)),
                "error": terminal_text(last_order.get("error"), 1000)
                if last_order.get("error") else None,
            }
        accounting = (copy.deepcopy(st.accounting)
                      if isinstance(st.accounting, Mapping) else {})
        trade_source = session_trades if session_trades is not None else st.trades
        trades = [copy.deepcopy(t) for t in list(trade_source) if isinstance(t, Mapping)]
        snap = {
            "now": now_wall,
            "mono": now_mono,
            "uptime": max(0.0, now_mono - st.started_mono),
            "round_label": str(st.round_label),
            "round_key": st.round_key,
            "seconds_left": (int(st.seconds_left)
                             if _finite(st.seconds_left) is not None else None),
            "health": st.feed_health(now_mono),
            "spot": _finite(st.spot.value),
            "spot_age": st.spot_changed.age_at(now_mono),
            "spot_status": st.spot_changed.status_at(5.0, 20.0, now_mono),
            "chainlink": _finite(st.chainlink.value),
            "chainlink_age": st.chainlink.age_at(now_mono),
            "chainlink_ms": _finite(st.chainlink.latency_ms),
            "chainlink_repeat": st.chainlink_repeat,
            "chainlink_calls": st.chainlink.count,
            "start_price": _finite(st.start_price.value),
            "start_price_src": st.start_price.source,
            "start_chainlink": _finite(st.start_chainlink.value),
            "start_chainlink_src": st.start_chainlink.source,
            "book": book,
            "down_book": down_book,
            "book_age": st.book.age_at(now_mono),
            "book_ms": _finite(st.book.latency_ms),
            "book_token": st.book_token,
            "down_book_token": st.down_book_token,
            "book_status": st.book.status_at(90.0, 400.0, now_mono),
            "sig_price": terminal_text(st.sig_price.value, 16) if st.sig_price.value else None,
            "sig_book": terminal_text(st.sig_book.value, 16) if st.sig_book.value else None,
            "sig_chainlink": terminal_text(st.sig_chainlink.value, 16) if st.sig_chainlink.value else None,
            "decision": terminal_text(st.decision.value, 16) if st.decision.value else None,
            "decision_forced": st.decision_forced,
            "last_order": last_order,
            "last_order_ms": _finite(st.last_order.latency_ms),
            "last_order_error": terminal_text(st.last_order_error, 1000)
            if st.last_order_error else None,
            "telemetry_error": terminal_text(st.telemetry_error, 1000)
            if st.telemetry_error else None,
            "orders_ok": st.orders_ok,
            "orders_fail": st.orders_fail,
            "staked": _finite(st.staked) or 0.0,
            "stake_curve": [number for _, value in st.stake_curve
                            if (number := _finite(value)) is not None],
            "cancel": st.cancel.value,
            "balance": balance,
            "balance_age": st.balance.age_at(now_mono),
            "tokens": tokens,
            "token_fallback": st.token_fallback,
            "candles": [(c.o, c.h, c.l, c.c) for c in st.candles],
            "candle_t": [c.t for c in st.candles],
            "events": copy.deepcopy(list(st.events)[-80:]),
            "trades": trades,
            "exits": [copy.deepcopy(e) for e in list(st.exits)[-40:]],
            "stop_status": copy.deepcopy(st.stop_status) if isinstance(st.stop_status, Mapping) else {},
            "absent": dict(st.absent),
            "overlay": copy.deepcopy(st.overlay),
            "loop_status": st.loop_beat.status_at(3.0, 12.0, now_mono),
            "loop_age": st.loop_beat.age_at(now_mono),
            "bands": tuple(st.bands),
            "bands_enabled": bool(st.bands_enabled),
            "bet_size": _finite(st.bet_size),
            "trade_window": st.trade_window,
            "max_buy_price": _finite(st.max_buy_price),
            "min_buy_price": _finite(st.min_buy_price),
            "render_ms": (_finite(sum(st.render_ms) / len(st.render_ms))
                          if st.render_ms else None),
            "frames": st.frames,
            "mode": terminal_text(st.mode, 16),
            "accounting": accounting,
        }
    return snap


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _up_down_share(trades, field: str) -> str | None:
    """Count this-round journal sides for one signal. Missing sides stay out."""
    if not trades:
        return None
    up = sum(1 for t in trades if str(t.get(field) or "").upper() == "UP")
    down = sum(1 for t in trades if str(t.get(field) or "").upper() == "DOWN")
    if up + down == 0:
        return None
    return f"UP {up}  DOWN {down}"


# ------------------------------------------------------------------ sizing ---
class Sizing:
    """Row/column budget for the current terminal, recomputed every frame."""

    def __init__(self, cols: int, rows: int) -> None:
        self.cols, self.rows = cols, rows
        self.wide = cols >= 150
        self.narrow = cols < 84
        self.stack = cols < 64
        self.status_rows = 3 if rows >= 20 else 1

        fixed = 1 + 1 + self.status_rows + 1        # header, health, status, footer
        avail = rows - fixed
        self.top = self.mid = self.bot = 0
        if avail >= 23:
            extra = avail - 23
            self.top = min(18, 11 + int(extra * 0.40))
            self.mid = min(16, 8 + int(extra * 0.35))
            self.bot = avail - self.top - self.mid
        elif avail >= 17:
            self.top = 9
            self.mid = avail - 9
        elif avail >= 8:
            self.top = avail
        else:
            self.top = max(0, avail)
        self.show_mid = self.mid >= 6
        self.show_bot = self.bot >= 6
        if not self.show_mid:
            self.top += self.mid
            self.mid = 0
        if not self.show_bot:
            self.top += self.bot // 2
            self.mid += self.bot - self.bot // 2
            self.bot = 0
        self.show_chart = cols >= 84 and self.top >= 8
        self.show_dist = cols >= 118


def L(short: str, long: str, s: Sizing) -> str:
    return long if not s.narrow else short


def _trade_success(result) -> bool:
    """Did this journal row actually become a position?

    BUGFIX: the old set listed only the explicit rejections, so every
    `skipped_*` row - an attempt the bot deliberately did NOT submit, because
    the leg was unfillable, the signal moved, or the pair would lose - fell
    through to True and rendered as a green FILL. The band log makes that
    obvious: a band that never traded would show a column of fills.
    """
    outcome = str(result or "").lower()
    if outcome.startswith("skipped"):
        return False
    return outcome not in {"", "rejected_or_unsubmitted", "failed", "rejected"}


def _trade_sent(result) -> bool:
    """Did this journal row ever reach submission?

    BUGFIX: a `skipped_*` row is an attempt the bot deliberately did NOT
    submit - the leg was unfillable, the signal moved, or the pair would
    lose. It never reached the broker, so it is neither a successful send nor
    a failed one, but every counter here derived failures as "not a success"
    (`len(trades) - oks`) and so booked it as a failed SEND. One skipped
    phase-2 attempt rendered as `SENT FAIL 1 50%` in SIGNAL DISTRIBUTION
    beside `ORDERS OK/FAIL 1 / 0` and `SEND RATE 100%` in the cash panel,
    which reads as a dashboard contradiction; the trade and band logs marked
    the same row a red `REJECT`, indistinguishable from a venue refusal.

    `rejected_or_unsubmitted` deliberately stays a failed SEND. Its name
    spans both cases and the row alone cannot separate them, so it keeps the
    bucket it has always had rather than being silently reclassified.
    """
    return not str(result or "").lower().startswith("skipped")


# ------------------------------------------------------------------ pieces ---
def _header(snap, cols: int, g: Glyphs, s: Sizing) -> Row:
    clk = time.strftime("%H:%M:%S", time.localtime(snap["now"]))
    secs = snap["seconds_left"]
    left = [
        (" BTC-5M CLOBv2 ", Style("white", "ink", bold=True)),
        (" ", PAPER),
        (f"{snap['mode']:<5}", Style("purple", bold=True)),
        (g.v, RULE),
        (" BTC/USDT UP-DOWN ", Style("ink")),
        (g.v, RULE),
        (f" {snap['round_label']} ", Style("blue", bold=True)),
        (g.v, RULE),
    ]
    if secs is not None:
        armed = snap["trade_window"] is not None and secs <= snap["trade_window"]
        left += [(f" T-{secs:03d} ", Style("white", "red" if armed else "blue", bold=True))]
    else:
        left += [(" T-??? ", FAINT)]
    right = [
        (f" render {snap['render_ms']:.0f}ms " if snap["render_ms"] else " render --  ", FAINT),
        (g.v, RULE),
        (f" {clk} ", Style("ink", "cream", bold=True)),
    ]
    rw = sum(len(t) for t, _ in right)
    lw = sum(len(t) for t, _ in left)
    mid = cols - lw - rw
    if mid < 0:
        return pad(left + right, cols)
    return pad(left + [(" " * mid, PAPER)] + right, cols)


def _health(snap, cols: int, g: Glyphs, s: Sizing) -> Row:
    order = ["BINANCE WS", "POLY WS", "POLY BOOK", "CHAINLINK", "USER WS",
             "DATABASE", "RECONCILE", "SETTLEMENT", "GAMMA API", "LOOP"]
    short = {"BINANCE WS": "BNC", "POLY WS": "PWS", "POLY BOOK": "BOOK",
             "CHAINLINK": "CHNL", "USER WS": "UWS", "DATABASE": "DB",
             "RECONCILE": "RECN", "SETTLEMENT": "SETL", "GAMMA API": "GMMA",
             "LOOP": "LOOP"}
    h = snap["health"]
    row: Row = []
    for name in order:
        st = h.get(name, "WAIT")
        label = short[name] if s.narrow else name
        mark = {"OK": "\u25cf", "STALE": "\u25d1", "DISCONNECTED": "\u25cb",
                "WAIT": "\u25cb", "ABSENT": "\u00b7"}.get(st, "\u00b7")
        row.append((f" {mark}{label} ", state_style(st)))
        row.append((" ", PAPER))
        if sum(len(t) for t, _ in row) > cols - 8:
            break
    return pad(row, cols)


def _kpi(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    bal = snap["balance"]
    acct = snap.get("accounting") or {}
    w = cols - 2
    body: list[Row] = []

    # Five-row bold block words. A terminal cannot change font size, so
    # height and heavy glyphs are what make the cash figure giant.
    cash = _finite(bal.get("balance")) if isinstance(bal, Mapping) else None
    if cash is not None:
        head = f"${cash:,.2f}".replace(",", "")
        hstyle = Style("green", bold=True)
    else:
        head, hstyle = MISSING, Style("faint")
    body += giant_digits(head, w, hstyle, g=g)
    body.append([(g.h * w, RULE)])

    ok, fail = snap["orders_ok"], snap["orders_fail"]
    tot = ok + fail
    body.append([
        (fit("ORDERS OK/FAIL", w - 12, "<"), DIM),
        (fit(str(ok), 6, ">"), Style("green", bold=True) if ok else DIM),
        (" /", DIM),
        (fit(str(fail), 4, ">"), Style("red", bold=True) if fail else DIM),
    ])
    body.append(kv("SEND RATE", f"{ok / tot * 100:.0f}%" if tot else MISSING, w,
                   Style("ink") if tot else FAINT))
    body.append(kv("CUM STAKE SENT", f"${snap['staked']:,.2f}", w, Style("blue", bold=True)))
    body.append(kv("BET SIZE", f"${snap['bet_size']:,.2f}" if snap["bet_size"] else MISSING, w))
    body.append([(g.h * w, RULE)])
    realized = _finite(acct.get("realized_pnl"))
    unreal = _finite(acct.get("unrealized_mark_to_bid"))
    total = _finite(acct.get("total_pnl", acct.get("equity_pnl")))
    exposure = _finite(acct.get("pending_cost"))
    win_rate = _finite(acct.get("win_rate"))
    if acct:
        # Total equity is withheld while any open position cannot be marked,
        # which is every position between its round ending and Polymarket
        # publishing the resolution. Say why it is blank instead of showing a
        # bare `--`, and never hide realized PnL behind it: that figure is
        # exact the moment a market resolves.
        pending = acct.get("unmarkable_positions") or 0
        body.append(kv("TOTAL PNL",
                       f"${total:+,.4f}" if total is not None else
                       (f"{MISSING} {pending} unsettled" if pending else MISSING),
                       w, pnl_style(total)))
        body.append(kv(
            "REALIZED / UNREAL" + (" (part)" if pending else ""),
            f"${realized:+,.4f} / ${unreal:+,.4f}"
            if realized is not None and unreal is not None else
            (f"${realized:+,.4f} / {MISSING}" if realized is not None else MISSING),
            w, pnl_style(realized)))
        body.append(kv(
            "EXPOSURE / WINRATE",
            f"${exposure:,.2f} / {win_rate * 100:.1f}%"
            if exposure is not None and win_rate is not None else
            (f"${exposure:,.2f} / {MISSING}" if exposure is not None else MISSING),
            w, Style("ink")))
        body.append(kv("WINS / LOSSES",
                       f"{acct.get('wins', 0)} / {acct.get('losses', 0)}", w))
    else:
        body.append(kv("TOTAL PNL", MISSING, w, FAINT))
        body.append(kv("REALIZED / UNREAL", MISSING, w, FAINT))
        body.append(kv("EXPOSURE / WINRATE", MISSING, w, FAINT))
        body.append(kv("WINS / LOSSES", MISSING, w, FAINT))

    age = snap["balance_age"]
    note = ("paper" if snap["mode"] == "PAPER" else
            (f"{age / 60:,.0f}m old" if (bal is not None and age is not None) else "no pnl"))
    title = "PAPER CASH" if snap["mode"] == "PAPER" else "ACCOUNT USDC"
    title_style = (Style("white", "ink", bold=True) if snap["mode"] == "PAPER"
                   else Style("ink", "cream", bold=True))
    note_style = Style("ink", bold=True) if snap["mode"] == "PAPER" else FAINT
    return panel(title, body, cols, rows, g, right_note=note,
                 title_style=title_style, note_style=note_style)


def _round_panel(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    b = snap["book"]
    down = snap["down_book"]
    spot = snap["spot"]
    price_to_beat = snap["start_chainlink"]
    running = snap["chainlink"]
    dist = (running - price_to_beat
            if running is not None and price_to_beat is not None else None)
    body: list[Row] = []
    body.append(kv("ROUND", snap["round_label"], w, Style("blue", bold=True)))
    body.append(kv("SECONDS LEFT", f"{snap['seconds_left']}s" if snap["seconds_left"] is not None else MISSING, w))
    body.append(kv("PRICE TO BEAT",
                   f"${price_to_beat:,.2f}" if price_to_beat is not None else MISSING,
                   w, Style("amber", bold=True) if price_to_beat is not None else FAINT))
    running_txt = f"${running:,.2f}" if running is not None else MISSING
    if running is not None and snap["chainlink_repeat"]:
        running_txt += f"  x{snap['chainlink_repeat'] + 1} same"
    body.append(kv("RUNNING PRICE", running_txt, w,
                   pnl_style(dist) if dist is not None else
                   (Style("ink", bold=True) if running is not None else FAINT)))
    body.append(kv("DIST TO BEAT", f"{dist:+,.2f}" if dist is not None else MISSING, w,
                   pnl_style(dist)))
    body.append(kv(L("BINANCE SPOT", "BINANCE SPOT (aux)", s),
                   f"${spot:,.2f}" if spot is not None else MISSING, w,
                   Style("ink") if spot is not None else FAINT))
    body.append([(g.h * w, RULE)])
    ask = f"{b['ask']:.3f}" if b.get("ask") is not None else MISSING
    bid = f"{b['bid']:.3f}" if b.get("bid") is not None else MISSING
    spr = f"{b['spread']:.3f}" if b.get("spread") is not None else MISSING
    body.append(kv(L("UP ASK/BID", "UP  ASK / BID", s), f"{ask} / {bid}", w,
                   Style("ink") if b else FAINT))
    body.append(kv(L("UP SPREAD", "UP  SPREAD", s), spr, w, Style("ink") if b else FAINT))
    down_ask = f"{down['ask']:.3f}" if down.get("ask") is not None else MISSING
    down_bid = f"{down['bid']:.3f}" if down.get("bid") is not None else MISSING
    body.append(kv(L("DN ASK/BID", "DOWN ASK / BID", s),
                   f"{down_ask} / {down_bid}", w,
                   Style("ink") if down else FAINT))
    body.append([(g.h * w, RULE)])
    d = snap["decision"]
    body.append(kv("CURRENT SIDE", d or MISSING, w,
                   Style("green" if d == "UP" else "red", bold=True) if d else FAINT))
    mom = snap["sig_price"]
    binance_move = (spot - snap["start_price"]
                    if spot is not None and snap["start_price"] is not None else None)
    if mom and binance_move is not None:
        mom_txt = f"{mom}  {binance_move:+,.2f}"
    elif mom:
        mom_txt = mom
    else:
        mom_txt = MISSING
    body.append(kv("MOMENTUM", mom_txt, w,
                   Style("green" if mom == "UP" else "red", bold=True) if mom else FAINT))
    details = [item for item in
               list((snap.get("accounting") or {}).get("open_position_details") or [])
               if isinstance(item, Mapping)]
    tokens = snap.get("tokens") or {}
    selected_token = (tokens.get("up_token_id") if d == "UP" else
                      tokens.get("down_token_id") if d == "DOWN" else None)
    position = next((p for p in details
                     if str(p.get("token_id")) == str(selected_token)), None)
    if position is None and details:
        position = max(details, key=lambda p: _finite(p.get("latest_fill_wall")) or 0.0)
    entry = _finite(position.get("average_entry_price")) if position else None
    shares = _finite(position.get("shares")) if position else None
    cost = _finite(position.get("cost")) if position else None
    body.append(kv("ENTRY PRICE", f"{entry:.5f}" if entry is not None else MISSING,
                   w, Style("ink") if position else FAINT))
    body.append(kv("SHARES", f"{shares:.5f}" if shares is not None else MISSING,
                   w, Style("ink") if position else FAINT))
    body.append(kv("POSITION COST", f"${cost:.5f}" if cost is not None else MISSING,
                   w, Style("ink") if position else FAINT))
    body.append(kv("WIN PAYOUT", f"${shares:.5f}" if shares is not None else MISSING,
                   w, Style("ink") if position else FAINT))
    body.append(kv("EDGE / FAIR VALUE", MISSING, w, FAINT))
    body.append(kv("ASK BAND (MIN-MAX)",
                   (f"{snap['min_buy_price']:.2f}-{snap['max_buy_price']:.2f}"
                    if snap.get("min_buy_price") is not None and snap["max_buy_price"]
                    else (f"{snap['max_buy_price']:.2f}" if snap["max_buy_price"] else MISSING)),
                   w, Style("amber", bold=True)))
    return panel("ROUND / POSITION", body, cols, rows, g, right_note="")


def _chart(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    inner_w, inner_h = cols - 2, rows - 2
    body = candles(snap["candles"], inner_w, inner_h, g,
                   ref=snap["start_price"], last=snap["spot"],
                   times=snap.get("candle_t"))
    spot, open_price = snap["spot"], snap["start_price"]
    secs = snap["seconds_left"]
    above = None if (spot is None or open_price is None) else spot >= open_price
    bits = [f"{TerminalState.CANDLE_SECONDS}s"]
    if secs is not None:
        bits.append(f"T-{secs:03d}")
    bits.append("ABOVE OPEN" if above else
                ("BELOW OPEN" if above is False else "no Binance open"))
    note_style = (Style("green", bold=True) if above else
                  (Style("red", bold=True) if above is False else FAINT))
    return panel("BTC/USDT  BINANCE @trade", body, cols, rows, g,
                 right_note="  ".join(bits), note_style=note_style)


def _status_strip(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    secs = snap["seconds_left"]
    armed = secs is not None and snap["trade_window"] is not None and secs <= snap["trade_window"]
    sides = [snap["sig_price"], snap["sig_book"], snap["sig_chainlink"]]
    named = [x for x in sides if x]
    agree = f"{max((named.count(x) for x in set(named)), default=0)}/3" if named else MISSING
    d = snap["decision"]
    lo = snap["last_order"]
    cap = snap["max_buy_price"]
    floor = snap.get("min_buy_price")
    acct = snap.get("accounting") or {}
    open_positions = acct.get("open_positions")
    settle_health = snap["health"].get("SETTLEMENT", "ABSENT")
    cells = [
        ("ROUND", "ROUND", f"T-{secs:03d}" if secs is not None else "--", "ARMED" if armed else "IDLE"),
        ("SIGNAL", "SIGNAL", agree, "OK" if named else "WAIT"),
        ("GATE", "ENTRY GATE", "ABSENT", "ABSENT"),
        ("SIDE", "SIDE", d or "--", "UP" if d == "UP" else ("DOWN" if d == "DOWN" else "WAIT")),
        ("MOM", "MOMENTUM", snap["sig_price"] or "--",
         snap["sig_price"] or "WAIT"),
        ("EDGE", "EDGE", "ABSENT", "ABSENT"),
        ("PAIR", "PAIR COST", "ABSENT", "ABSENT"),
        ("RISK", "RISK", "ABSENT", "ABSENT"),
        ("ASKCAP", "ASK GUARD",
         (f"{floor:.2f}-{cap:.2f}" if floor is not None and cap else
          (f"<={cap:.2f}" if cap else "--")),
         "OK" if cap else "WAIT"),
        ("BOOK", "BOOK FRESH", snap["book_status"], snap["book_status"]),
        ("EXEC", "EXECUTION", ("OK" if lo["ok"] else "FAIL") if lo else "IDLE",
         ("OK" if lo["ok"] else "FAIL") if lo else "IDLE"),
        ("POS", "POSITION", str(open_positions) if open_positions is not None else "ABSENT",
         "OK" if open_positions is not None else "ABSENT"),
        ("SETTLE", "SETTLEMENT", settle_health, settle_health),
    ]
    if s.stack:
        cells = [c for c in cells if c[3] != "ABSENT"]

    n = len(cells)
    base = max(6, cols // n)
    widths = [base] * n
    widths[-1] += cols - base * n
    if widths[-1] < 6:                       # never let the last cell collapse
        widths = [max(6, (cols - 6) // n)] * n
        widths[-1] = cols - sum(widths[:-1])
    use_long = base >= 12

    if rows == 1:
        row: Row = []
        for (short, long, val, st), wd in zip(cells, widths):
            row += chip(f"{trunc(short, max(1, wd - 4))}:{val}", st, wd)
        return [pad(row, cols)]

    top: Row = []
    label: Row = []
    value: Row = []
    for (short, long, val, st), wd in zip(cells, widths):
        style = state_style(st)
        name = long if use_long else short
        top += [(g.h * wd, RULE)]
        label += [(fit(" " + trunc(name, wd - 1), wd, "<"), Style("dim", "cream"))]
        value += [(fit(" " + trunc(val, wd - 1), wd, "<"), style)]
    return [pad(top, cols), pad(label, cols), pad(value, cols)][:rows]


def _pipeline(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    """The bot's real path, one node per row, connected by a left rail."""
    w = cols - 2
    b = snap["book"]
    tok = snap["tokens"] or {}

    def node(name, value, st, extra=""):
        return (name, value, st, extra)

    def px(value):
        """One side of the book can be empty; that is a state, not an error."""
        return f"{value:.3f}" if value is not None else MISSING

    spot_ok = snap["spot_status"]
    chainlink_status = snap["health"].get("CHAINLINK", "WAIT")
    nodes = [
        node("PRICE WS", f"${snap['spot']:,.2f}" if snap["spot"] is not None else "--", spot_ok,
             f"{snap['spot_age']:.1f}s" if snap["spot_age"] is not None else ""),
        node("RUNNING PRICE",
             f"${snap['chainlink']:,.2f}" if snap["chainlink"] is not None else "--",
             chainlink_status,
             f"same x{snap['chainlink_repeat'] + 1}" if snap["chainlink_repeat"] else
             (f"{snap['chainlink_ms']:.0f}ms" if snap["chainlink_ms"] is not None else "")),
        node("ROUND CLOCK", f"T-{snap['seconds_left']:03d}" if snap["seconds_left"] is not None else "--",
             "OK" if snap["seconds_left"] is not None else "WAIT", snap["round_label"]),
        node("PRICE TO BEAT",
             f"${snap['start_chainlink']:,.2f}" if snap["start_chainlink"] is not None else "--",
             "OK" if snap["start_chainlink"] is not None else "WAIT",
             snap["start_chainlink_src"]),
        node("MARKET DISC", trunc(str(tok.get("slug") or "--"), 26),
             "FAIL" if snap["token_fallback"] else ("OK" if tok else "WAIT"),
             "PREV WINDOW" if snap["token_fallback"] else ""),
        node("BOOK FETCH",
             f"a{px(b.get('ask'))} b{px(b.get('bid'))}" if b else "--",
             snap["book_status"], f"{snap['book_ms']:.0f}ms" if snap["book_ms"] else ""),
        node("SIG PRICE", snap["sig_price"] or "--", snap["sig_price"] or "WAIT"),
        node("SIG BOOK", snap["sig_book"] or "--", snap["sig_book"] or "WAIT"),
        node("SIG CHAINLINK", snap["sig_chainlink"] or "--", snap["sig_chainlink"] or "WAIT"),
        node("FINAL DECISION", snap["decision"] or "--", snap["decision"] or "WAIT",
             ""),
        node("CANCEL OPEN", "OK" if snap["cancel"] else ("FAIL" if snap["cancel"] is False else "--"),
             "OK" if snap["cancel"] else ("FAIL" if snap["cancel"] is False else "IDLE")),
        node("PAPER FOK" if snap["mode"] == "PAPER" else "PLACE FOK",
             (("FILLED" if snap["mode"] == "PAPER" else "SENT")
              if snap["last_order"]["ok"] else "REJECTED") if snap["last_order"] else "--",
             ("OK" if snap["last_order"]["ok"] else "FAIL") if snap["last_order"] else "IDLE",
             trunc(snap["last_order_error"] or "", 22) if snap["last_order"] and not snap["last_order"]["ok"] else
             (f"{snap['last_order_ms']:.0f}ms" if snap["last_order_ms"] else "")),
        node("TRADE LOG", f"{len(snap['trades'])} rows", "OK" if snap["trades"] else "IDLE", "csv"),
    ]

    # Collapse in a fixed order when the panel is short. The tail of the
    # pipeline (decision -> order) is what matters during a trade window, so
    # the head collapses first.
    room = rows - 2
    if len(nodes) > room:
        sigs = [n for n in nodes if n[0].startswith("SIG ")]
        merged = "/".join((n[1] if n[1] != "--" else "-") for n in sigs)
        nodes = [n for n in nodes if not n[0].startswith("SIG ")]
        nodes.insert(6, node("SIGNALS P/B/C", merged, snap["decision"] or "WAIT"))
    for name in ("CANCEL OPEN", "RUNNING PRICE", "TRADE LOG", "MARKET DISC", "ROUND CLOCK"):
        if len(nodes) <= room:
            break
        nodes = [n for n in nodes if n[0] != name]

    name_w = 14 if not s.narrow else 11
    val_w = max(6, min(20, w - name_w - 8))
    body: list[Row] = []
    for i, (name, value, st, extra) in enumerate(nodes):
        rail = g.tee_r if i else g.tl
        arrow = g.arrow_r
        style = state_fg(st)
        row: Row = [
            (rail + g.h, RULE), (arrow, Style("rule")),
            ("[", RULE), (fit(name, name_w, "<"), Style("ink", bold=True)), ("]", RULE),
            (" ", PAPER), (fit(value, val_w, "<"), style),
        ]
        used = sum(len(t) for t, _ in row)
        if extra and w - used > 4:
            row.append((" " + fit(extra, w - used - 1, "<"), FAINT))
        body.append(pad(row, w))

    absent = snap["absent"]
    if len(body) < rows - 2:
        body.append([(g.h * w, RULE)])
        body.append([(fit(" NOT IN THIS BUILD (rendered as ABSENT, never faked)", w, "<"),
                      Style("purple", bold=True))])
        keys = list(absent.keys())
        per = max(1, w // 15)
        for i in range(0, len(keys), per):
            chunk = " ".join(f"{k}" for k in keys[i:i + per])
            body.append([(fit("  " + chunk, w, "<"), FAINT)])

    return panel("DECISION PIPELINE  (observed)", body, cols, rows, g,
                 right_note=snap["decision"] or "no side")


def _matrix(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    b = snap["book"]
    down_book = snap["down_book"]
    all_hdr = ["SIDE", "BID", "ASK", "SPRD", "DEPTH", "FAIR", "EDGE", "POS", "COST", "PNL", "STATUS"]
    all_cw = [5, 6, 6, 5, 7, 5, 5, 4, 5, 5, 11]
    # Keep order, drop the least useful columns until the row fits exactly.
    drop_order = ["POS", "COST", "PNL", "EDGE", "FAIR", "DEPTH", "SPRD"]
    keep = list(all_hdr)
    def fits(cols_kept):
        return sum(all_cw[all_hdr.index(h)] + 1 for h in cols_kept) <= w
    for d in drop_order:
        if fits(keep):
            break
        keep.remove(d)
    hdr = keep
    cw = [all_cw[all_hdr.index(h)] for h in keep]
    idx = [all_hdr.index(h) for h in keep]

    def f(v, spec="{:.3f}"):
        value = _finite(v)
        return spec.format(value) if value is not None else MISSING

    up = [
        ("UP", Style("green", bold=True)),
        (f(b.get("bid")), Style("green")),
        (f(b.get("ask")), Style("red")),
        (f(b.get("spread")), Style("ink")),
        (f(b.get("depth_ask"), "{:,.0f}"), Style("blue")),
        (MISSING, FAINT), (MISSING, FAINT), (MISSING, FAINT),
        (MISSING, FAINT), (MISSING, FAINT),
        (snap["book_status"], state_fg(snap["book_status"])),
    ]
    dn = [
        ("DOWN", Style("red", bold=True)),
        (f(down_book.get("bid")), Style("green")),
        (f(down_book.get("ask")), Style("red")),
        (f(down_book.get("spread")), Style("ink")),
        (f(down_book.get("depth_ask"), "{:,.0f}"), Style("blue")),
        (MISSING, FAINT), (MISSING, FAINT),
        (MISSING, FAINT), (MISSING, FAINT), (MISSING, FAINT),
        (snap["book_status"], state_fg(snap["book_status"])),
    ]
    up = [up[i] for i in idx]
    dn = [dn[i] for i in idx]

    body = table(hdr, cw, [up, dn], w, max_rows=2)
    body.append([(g.h * w, RULE)])
    body.append(meter("FEED", 1.0 if snap["spot_status"] == "OK" else
                      (0.5 if snap["spot_status"] == "STALE" else 0.0), w, g))
    body.append(meter("BOOK", 1.0 if snap["book_status"] == "OK" else
                      (0.5 if snap["book_status"] == "STALE" else 0.0), w, g))
    body.append(meter("LOOP", 1.0 if snap["loop_status"] == "OK" else
                      (0.5 if snap["loop_status"] == "STALE" else 0.0), w, g))
    body.append([(fit(f" token {trunc(str(snap['book_token'] or '--'), max(6, w - 8))}", w, "<"), FAINT)])
    return panel("MARKET MATRIX", body, cols, rows, g,
                 right_note="UP + DOWN live books")


def _equity(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    curve = snap["stake_curve"]
    body: list[Row] = []
    h = max(2, rows - 5)
    body += sparkline(curve, w, h, g, baseline=0.0)
    body.append([(g.h * w, RULE)])
    body.append(kv("CUM STAKE SENT", f"${snap['staked']:,.2f}", w, Style("blue", bold=True)))
    acct = snap.get("accounting") or {}
    pnl = _finite(acct.get("total_pnl", acct.get("equity_pnl")))
    if pnl is not None:
        body.append(kv("TOTAL PNL", f"${pnl:+,.4f}", w, pnl_style(pnl)))
    else:
        # Same rule as the cash panel: report what has settled rather than
        # blanking the row for the minutes a resolution takes to publish.
        realized = _finite(acct.get("realized_pnl"))
        body.append(kv("REALIZED PNL",
                       f"${realized:+,.4f}" if realized is not None else MISSING,
                       w, pnl_style(realized)))

    return panel("STAKE / PNL", body, cols, rows, g,
                 right_note="paper mark-to-bid" if snap["mode"] == "PAPER" else "stake")


def _dist(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    trades = snap["trades"]
    ups = sum(1 for t in trades if str(t.get("side")).upper() == "UP")
    dns = sum(1 for t in trades if str(t.get("side")).upper() == "DOWN")
    # Only rows that actually reached submission belong in the SENT bars;
    # see _trade_sent. A skip still counts as a DECIDE, so DECIDE and SENT
    # now legitimately disagree whenever the bot declined to send - which is
    # the real shape of the round, and what the cash panel has always shown.
    sent_rows = [t for t in trades if _trade_sent(t.get("result"))]
    oks = sum(1 for t in sent_rows if _trade_success(t.get("result")))
    fails = len(sent_rows) - oks
    skipped = len(trades) - len(sent_rows)
    decided, sent = ups + dns, oks + fails
    buckets = [("DECIDE UP", ups), ("DECIDE DOWN", dns),
               ("SENT OK", oks), ("SENT FAIL", fails)]
    totals = [decided, decided, sent, sent]
    if skipped:
        # Carried as its own bar rather than the panel note: at this panel's
        # width the note is already truncated mid-string by panel(), so a
        # count parked there would not survive to the screen. Added only when
        # there is something to report, so a round with no skips lays out
        # exactly as before and the extra row costs nothing.
        buckets.append(("NOT SENT", skipped))
        totals.append(len(trades))
    body = histogram(buckets, w, len(buckets), g, totals=totals)
    body.append([(g.h * w, RULE)])
    pb = sum(1 for t in trades if t.get("price_side") and t.get("price_side") == t.get("book_side"))
    body.append(kv("PRICE=BOOK AGREE", f"{pb}/{len(trades)}" if trades else MISSING, w,
                   Style("ink") if trades else FAINT))
    body.append(kv("CHAINLINK UP SHARE",
                   f"{sum(1 for t in trades if t.get('chainlink_side') == 'UP')}/{len(trades)}"
                   if trades else MISSING, w, Style("amber") if trades else FAINT))
    mom_dist = _up_down_share(trades, "price_side")
    body.append(kv("MOMENTUM DIST", mom_dist or MISSING, w,
                   Style("ink") if mom_dist else FAINT))
    return panel("SIGNAL DISTRIBUTION", body, cols, rows, g, right_note="this round")


def _trades(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    all_hdr = ["TIME", "SIDE", "COST", "P", "B", "C", "RESULT", "PNL"]
    all_cw = [8, 5, 6, 3, 3, 3, 6, 4]
    keep = list(all_hdr)
    for d in ("PNL", "C", "B", "P", "COST"):
        if sum(all_cw[all_hdr.index(h)] + 1 for h in keep) <= w:
            break
        keep.remove(d)
    hdr = keep
    cw = [all_cw[all_hdr.index(h)] for h in keep]
    idx = [all_hdr.index(h) for h in keep]
    rows_data = []
    for t in reversed(snap["trades"][-40:]):
        ok = _trade_success(t.get("result"))
        # A row the bot never submitted is not a rejection - see _trade_sent.
        was_sent = _trade_sent(t.get("result"))
        cells = [
            (str(t.get("time_et", ""))[-11:-3] or "--", DIM),
            (str(t.get("side", "--")), Style("green" if t.get("side") == "UP" else "red", bold=True)),
            (f"${(_finite(t.get('amount')) or 0.0):.2f}", Style("ink")),
            (str(t.get("price_side") or "-")[:2], FAINT),
            (str(t.get("book_side") or "-")[:2], FAINT),
            (str(t.get("chainlink_side") or "-")[:2], FAINT),
            (("FILL" if snap["mode"] == "PAPER" else "SENT") if ok
             else ("REJECT" if was_sent else "SKIP"),
             Style("green" if ok else ("red" if was_sent else "amber"),
                   bold=True)),
            (MISSING, FAINT),
        ]
        rows_data.append([cells[i] for i in idx])
    body = table(hdr, cw, rows_data, w, max_rows=max(1, rows - 3))
    return panel("RECENT TRADES", body, cols, rows, g,
                 right_note="this round | history: paper_orders.jsonl"
                 if snap["mode"] == "PAPER" else "this round | history: ledger")


def _band_trades(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    """Phase-1 band entries, and nothing else.

    Replaces the old EXITS / STOP panel. Deliberately excludes every phase-2
    row: the band is a separate entry rule being measured on its own fills,
    and one table holding both cannot answer "how is the band doing" - the
    SIDE and RESULT columns would be describing two different strategies at
    once. The same reasoning the exits panel used for keeping exits out of
    RECENT TRADES applies here to keeping phase 2 out of the band log.
    """
    w = cols - 2
    body: list[Row] = []
    bands = snap.get("bands") or ()
    if not snap.get("bands_enabled"):
        body.append(pad([(fit("bands are OFF (PHASE1_ENABLED=0)", w, "<"), FAINT)], w))
    elif not bands:
        body.append(pad([(fit("no band schedule configured", w, "<"), FAINT)], w))
    else:
        for start, end, low, high, gap in bands[:2]:
            body.append(pad([
                (fit(f"T-{int(start)}..T-{int(end)}", 13, "<"), DIM),
                (fit(f"{low:.2f}-{high:.2f}", 10, "<"), Style("ink")),
                (fit(f"every {gap:.0f}s", 10, "<"), FAINT),
            ], w))
        if len(bands) > 2:
            body.append(pad([
                (fit(f"+{len(bands) - 2} more windows", w, "<"), FAINT)], w))
    body.append(pad([(fit("", w, "<"), PAPER)], w))

    hdr = ["TIME", "SIDE", "COST", "SIG", "RESULT"]
    cw = [8, 5, 6, 4, 7]
    while sum(cw) + len(cw) > w and len(hdr) > 2:
        hdr.pop()
        cw.pop()
    rows_data = []
    for t in reversed(snap.get("trades") or []):
        # Only band. A phase-2 row in here would be exactly the mixing this
        # panel exists to avoid.
        if str(t.get("phase") or "") != "phase1":
            continue
        ok = _trade_success(t.get("result"))
        # A row the bot never submitted is not a rejection - see _trade_sent.
        was_sent = _trade_sent(t.get("result"))
        cells = [
            (str(t.get("time_et", ""))[-11:-3] or "--", DIM),
            (str(t.get("side", "--")),
             Style("green" if t.get("side") == "UP" else "red", bold=True)),
            (f"${(_finite(t.get('amount')) or 0.0):.2f}", Style("ink")),
            (str(t.get("price_side") or "-")[:2], FAINT),
            (("FILL" if snap["mode"] == "PAPER" else "SENT") if ok
             else ("REJECT" if was_sent else "SKIP"),
             Style("green" if ok else ("red" if was_sent else "amber"),
                   bold=True)),
        ]
        rows_data.append(cells[:len(hdr)])
    used = len(body)
    body += table(hdr, cw, rows_data, w, max_rows=max(1, rows - 3 - used))
    note = (f"{bands[0][2]:.2f}-{bands[0][3]:.2f} | band only"
            if bands and snap.get("bands_enabled") else "off")
    return panel("BAND TRADES", body, cols, rows, g, right_note=note)


def _positions(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    """What this round holds per side, at what average, and what it is worth.

    Takes the band log's slot in the default layout. PHASE1_ENABLED=0 is
    this repo's default, so that panel spent every frame saying bands are
    off, while the thing a paper run most needs on screen - the position
    itself - was either split across other panels (ROUND / POSITION shows a
    single leg, STAKE / PNL shows only account totals) or not shown at all.
    The band log is not gone: it reclaims this slot whenever bands are on.

    Every figure here is one the ledger already computed for this frame in
    Ledger.summary(mark=...) - shares, average_entry_price, cost, mark_bid
    and unrealized_to_bid per open position - so this panel re-derives
    nothing and cannot drift from STAKE / PNL. A leg the book cannot mark
    shows the missing marker rather than a zero, the same rule the rest of
    the dashboard follows.
    """
    w = cols - 2
    acct = snap.get("accounting") or {}
    details = [d for d in (acct.get("open_position_details") or ())
               if isinstance(d, Mapping)]
    tokens = snap.get("tokens") or {}
    up_id = str(tokens.get("up_token_id") or "")
    down_id = str(tokens.get("down_token_id") or "")

    def leg(token_id: str) -> dict | None:
        held = [d for d in details if str(d.get("token_id")) == token_id]
        if not token_id or not held:
            return None
        shares = sum(_finite(d.get("shares")) or 0.0 for d in held)
        cost = sum(_finite(d.get("cost")) or 0.0 for d in held)
        # Share-weighted, so several fills at different prices read as the
        # one average the position was actually built at - which is the
        # number that decides whether the current bid is a profit.
        priced = [(_finite(d.get("shares")) or 0.0,
                   _finite(d.get("average_entry_price"))) for d in held]
        avg = (sum(n * pr for n, pr in priced) / shares
               if shares and all(pr is not None for _, pr in priced) else None)
        bid = next((b for b in (_finite(d.get("mark_bid")) for d in held)
                    if b is not None), None)
        marks = [_finite(d.get("unrealized_to_bid")) for d in held]
        pnl = (sum(marks) if marks and all(m is not None for m in marks)
               else None)
        return {"shares": shares, "cost": cost, "avg": avg, "bid": bid,
                "value": None if bid is None else shares * bid, "pnl": pnl}

    legs = [("UP", leg(up_id)), ("DOWN", leg(down_id))]

    all_hdr = ["SIDE", "SHARES", "AVG", "COST", "BID", "VALUE", "PNL"]
    all_cw = [4, 7, 5, 7, 5, 7, 7]
    # Same adaptive trim the trade and matrix tables use, ordered by what can
    # be recovered elsewhere. BID and VALUE go first: PNL is derived from the
    # bid, and MARKET MATRIX prints both sides' live bid and ask. AVG and
    # COST go next - either can be reconstructed from the other plus SHARES.
    # SHARES is never dropped: it is the one figure in this row that appears
    # nowhere else on the dashboard, and shedding it to make room for COST
    # (which is what the first version of this column did) traded away the
    # more load-bearing number.
    keep = list(all_hdr)
    for drop in ("VALUE", "BID", "COST", "AVG"):
        if sum(all_cw[all_hdr.index(h)] + 1 for h in keep) <= w:
            break
        keep.remove(drop)
    hdr = keep
    cw = [all_cw[all_hdr.index(h)] for h in keep]
    idx = [all_hdr.index(h) for h in keep]

    def num(value, spec: str) -> str:
        v = _finite(value)
        return spec.format(v) if v is not None else MISSING

    def money(value, signed: bool = False) -> str:
        """Two decimals while they fit, whole dollars once they do not.

        A 7-wide cell truncates "$-100.00" to "$-100.0", which reads as a
        different number rather than as a clipped one. Dropping the cents
        past $99.99 keeps every figure honest at every width.
        """
        v = _finite(value)
        if v is None:
            return MISSING
        spec = f"{{:{'+' if signed else ''},.{0 if abs(v) >= 100 else 2}f}}"
        return "$" + spec.format(v)

    rows_data = []
    for name, held in legs:
        side_style = Style("green" if name == "UP" else "red", bold=True)
        if held is None:
            cells = [(name, side_style)] + [(MISSING, FAINT)] * 6
        else:
            cells = [
                (name, side_style),
                (num(held["shares"], "{:.3f}"), Style("ink")),
                (num(held["avg"], "{:.3f}"), Style("ink")),
                # Running total for this side, not the last fill: what the
                # leg has cost so far, fees in, alongside the average it was
                # built at. Both move on every fill.
                (money(held["cost"]), Style("ink")),
                (num(held["bid"], "{:.3f}"), Style("blue")),
                (money(held["value"]), Style("ink")),
                (money(held["pnl"], signed=True), pnl_style(held["pnl"])),
            ]
        rows_data.append([cells[i] for i in idx])
    body = table(hdr, cw, rows_data, w, max_rows=2)

    open_legs = [held for _, held in legs if held]
    values = [held["value"] for held in open_legs]
    marks = [held["pnl"] for held in open_legs]
    round_cost = sum(held["cost"] for held in open_legs) if open_legs else None
    round_value = (sum(values) if values and all(v is not None for v in values)
                   else None)
    round_pnl = (sum(marks) if marks and all(m is not None for m in marks)
                 else None)
    realized = _finite(acct.get("realized_pnl"))
    total = _finite(acct.get("total_pnl", acct.get("equity_pnl")))

    shares_total = sum(held["shares"] for held in open_legs) if open_legs else None
    up_leg, down_leg = legs[0][1], legs[1][1]
    both = " / ".join(
        f"{(h['shares'] if h else 0.0):,.3f}" for h in (up_leg, down_leg))
    # The label shortens rather than letting kv() ellipsise the value: the
    # numbers are the point of the row, the wording is not.
    both_label = "UP / DOWN SHARES" if w >= 32 else "UP / DN SH"
    footer = [
        ("shares", kv(both_label, both if open_legs else MISSING, w,
                      Style("ink") if open_legs else FAINT)),
        ("total_shares", kv("TOTAL SHARES",
                            f"{shares_total:,.3f}" if shares_total is not None
                            else MISSING, w,
                            Style("ink") if shares_total is not None else FAINT)),
        ("cost", kv("ROUND COST",
                    f"${round_cost:,.2f}" if round_cost is not None else MISSING,
                    w, Style("ink") if round_cost is not None else FAINT)),
        ("value", kv("ROUND VALUE (BID)",
                     f"${round_value:,.2f}" if round_value is not None else MISSING,
                     w, Style("ink") if round_value is not None else FAINT)),
        ("round", kv("ROUND PNL",
                     f"${round_pnl:+,.2f}" if round_pnl is not None else MISSING,
                     w, pnl_style(round_pnl))),
        ("realized", kv("REALIZED",
                        f"${realized:+,.2f}" if realized is not None else MISSING,
                        w, pnl_style(realized))),
        ("total", kv("TOTAL PNL",
                     f"${total:+,.2f}" if total is not None else MISSING,
                     w, pnl_style(total))),
    ]
    # panel() truncates its body silently, which would eat whichever rows sit
    # last - so shed the most derivable lines first and keep the P&L that
    # cannot be reconstructed by eye from the two rows above.
    avail = (rows - 2) - len(body)
    if avail >= 2:
        body.append([(g.h * w, RULE)])
        avail -= 1
        for name in ("value", "total_shares", "cost", "realized"):
            if len(footer) <= avail:
                break
            footer = [f for f in footer if f[0] != name]
        body += [row for _, row in footer[:avail]]

    # Positions on tokens that are not this round's legs are still real money
    # - a previous round settles ~85s after it ends - so say so rather than
    # letting this panel read as the whole book.
    others = sum(1 for d in details
                 if str(d.get("token_id")) not in (up_id, down_id))
    note = "mark to bid" + (f" | +{others} settling" if others else "")
    return panel("POSITIONS", body, cols, rows, g, right_note=note)


def _holdings_or_bands(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    """One slot, whichever of the two is actually in play.

    Bands own it while PHASE1 is on, because then the band log is the record
    of what that phase did. Otherwise it shows the live position, which is
    what the default (phase-2 only) profile needs and which the band panel
    could only ever report as switched off.
    """
    return (_band_trades(snap, cols, rows, g, s) if snap.get("bands_enabled")
            else _positions(snap, cols, rows, g, s))


def _stop_panel(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    """What the stop loss is watching, and what it has sold.

    `state.exits`/`state.stop_status` are populated every frame by
    run_feeds._dashboard_inner and copied into the snapshot, but until now no
    panel read either key: an operator running with STOP_LOSS_ENABLED=1 had
    zero visibility into what the stop was watching or had done, despite the
    full ledger-to-UI pipeline already being wired end to end.
    """
    w = cols - 2
    status = snap.get("stop_status") or {}
    body: list[Row] = []
    if not status.get("enabled"):
        body.append(pad([(fit("stop loss is OFF (STOP_LOSS_ENABLED=0)", w, "<"), FAINT)], w))
    else:
        armed = bool(status.get("armed"))
        body.append(kv("STATUS", "ARMED" if armed else "watching", w,
                       Style("amber", bold=True) if armed else FAINT))
        trigger, floor = status.get("trigger"), status.get("floor")
        body.append(kv(
            "TRIGGER / FLOOR",
            f"{trigger:.3f} / {floor:.3f}"
            if trigger is not None and floor is not None else MISSING,
            w, Style("ink")))
        held = status.get("held") or []
        if not held:
            body.append(pad([(fit("no legs currently held", w, "<"), FAINT)], w))
        else:
            for leg in held[:3]:
                bid = leg.get("bid")
                shares = _finite(leg.get("shares")) or 0.0
                value = (f"{shares:.2f} sh @ bid {bid:.3f}" if bid is not None
                         else f"{shares:.2f} sh @ bid --")
                body.append(kv(
                    f"HELD {leg.get('side', '--')}", value, w,
                    Style("green" if leg.get("side") == "UP" else "red")))
    body.append(pad([(fit("", w, "<"), PAPER)], w))

    hdr = ["TIME", "SIDE", "SHARES", "PRICE", "PROCEEDS"]
    cw = [8, 5, 7, 6, 8]
    while sum(cw) + len(cw) > w and len(hdr) > 2:
        hdr.pop()
        cw.pop()
    rows_data = []
    for e in reversed(list(snap.get("exits") or [])):
        cells = [
            (str(e.get("time", "")), DIM),
            (str(e.get("side", "--")),
             Style("green" if e.get("side") == "UP" else "red", bold=True)),
            (f"{(_finite(e.get('shares')) or 0.0):.2f}", Style("ink")),
            (f"{(_finite(e.get('price')) or 0.0):.3f}", FAINT),
            (f"${(_finite(e.get('proceeds')) or 0.0):.2f}", Style("ink")),
        ]
        rows_data.append(cells[:len(hdr)])
    used = len(body)
    body += table(hdr, cw, rows_data, w, max_rows=max(1, rows - 3 - used))
    return panel("STOP LOSS", body, cols, rows, g, right_note="watch + exits")


def _events(snap, cols: int, rows: int, g: Glyphs, s: Sizing) -> list[Row]:
    w = cols - 2
    lv = {"good": Style("green"), "bad": Style("red", bold=True),
          "warn": Style("amber"), "info": Style("ink")}
    body: list[Row] = []
    n = max(1, rows - 2)
    for e in list(snap["events"])[-n:]:
        ts = time.strftime("%H:%M:%S", time.localtime(e.wall))
        tag = fit(e.tag[:6], 6, "<")
        rep = f" x{e.repeat}" if e.repeat > 1 else ""
        head_w = 9 + 7
        body.append(pad([
            (ts + " ", DIM), (tag + " ", Style("blue", bold=True)),
            (fit(e.text + rep, max(1, w - head_w), "<"), lv.get(e.level, PAPER)),
        ], w))
    return panel("SYSTEM / EVENT FEED", body, cols, rows, g, right_note="stdout captured")


def _footer(snap, cols: int, g: Glyphs, s: Sizing) -> Row:
    up = snap["uptime"]
    left = [
        ("Ctrl+C ", Style("white", "ink", bold=True)), (" quit  ", DIM),
        (" r ", Style("white", "ink", bold=True)), (" repaint  ", DIM),
        (f"up {int(up // 3600):02d}:{int(up % 3600 // 60):02d}:{int(up % 60):02d}  ", DIM),
        (f"frames {snap['frames']}  ", DIM),
    ]
    right = [(" -- = no source in this build; never fabricated ", Style("purple", bold=True))]
    lw = sum(len(t) for t, _ in left)
    rw = sum(len(t) for t, _ in right)
    mid = cols - lw - rw
    if mid < 1:
        return pad(left, cols)
    return pad(left + [(" " * mid, PAPER)] + right, cols)


def _overlay(frame: list[Row], snap, cols: int, rows: int, g: Glyphs) -> None:
    """Transient centred notification, drawn into the frame.

    It is part of the frame, so the diff renderer handles its appearance and
    disappearance like any other change - nothing blocks and nothing is
    redrawn wholesale.
    """
    ov = snap["overlay"]
    if ov is None or not ov.alive(snap["mono"]):
        return
    inten = ov.intensity(snap["mono"])
    hue = {"good": "green", "bad": "red", "info": "blue"}.get(ov.level, "blue")
    if inten >= 0.35:                       # glow phase
        box = Style("white", hue, bold=True)
        edge = Style("white", hue)
    else:                                   # fade phase
        box = Style(hue, "paper2", bold=True)
        edge = Style(hue, "paper2")

    box_w = min(cols - 4, max(30, len(ov.big) + 12, len(ov.sub) + 12))
    x = max(0, (cols - box_w) // 2)
    y = max(0, rows // 2 - 3)
    inner = box_w - 2
    lines = [
        ([(g.tl + g.h * inner + g.tr, edge)]),
        ([(g.v, edge), (fit(ov.big, inner, "^"), box), (g.v, edge)]),
        ([(g.v, edge), (fit(ov.sub, inner, "^"), box), (g.v, edge)]),
        ([(g.bl + g.h * inner + g.br, edge)]),
    ]
    for i, seg in enumerate(lines):
        yy = y + i
        if not (0 <= yy < rows):
            continue
        flat = "".join(t for t, _ in pad(frame[yy], cols))
        frame[yy] = pad([(flat[:x], Style("faint"))] + seg +
                        [(flat[x + box_w:], Style("faint"))], cols)


# ------------------------------------------------------------------- build ---
def build(snap: dict, cols: int, rows: int, g: Glyphs) -> list[Row]:
    cols = max(20, cols)
    rows = max(4, rows)
    s = Sizing(cols, rows)
    frame: list[Row] = [_header(snap, cols, g, s), _health(snap, cols, g, s)]

    # ---- top band ----
    if s.top > 0:
        if s.stack:
            parts = [_kpi(snap, cols, s.top, g, s)]
            widths = [cols]
        elif not s.show_chart:
            w1, w2 = hsplit(cols, [0.50, 0.50], [36, 30])
            parts = [_kpi(snap, w1, s.top, g, s), _round_panel(snap, w2, s.top, g, s)]
            widths = [w1, w2]
        else:
            w1, w2, w3 = hsplit(cols, [0.34, 0.26, 0.40], [38, 30, 30])
            parts = [_kpi(snap, w1, s.top, g, s),
                     _round_panel(snap, w2, s.top, g, s),
                     _chart(snap, w3, s.top, g, s)]
            widths = [w1, w2, w3]
        frame += join(parts, widths, s.top)

    # ---- status strip ----
    frame += _status_strip(snap, cols, s.status_rows, g, s)

    # ---- mid band ----
    if s.show_mid:
        if s.stack:
            frame += _pipeline(snap, cols, s.mid, g, s)
        else:
            w1, w2 = hsplit(cols, [0.52, 0.48], [40, 34])
            left = _pipeline(snap, w1, s.mid, g, s)
            h1 = max(6, s.mid // 2)
            right = _matrix(snap, w2, h1, g, s) + _equity(snap, w2, s.mid - h1, g, s)
            frame += join([left, right], [w1, w2], s.mid)

    # ---- bottom band ----
    if s.show_bot:
        if s.stack:
            frame += _events(snap, cols, s.bot, g, s)
        elif not s.show_dist:
            w1, w2 = hsplit(cols, [0.5, 0.5], [30, 30])
            frame += join([_trades(snap, w1, s.bot, g, s), _events(snap, w2, s.bot, g, s)],
                          [w1, w2], s.bot)
        elif cols >= 120:
            # The stop panel displaces the P&L histogram only while the stop
            # loss is actually in play - a summary you can read after the
            # fact matters less than an unfilled stop you need to see while
            # it is happening, but there is nothing to show when the feature
            # is off and STOP_LOSS_ENABLED=0 is this repo's default.
            stop_active = bool((snap.get("stop_status") or {}).get("enabled"))
            # POSITIONS (w3) carries five numbers per side; the trade and
            # event feeds either side are logs that simply show fewer rows
            # when squeezed, so the width is taken from them.
            w1, w2, w3, w4 = hsplit(cols, [0.20, 0.23, 0.31, 0.26],
                                    [26, 28, 28, 28])
            first = (_stop_panel(snap, w1, s.bot, g, s) if stop_active
                     else _dist(snap, w1, s.bot, g, s))
            frame += join([first,
                           _trades(snap, w2, s.bot, g, s),
                           _holdings_or_bands(snap, w3, s.bot, g, s),
                           _events(snap, w4, s.bot, g, s)],
                          [w1, w2, w3, w4], s.bot)
        else:
            # Narrower than four columns: the exit/stop panel, when active,
            # displaces the band log rather than the trade or event feeds.
            stop_active = bool((snap.get("stop_status") or {}).get("enabled"))
            w1, w2, w3 = hsplit(cols, [0.30, 0.30, 0.40], [28, 28, 30])
            third = (_stop_panel(snap, w2, s.bot, g, s) if stop_active
                     else _holdings_or_bands(snap, w2, s.bot, g, s))
            frame += join([_trades(snap, w1, s.bot, g, s),
                           third,
                           _events(snap, w3, s.bot, g, s)], [w1, w2, w3], s.bot)

    frame.append(_footer(snap, cols, g, s))

    # exact geometry, always
    while len(frame) < rows:
        frame.append(blank(cols))
    frame = [pad(r, cols) for r in frame[:rows]]
    _overlay(frame, snap, cols, rows, g)
    return frame
