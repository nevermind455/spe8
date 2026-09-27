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

## Where "profit from volume" is literally true

There is one place on this venue where volume genuinely pays, independent of
predicting anything - and it is the maker side, which this build never touches.
Two separate flows, both configured **per market**:

* **Maker rebates.** A share of the taker fees collected in a market, paid
  back to makers whose limit orders filled, pro-rata by the fee-equivalent
  volume their liquidity generated. Reported at 20% of collected taker fees in
  crypto after the July 2026 fee update (25% in most categories, 15% sports),
  paid daily, $1 minimum.
* **Liquidity rewards.** A daily pool for *resting* limit orders inside a
  configured max spread from the adjusted midpoint, scored by size and
  distance from best, subject to a minimum qualifying size. Only markets in
  the venue's sampling set carry a pool.

Both numbers above are from secondary sources, not a first-hand read - the
container this was written in cannot reach `polymarket.com`. **Do not size
anything on them.** `rewards_check.py` reads the real config for the actual
BTC 5-minute market and prints it; run that on a box with network access
before treating any of this as a number.

### What the maker side is worth, before rebates

Makers pay no trading fee. That alone removes the entire per-fill hurdle:

| price | taker must win | maker must win | hurdle removed |
|---|---|---|---|
| 0.35 | 36.59% | 35.00% | 1.59pp |
| 0.40 | 41.68% | 40.00% | 1.68pp |
| 0.50 | 51.75% | 50.00% | 1.75pp |
| 0.60 | 61.68% | 60.00% | 1.68pp |

Against phase 1's measured +5.84pp edge, the fee hurdle is 1.66pp — **28% of
the whole edge is fee drag**. Removing it takes EV from +15.1% to +19.3% per
dollar staked, with no improvement in prediction whatsoever. Rebates would sit
on top of that. This is a bigger lever than any band retune on the table.

### Why it is not free money

A maker does not choose when to trade. A maker is filled *because* someone
else decided the price was wrong, and in a 5-minute BTC market that someone is
usually acting on a spot move the resting order has not repriced for. That is
adverse selection, and the arithmetic is unforgiving:

**1.68pp of win rate lost to adverse selection cancels the entire fee saving.**

| true win rate | maker EV per $1 |
|---|---|
| 46.30% (no adverse selection) | +0.193 |
| 44.62% (−1.68pp) | +0.150 — same as taking |
| 43.30% (−3.00pp) | +0.116 |
| 41.30% (−5.00pp) | +0.064 |

1.68pp is a small number to lose to informed flow. It is entirely plausible
that making is worse here than taking, and equally plausible that it is much
better. Which of those is true is measurable — the book tape already records
both sides of the ladder every tick, so a resting order's fill can be replayed
against it — and it is not currently measured.

### $30,000 a day, priced out

```bash
python3 maker_rebate.py --volume 30000
```

The fee on a fixed notional is `N * theta * (1-p)`, so it is a flat percentage
of volume that shrinks as price rises. A rebate returning 20% of it pays:

| avg price | taker PAYS/day | maker EARNS/day | rebate rate |
|---|---|---|---|
| 0.30 | −$1,470 | +$294 | 98 bps |
| 0.40 | −$1,260 | +$252 | 84 bps |
| 0.50 | −$1,050 | +$210 | 70 bps |
| 0.60 | −$840 | +$168 | 56 bps |

At 0.40 that is **$252/day, about $7,560/month** — and the *same* volume as a
taker is **−$1,260/day, −$37,800/month**. A $1,512/day swing decided purely by
which side of the book you are on. The pro-rata allocation cancels out (your
slice of the pool is proportional to the fees your own fills generated), so
competition does not cut the *rate* — it only limits how much you get filled.

Three things that number is not:

1. **It is gross revenue on volume, not profit.** The break-even is an
   identity: **the adverse-selection loss that cancels the rebate is exactly
   the rebate rate** — 84 bps of notional at 0.40, or $252/day on $30k. Lose
   more than that on the positions and the volume costs you money at any size.
2. **$30k/day in one market needs $104 of filled volume every 5-minute round**
   — 41 fills a round at $2.50. Your maker volume cannot exceed the taker flow
   that actually arrives, so the market's own 24h volume is a hard ceiling.
   `rewards_check.py` prints it.
3. **Liquidity rewards are a different animal** and do not scale with volume:
   a fixed daily pool per market, split by score on resting size and distance
   from midpoint, diluted by every other maker. A $100/day pool at a 15% score
   share is $15/day no matter how much you turn over. Do not build a $30k/day
   plan on a $50/day pool.

So: rebates are a real answer to "profit from volume", and they are the only
one on this venue. They are also a different bot, and the hurdle is 84 bps of
adverse selection. Do not switch the live config to chase them on the strength
of a table in a markdown file.

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
