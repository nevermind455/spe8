# Gate 1 — backtest against the real book

## The thing almost everyone gets wrong

A backtest that checks an entry against one price per window — the close, the
mid, the best ask — is not a backtest. It is a chart with a story on it.

On Polymarket your fill does not come off a price. It comes off the ladder:
the order takes whatever is sitting at the best ask, then the next level, then
the next, until the stake is spent or the price cap stops it. Three things can
happen, and a price line shows only the first:

1. you fill at roughly the quoted price, because the top level was deep enough;
2. you fill **worse** than quoted, because it was not;
3. you do not fill **at all**, because the whole ladder inside your cap holds
   fewer shares than the venue's minimum order size.

Two of those three lose money, and both are invisible to a single number per
candle. A strategy that looks profitable on closes and is actually taking
outcome 2 or 3 half the time is not a strategy.

So gate 1: **test against recorded book snapshots, not price lines.**

## You have to record it yourself

Nobody sells historical Polymarket book depth. If it is not on your disk it
does not exist. `book_recorder.py` is the collector:

```bash
python3 book_recorder.py record --interval 2
```

Every tick it writes one JSON object per snapshot holding:

* every level, price and size, on **both sides** of **both legs** — not the
  best quote, not a volume total, the ladder;
* the venue's own book timestamp plus how stale the copy was in hand;
* seconds left in the round, so a snapshot can be matched to a band;
* the Chainlink 60s TWAP strike and current value, and Binance spot, so a
  signal rule can be re-derived later instead of being frozen at record time.

Snapshots that `orderbook.py` refused — stale, crossed, empty, future-dated —
are not written. A gap in the tape is honest; a level that was never there is
not. The file is JSONL, so adding a field later appends a key rather than
silently shifting every old row under a changed CSV header.

Resolution is the other half. A real book scored against a guessed outcome is
still a daydream:

```bash
python3 book_recorder.py resolve      # real winner of every recorded round
python3 book_recorder.py stats        # coverage: rounds, levels, gaps
```

Rounds that paid neither 1 nor 0 are recorded as `SPLIT` and excluded, not
rounded into a win.

## The replay

```bash
python3 book_backtest.py --signal chainlink --bands "300:240:0.35:0.45,..."
```

It runs two engines over the *same* signals, on the same ticks:

| | what it does |
|---|---|
| **BOOK** | walks the recorded asks: each parcel at its own price, the taker fee charged per parcel at that price, the stake raised to the venue minimum by the same `orderbook.venue_minimum_stake` the live and paper order paths use, and a **refusal** when the depth inside the cap cannot reach that minimum. |
| **QUOTE** | the daydream, reproduced honestly: the whole stake fills at the best ask, every time, no depth, no misses. This is what a close-price backtest reports. |

Settlement is against the recorded winner, paying exactly $1 a winning share.

The report ends on the number that matters:

```
  VERDICT: the price line overstated PnL by +8.66 on $390.00 staked.
```

plus what produced it — how many signalled entries could not have filled, the
phantom PnL the quote engine booked on them, how many fills walked past the
best ask, and the slippage in basis points against the price you thought you
were getting.

If BOOK is red while QUOTE is green, the edge was never there. That is the
gate doing its job, and it is cheaper to learn here.

## What this does not claim

* **Latency.** A snapshot is the book at the moment it was read. Your order
  arrives later, and the level you were aiming at may be gone. The replay
  fills against the book it saw, so it is still optimistic by one round trip.
* **Impact.** A $2.50 taker does not move this market, so the walk consumes
  depth without repricing it. That assumption fails at size.
* **Queue position.** Every trade here is a taker buy, because the bot crosses
  the spread and the maker rebate is zero. No resting-order fill is modelled,
  because none happens.
* **Coverage.** The tape is only as good as the hours you recorded. `stats`
  prints the span and the per-round snapshot counts; a band with no snapshots
  in its seconds-left range is untestable, not zero.

## Checks

```bash
python3 tests_book_backtest.py
```

Covers the ladder walk, the venue-minimum parity with the live order path,
per-parcel fees, settlement, the unresolved/split skips, one entry per band
per round, the tape surviving a recorder killed mid-write, and the refusal to
record a book the freshness checks rejected.
