# Survival, and what volume actually does

## The claim, split in two

> "We need to survive. If we survive we get profit from volume."

The first sentence is right and is the correct priority. The second is two
different claims wearing one coat, and they point opposite ways.

**Volume as evidence is free, and it is the whole game.** Every recorded round
shrinks the error bar on the edge's *sign*. It costs wall-clock time and
nothing else. This is the volume you want, and you want as much of it as
possible, as early as possible.

**Volume as stake is not free.** It multiplies whatever the per-trade
expectation already is. Positive, and size and time compound it. Negative, and
size and time are the exact mechanism that empties the account. Volume is a
lever on the sign. It cannot supply one.

Conflating the two is how a bot with a small negative edge gets described as
"needing more volume" right up until it is out of money.

## The hurdle is charged per fill

At a 0.39 average fill the taker fee is ~428 bps of notional, so break-even
sits about 1.7 points of win rate above the price paid:

| pay | must win | hurdle | fee drag |
|---|---|---|---|
| 0.30 | 31.47% | 1.47pp | 490 bps |
| 0.40 | 41.68% | 1.68pp | 420 bps |
| 0.50 | 51.75% | 1.75pp | 350 bps |
| 0.60 | 61.68% | 1.68pp | 280 bps |
| 0.70 | 71.47% | 1.47pp | 210 bps |

More trades do not reduce this. They pay it more often. Any "profit from
volume" story has to clear it first, on every fill.

The one genuine volume-revenue mechanism on this venue is maker liquidity
rewards, and this bot cannot earn them: it crosses the spread on every entry
and the maker rebate is zero. Earning those would be a different bot, not a
larger version of this one.

## What the tool does

```bash
python3 survival.py                       # the register's phase-1 standing
python3 survival.py --sweep-bet           # what size survives
python3 survival.py --from-tape book_tape.jsonl   # measured, not assumed
```

It carries the edge as a **posterior, not a point estimate**. A 46.3% win rate
over 123 fills is consistent with a real edge *and* with a coin, and those two
futures have very different ruin probabilities at identical bet sizes. Each
simulated future draws its own true win rate from what the sample actually
supports, so the output includes the futures where the edge was luck. Passing
`--fills 0` turns that off and assumes the rate is exact, which is the
optimistic case, not the honest one.

Ruin is **absorbing**: an account that hits its stop-out stops trading.
Modelling it as a dip you trade back out of would flatter every number.

## The three cases that matter

All on $1000, over one month (8,640 rounds), ruin = down 50%.

**Phase 1 as recorded — a +5.84pp edge, if it is real:**

| bet | % bank | ruin | lose $ | median | p05 |
|---|---|---|---|---|---|
| $0.50 | 0.05% | 1.1% | 10% | $1,650 | $834 |
| $2.50 | 0.25% | 6.2% | 8% | $4,312 | $500 |
| $10.00 | 1.00% | 12.4% | 13% | $14,275 | $495 |

**Exactly zero edge — a coin at the break-even rate:**

| bet | ruin | lose $ | median |
|---|---|---|---|
| $0.50 | 0.0% | 51% | $999 |
| $2.50 | 9.7% | 50% | $1,000 |
| $10.00 | 66.4% | 69% | $498 |
| $20.00 | 82.2% | 83% | $493 |

The median barely moves. Ruin goes from 0% to 82% on size alone. That is
volume-as-stake with no edge: it does not lose slowly, it loses by variance.

**Phase 2 as recorded — a real −4.31pp edge:**

| bet | ruin | lose $ | median |
|---|---|---|---|
| $0.50 | 20.1% | 87% | $718 |
| $2.50 | 76.1% | 85% | $499 |
| $10.00 | 85.0% | 86% | $497 |

Read the first row twice. At **0.05% of bankroll per trade** — about as small
as a bet can be — a −4.31pp edge still stops the account out one month in
five, and loses money 87% of the time. There is no size small enough to
out-survive a negative edge, because the hurdle is per fill and the fills keep
coming. Volume is what kills it.

## So what survival actually means here

1. **Get the sign before you get the size.** Phase 1 needs ~291 fills to
   reach |z| = 2 on its own effect size. That is days of recording, not
   months. It is the cheapest thing on this list.
2. **Keep the stake small while the sign is unknown** — not because small
   stakes rescue a bad edge (they do not, see above) but because they buy
   time to find out, and time is the only input that helps.
3. **Kill negative-EV phases outright.** Phase 2 is not a sizing problem or a
   tuning problem. `PHASE2_ENABLED=0` is already the shipped default; the
   numbers above are why that should stay true.
4. **Full Kelly is not the target.** At phase 1's measured edge full Kelly is
   12.3% of bankroll — $123 a trade on $1000. On an edge with z = +1.72 that
   is a way to be right about direction and still lose everything to
   variance. Quarter Kelly at most, and only after the sign is established.

## Checks

```bash
python3 tests_book_backtest.py
```

Covers the trade arithmetic (the fee is charged on losses too), EV sign
tracking the break-even hurdle, ruin being absorbing, size amplifying the sign
in both directions, and a thin sample producing more ruin than a thick one at
the same observed win rate.
