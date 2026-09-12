# Strategy register

What is live, what is on the shelf, and what has been ruled out — with the
numbers that decided each. Every variant here is switchable through `.env`;
none of them require a code change.

Last updated 2026-08-25. Profile inventory re-checked against `config.py`
2026-09-12.

**Nothing on this page is the shipped default.** Every profile below is an
opt-in `.env` override. With no `.env` the bot runs phase 1 only, on the
bands in `config.py` (and mirrored in `.env.example`):

```
PHASE1_ENABLED=1
PHASE1_BANDS=300:240:0.35:0.45,240:180:0.30:0.40,180:120:0.40:0.50,120:60:0.55:0.75:8
PHASE1_INTERVAL_SECONDS=12
PHASE2_ENABLED=0            SIGNAL_DECISION_RULE=price
BET_SIZE=2.50               MAX_BUY_PRICE=0.90    MIN_BUY_PRICE=0.20
TRADE_LAST_SECONDS=120      TRADE_INTERVAL_SECONDS=6
MIN_SECONDS_TO_EXPIRY=60    MAX_ROUND_EXPOSURE=(derived, $72.225)
```

To run any profile below, put its lines in `.env` and restart. To revert,
delete them.

---

## Previous two-phase band profile (parked 2026-08-25)

Note the bands here are the PARKED experiment's, not the shipped ones above.


```
PHASE1_BANDS=300:270:0.40:0.60:15,270:240:0.30:0.50:15,240:210:0.50:0.60:15,210:180:0.40:0.50:15,180:150:0.55:0.65:15,150:120:0.50:0.60:15,120:60:0.40:0.50
PHASE1_ENABLED=1     PHASE1_INTERVAL_SECONDS=12
PHASE2_ENABLED=1     TRADE_LAST_SECONDS=300   TRADE_INTERVAL_SECONDS=6
MIN_SECONDS_TO_EXPIRY=60
BET_SIZE=2.50        MAX_BUY_PRICE=0.90       MIN_BUY_PRICE=0.20
MAX_ROUND_EXPOSURE=98.44   (explicit combined paper cap)
```

Both phases run from T-300 through T-60. Phase 1 requires its selected
contract to be inside the active price band; Phase 2 retains book and
Chainlink diagnostics. In both phases, fresh Binance `SIG PRICE` is the sole
authority for the order side and is rechecked at executor commit. The final
minute is closed to both.

Standing at 230 settled fills:

| | fills | avg px | won | net PnL | /$100 | z |
|---|---|---|---|---|---|---|
| phase 1 | 123 | 0.388 | 46.3% | +$42.19 | +13.2 | +1.72 |
| phase 2 | 107 | 0.721 | 69.2% | −$26.73 | −6.7 | −0.72 |
| total | 230 | 0.543 | 57.0% | +$15.46 | +2.1 | +0.89 |

Neither phase has cleared |z| > 2. The pre-committed decision point is 1,000
phase-1 fills.

---

## PAPER signal-follow mode (opt-in; NOT the default)

```
PHASE1_ENABLED=0
PHASE2_ENABLED=1
PAPER_ALLOW_SIGNAL_FLIPS=1
PAPER_LEDGER_PATH=signal_flip_v1_ledger.json
PAPER_ACCOUNT_PATH=signal_flip_v1_account.json
PAPER_AUDIT_PATH=signal_flip_v1_orders.jsonl
PAPER_TRADE_LOG_PATH=signal_flip_v1_fills.csv
BOT_TRADE_LOG_PATH=signal_flip_v1_decisions.csv
```

Selecting this profile requires all of the lines above in `.env`: the
switch fails configuration loading unless phase 1 is parked and phase 2 is
enabled. It is a PAPER-only, no-band experiment. Phase 2 continues to enforce the
global price floor/ceiling, FOK depth, spread, round exposure, readiness, and
all initial/final/commit-time `SIG PRICE` checks. Same-side entries retain the
normal cadence while only one outcome is held. Buying the other outcome needs
a non-neutral UP/DOWN transition observed after the most recently accepted
side. Once both outcomes are held, every further entry needs another signal
epoch; a repeated cached direction cannot keep adding exposure. A neutral or
stale sample revokes any transition that has not yet produced an accepted
entry, so it cannot be reused after an unknown-price gap.

The switch fails configuration loading unless Phase 1 is parked and Phase 2
is enabled, preventing two overlapping entry cadences. LIVE always retains
the complement-leg block even if the variable is present. On restart, one
durable held token establishes a conservative baseline at the current epoch,
so a later transition is still required; two durable tokens are ambiguous and
fail closed.

