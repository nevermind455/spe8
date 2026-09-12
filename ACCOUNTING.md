# Accounting, settlement, and PnL

Accounting is fill-driven. A submitted or accepted order is not inventory.
Live positions enter the ledger only when the user WebSocket or filtered REST
reconciliation returns a bot-authorized `CONFIRMED` trade. `MATCHED`, `MINED`,
`RETRYING`, `FAILED`, malformed, wrong-market, wrong-token, overfilled, SELL,
and unrelated-wallet records do not enter PnL.

For each BUY lot:

```text
notional = shares × actual venue fill price
fee      = shares × fee_rate × (price × (1-price))^fee_exponent
cost     = notional + fee
```

The live `fd.r`/`fd.e` market parameters take precedence. The current crypto
schedule is 0.07 with five-decimal fee rounding, but the runtime does not
silently assume a zero fee when public venue parameters are missing.

Position values are:

```text
average entry = total filled notional / total shares
unrealized PnL = shares × live bid - cost
realized PnL   = shares × official payout - cost
paper cash     = starting cash - all fill costs + settled payouts
```

An unmarkable open position produces unknown equity rather than an invented
zero mark. Winning shares pay 1, losing shares pay 0, and an explicitly
resolved 50/50 outcome pays 0.5 per share.

Settlement independently checks the public CLOB winner/payout surface and the
exact Gamma event/condition. Both must be explicitly final and agree. Closing
the book, a near-1.0 price, BTC movement, or a lone 0.5/0.5 price is not enough.
Settled positions cannot settle twice; a late fill reopens the position for a
fresh official settlement.

The ledger is atomically persisted with `fsync`, rejects NaN/Infinity,
negative/corrupt aggregates, inconsistent lot totals, invalid settlement
values, a malformed durable identity, and a dedup set that does not exactly
match its lots. The paper account and ledger carry the same random identity, so
moving the pair is safe while a missing or replacement ledger fails closed.
Live accepted order authorization is persisted before further trading so
exposure and the accepted outcome-token leg survive a restart. Paper restores
confirmed token inventory by condition. Both prevent a restart from reopening
the complementary-outcome path. A durable-journal failure stops all future
live submissions.

Live restart reconciliation uses explicit conditions retained from durable
authorizations for a two-hour recovery horizon. It runs synchronously at
startup and periodically thereafter, rejects unrelated wallet activity, and
alarms if a REST replay contradicts a persisted fill instead of hiding it as a
duplicate. The bounded horizon is not a historical wallet scan; outages longer
than two hours remain a live-readiness limitation.

`reconcile_balance()` compares confirmed BUY costs with actual pUSD movement.
Deposits, redemptions, manual activity, or missing fills correctly appear as a
mismatch; this bot does not auto-redeem resolved outcome tokens.

Current deterministic result: `tests_accounting.py` — 175 passed, 0 failed
(re-measured 2026-09-12).
