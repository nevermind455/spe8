# Live public feeds and order-book state

```text
Binance BTC trade WS ─┐
Chainlink RTDS 60s TWAP ──> atomic round snapshot ──> unchanged strategy
Polymarket market WS ─┘                 │
public REST recovery/rules ─────────────┘

live only: authenticated user WS + market-filtered REST reconciliation
```

Run paper mode with real public feeds:

```bash
python run_feeds.py --paper --dash
```

`BOOK_SOURCE=ws_shadow` is the default: the audited REST snapshot remains the
strategy input while the maintained WebSocket book is compared against it.
`BOOK_SOURCE=ws` deliberately changes the book input and uses REST only for
startup/recovery/fallback; its one-per-round audit sample never enters a
decision. `PRICE_STALE_POLICY` affects only the legacy display value. The
decision path always calls `fresh_snapshot()`.

All feed supervisors use bounded reconnect backoff, receive timeouts,
application-level heartbeats where required, liveness state, stale/future
timestamp rejection, and redacted errors. A market-WS disconnect immediately
marks every local book UNSYNCED. Deltas are ignored until a fresh WebSocket or
REST snapshot arrives. Token generation and explicit unsubscribe prevent a
late previous-round update from entering the new round.

Round state and its public/private subscription intent commit under one
transition lock. Concurrent boundary clearing, discovery, and prewarming
cannot leave a newer round paired with an older subscription. Repeating the
same logical transition also repairs downstream book/subscription drift
without resetting a healthy generation.

The user channel waits for explicit bot condition filters and removes old
filters during rotation. A quiet channel is judged by heartbeat, not trade
traffic. Fill lifecycle records are deduplicated by trade ID; cumulative
`size_matched` is never summed; contradictory terminal states are quarantined.
REST reconciliation also requires explicit bot market filters. A forced
startup pass synchronously drains authorized fills into the durable ledger
before the strategy starts. Further passes run after every successfully sent
user-subscription generation, while the socket is unhealthy, and every 120
seconds while healthy. Restart filters include durable authorizations from the
last two hours; replayed rows must match the persisted lot identity exactly.

Live execution requires the authenticated user WebSocket. Both strategy phases
check that the current condition was actually sent on the active session and
that an application-level PONG is fresh, then check again immediately before
submission. REST is defense-in-depth, not the sole live fill channel.

`BOOK_SOURCE=ws_shadow`, `PRICE_STALE_POLICY=none`, `BTC_FEED=ws`,
`USER_WS=on`, and `RECONCILE=auto` are the shipped settings. Paper and health
mode force private user/reconciliation feeds off before tasks are created.

Current deterministic result: `tests_feeds.py` — 255 passed, 0 failed
(re-measured 2026-09-12),
including localhost WebSocket disconnect/reconnect, heartbeat, stale-data,
book atomicity, rotation, fill lifecycle, reconciliation, process-lock, and
strategy-hash regression checks.