`BOT_TRADE_LOG_PATH` is optional. Relative paths resolve beside `main_bot.py`;
without it the journal remains exactly `trade_log.csv`. This mode is
directional signal following, not simultaneous/equal-share pair arbitrage.

---

## Reserve A — raised bands (tested, parked 2026-08-17)

```
PHASE1_BANDS=300:240:0.40:0.50,240:180:0.35:0.50,180:120:0.50:0.60
```

Moves every band up, and flips the closing window from buying the underdog to
buying the favourite. **Tested against both datasets; parked because it came
out behind the live config on each, at every spread assumption.**

Head to head at an assumed 1c spread:

| dataset | variant | trades | won | implied | /$100 | z |
|---|---|---|---|---|---|---|
| live (235 fills) | reserve A | 84 | 51.2% | 43.6% | +15.8 | +1.41 |
| live | **live config** | 112 | 48.2% | 39.8% | **+16.9** | **+1.82** |
| archive (688 fills) | reserve A | 234 | 46.6% | 46.6% | −1.3 | +0.01 |
| archive | **live config** | 164 | 49.4% | 41.1% | **+14.6** | **+2.16** |

Per window, at 1c spread:

| window | band | live /$100 | live z | archive /$100 | archive z |
|---|---|---|---|---|---|
| T-300..240 | 0.40–0.50 | +32.9 | +2.20 | −24.2 | −1.28 |
| T-240..180 | 0.35–0.50 | +2.6 | +0.20 | +29.6 | +3.27 |
| T-180..120 | 0.50–0.60 | **−26.1** | −0.94 | **−38.4** | **−3.19** |

**The closing band is what sinks it.** 0.50–0.60 at T-180..120 is negative on
both datasets, and on the larger one it is z = −3.19: 65 observations winning
35.4% against a 55.0% implied price. That band buys the favourite in the
window nearest the endgame, which is the same structure that cost $31.51 in
the final minute — as a round resolves, the likely side is the one the market
is most willing to sell you.

Worth keeping because the **opening** window (0.40–0.50 at T-300..240) was the
best single cell on live data, +32.9 per $100 at z = +2.20. The two datasets
disagree on its sign, so it is a candidate, not a finding.

### Reserve A′ — opening band only (untested)

```
PHASE1_BANDS=300:240:0.40:0.50,240:180:0.30:0.40,180:120:0.40:0.50
```

Reserve A's one promising change, without the closing band that sinks it.
Not yet backtested.

---

## Ruled out

| variant | why |
|---|---|
| `0.35–0.40`, T-300..T-240 only | Band reachable in 13 of 57 rounds. Fires roughly once every 4 rounds. |
| Three time-varying bands, first attempt | Windows measured +6.2 / +7.3 / +6.2 per $100 — one number three times. Six parameters fitting 57 rounds. |
| Flat `0.25–0.50` | Its advantage came almost entirely from the 0.45–0.50 slice, the most bias-contaminated cell in the dataset. |
| Pair arbitrage | UP ask + DOWN ask sums to ~1.010; needs below ~0.962 net of fees. No free money. |
| Chainlink as a leading signal | The settlement TWAP lags spot; it was the *worst* predictor even after the stream fix. |
| Signal trading, T-240..T-120 | −20.5 per $100 across 354 fills. Removed from the live config. |
| Signal trading, final 60s | 31.2% won against a 69.6% break-even, z = −3.29. Closed via `MIN_SECONDS_TO_EXPIRY=60`. |

---

## How the backtest works, and what it cannot tell you

Both legs of a binary market are complements: a fill at price `p` means the
other side was buyable at about `1-p` at that instant, and it wins exactly
when the recorded side loses. So every recorded fill is also an observation of
the opposite bet, which is what makes these variants testable without trading
them.

The limit is the sampling. It only observes moments the bot **chose to act**,
so it is evidence about those moments, not about all moments. The archive is
worse in this respect than the live ledger: its fills were signal-selected, so
its complements inherit the mirror image of that signal's error. That is why
a variant scoring well on the archive alone is not evidence.

`signal_journal.py` exists to remove this limitation — it records every round
at every sample regardless of trading, so future variants can be tested on an
unbiased sample instead of on the shadow of past decisions.

To activate any variant, put its `PHASE1_BANDS` line in `.env` and restart.
To revert, delete the line.
