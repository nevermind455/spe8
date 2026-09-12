# Production-grade audit report

Audit date: 2026-08-12

Follow-up audit: 2026-08-25. Round transitions are now serialized through
their downstream subscriptions, paper account/ledger identity is relocation-
safe and fails closed on an orphan, restart fill reconciliation is durable and
subscription-generation aware, held outcome tokens are restored in both
strategy phases, and live submission is gated on a sent current-market user
subscription plus fresh application PONG. Current focused results are 151
accounting, 243 feed, 176 strategy/fix, 113 paper, 40 bugfix, and 48,929
dashboard checks, all passing.

Scope: every shipped source, runner, feed, dashboard, accounting module, test,
configuration example, and operating document. The audited path was:

```text
market discovery → round initialization → BTC/Chainlink signal → order book
→ strategy decision → order preflight/sign/post or paper fill → fill ingestion
→ position accounting → independent resolution → settlement/PnL → next round
```

`strategy.py` has one narrow post-audit guard: an exactly unchanged finite
opening/current price now abstains instead of casting a false UP vote. Its
SHA-256 **at the 2026-08-12 audit** was
`95d46436999c5d5cdc24742b0fa4f40842017fe5aa89dcd691f72e4d76b81d91`.
All non-equality decisions, final consensus, and repeated-entry behavior remain
intact.

Noted 2026-09-12: that digest is historical. `strategy.py` has since gained
the `SIG_PRICE_MIN_MOVE_BPS` momentum gate, and its current approved digest
is `069e61b18709a6f56de1b54582ffd803fb695590341fd53e1c3dd670a2df1878`. The
authoritative value is the `BASELINE_SHA` table in `tests_feeds.py` and
`tests_dashboard.py`, which is enforced on every run; this prose is not.
Do not treat the digest above as a current check.

## Findings and exact fixes

Locations below point to the repaired code in this build.

| ID | Severity | File, function, exact location | What was wrong | Why it can cause real trading errors | Exact fix applied |
|---|---|---|---|---|---|
| C1 | Critical | `market_discovery.py:get_btc_5m_tokens`, lines 130-145 | Discovery could fall back to an expired previous round or accept a loosely matched event. | A valid signal could buy an expired market whose winner was already known. | Require the exact canonical `btc-updown-5m-{window}` slug, allow only current/one-round-ahead windows, and remove every previous-round fallback. |
| C2 | Critical | `market_discovery.py:_parse_event`, lines 83-127; `polymarket_trade.py:_validate_market_mapping`, lines 323-375 | Token arrays were treated as positional, so token zero could be assumed UP. | Reversed outcome order turns every UP order into DOWN and inverts settlement. | Map IDs by normalized outcome label, require exactly UP and DOWN, unique positive token IDs, valid condition/market IDs, then cross-check Gamma against the public CLOB mapping before every live order. |
| C3 | Critical | `chainlink.py:get_chainlink_btc_price`, lines 10-12; `chainlink_strike.py`, lines 24-40 and 216-289 | The ordinary Chainlink spot feed was used as the market oracle. | BTC five-minute markets use the Chainlink-computed 60-second TWAP; spot versus TWAP can flip close signals. | Disable the legacy spot helper and consume only RTDS `crypto_prices_twap_sixty` / `prices.crypto.chainlink.twap`, symbol `btc/usd`, window 60. |
| C4 | Critical | `chainlink_strike.py:ChainlinkStrike._session/_handle`, lines 173-289 | A mid-round reconnect could label the first available TWAP as the opening strike. | It invents a different price-to-beat and produces false UP/DOWN signals. | Track the connection window, capture a strike only from a payload timestamp exactly on the next five-minute boundary, and fail closed because RTDS has no replay/history. |
| C5 | Critical | `polymarket_trade.py:_accepted_order_response`, lines 235-266 | Empty, malformed, error-bearing, or merely truthy responses could be recorded as fills/success. | Rejected/unfilled orders become phantom positions and false PnL. | Require an object, explicit success, `matched` status, order ID, well-formed trade/hash arrays, and at least one trade ID or transaction hash. Accepted `delayed` responses remain pending, never fills. |
| C6 | Critical | `polymarket_trade.py:_place_trade`, lines 542-624 | Retrying a timeout/ambiguous POST could reuse an order/signature or duplicate a purchase that actually reached the matcher. | A network timeout can become two real fills. | Retry only explicit FOK no-fill results, rebuild and freshly sign every retry, never retry ambiguous transport/results, and block the condition through round end. |
| C7 | Critical | `accounting/ledger.py:COUNTED_STATUSES/record_fill`, lines 32-35 and 144-205 | Provisional MATCHED/MINED/RETRYING lifecycle events were treated as inventory. | They can later fail, leaving shares and PnL that never existed. | Count only terminal `CONFIRMED` BUY trades; retain pending/failed lifecycle telemetry without booking it. |
| C8 | Critical | `paper_trade.py:install_paper_execution`, lines 671-687; `polymarket_trade.py:disable_live_execution`, lines 63-68 | The former “paper” path could share live execution functions/client state. | A paper decision could authenticate, cancel, sign, or post a real order. | Add an irreversible process-local live-client firewall, replace all execution/balance/cancel bindings with `PaperBroker`, force private feeds off, and require the firewall before the bot loop starts. |
| C9 | Critical | `accounting/resolution.py:fetch/parse_market/parse_clob_market`, lines 73-242 | Settlement could be inferred from a closed book, market price, or one API surface. | A delayed/disputed/incorrect outcome pays the wrong side and permanently corrupts PnL. | Require explicit final CLOB winner flags/payouts and explicit Gamma oracle resolution for the exact slug/condition, then require identical payouts. |
| C10 | Critical | `polymarket_trade.py:_journal_receipt/_place_trade`, lines 436-445 and 585-649 | A matched/accepted order could continue trading when its authorization journal failed. | Later fills cannot be safely attributed; a caller retry could duplicate an already accepted action. | Return “submitted” for the already-posted action, mark a CRITICAL process-wide journal fault, block every future live submission, block the round, and stop the main bot. |
| C11 | Critical | `accounting/ledger.py:authorize_order/_fill_matches_authorization`, lines 108-121 and 255-277 | Account-wide user/REST trades could enter this bot’s ledger using only an order ID or incomplete legacy metadata. | Manual or another bot’s trade contaminates inventory, exposure, win rate, and PnL. | Persist condition, token, side, requested notional, round end, and fee metadata; reject missing legacy metadata, wrong market/token/side, and cumulative fills above authorized notional. |
| H1 | High | `market_discovery.py:_tradeable/_valid_window`, lines 64-80 | Discovery did not strictly validate active/closed/order-book/order-acceptance flags and exact timestamps. | It can select a disabled, pre-open, or incorrectly timed market. | Require all eligibility flags plus start = window and end = window+300 within one second. |
| H2 | High | `run_feeds.py:_rotation_loop`, lines 499-543; `feeds/hub.py:set_round/prepare_round`, lines 121-185 | Round rotation could leave the prior condition/book active or prepare the next book too late. | Signals can read stale round state or the first trade can use an unsynced book. | Clear current-round surfaces immediately at every boundary, discover current tokens independently, pre-subscribe the next round in the final 30 seconds, and promote it only after fresh discovery agrees. |
| H3 | High | `main_bot.py:run_bot`, lines 170-204 | Opening prices could carry across rounds or be overwritten/created from a late print. | The next market compares against the wrong reference. | Reset both reference prices on every exact Unix window change; accept the Binance auxiliary reference only from a print timestamped in the first five seconds; obtain the official strike only from the boundary TWAP. |
| H4 | High | `main_bot.py:run_bot`, lines 221-245 and 310-334 | Market/book/clock I/O could cross a 300-second boundary after the signal was formed. | An otherwise valid decision can submit into the next or expired market. | Use one sampled wall time for round identity, validate discovered start/end, then resample and revalidate immediately before cancellation and submission. |
| H5 | High | `price_ws.py:publish_price/fresh_snapshot`, lines 15-49; `feeds/binance.py`, lines 35-167 | Old, future, out-of-order, disconnected, or duplicate BTC prints could remain actionable. | Stale momentum produces a real but invalid order. | Publish atomically, reject timestamp regression, require both monotonic-receipt and exchange timestamps to be fresh, withhold future/stale data, and reconnect on silence. |
| H6 | High | `chainlink_strike.py:current_value/_run`, lines 80-99 and 152-170 | TWAP freshness checked only receipt time and reconnect backoff did not actually grow. | Replayed/old values can drive signals; an outage can cause a hot reconnect loop. | Check receipt and Chainlink observation age (including future skew), withhold on disconnect, and implement effective 0.25→0.5→1→2→4→8 second backoff. |
| H7 | High | `feeds/poly_market.py:_session/_ping_loop`, lines 78-171; `feeds/book.py:desync_all`, lines 139-147 | A disconnected market socket could keep a locally gapped book marked live; protocol pings were incomplete. | Missed deltas make price, spread, depth, and strategy book-side wrong. | Send application-level PING, enforce PONG/receive timeouts, mark all books UNSYNCED on connect/disconnect, and require a new snapshot/REST resync before serving them. |
| H8 | High | `feeds/book.py:apply_snapshot/apply_price_changes`, lines 149-295 | Malformed levels, stale/future timestamps, crossed states, inactive tokens, and multi-row partial application were possible. | Readers can observe impossible/transient books or old-round deltas. | Validate every level/timestamp, stage multi-level events atomically, reject crossed books, enforce generation/active token identity, and drop deltas until synchronized. |
| H9 | High | `orderbook.py:parse_orderbook/validate_buy_liquidity`, lines 41-116 | REST books were trusted without asset, timestamp, finite range, ordering, spread, or complete FOK depth validation. | Orders can target the wrong token, be rejected, cross an extreme spread, or fill beyond the intended cap. | Validate asset ID/exchange age, aggregate duplicate levels, sort best-first, reject empty/crossed books, require both sides, bounded spread, and full executable notional at/below the rounded cap. |
| H10 | High | `polymarket_trade.py:_validate_market_mapping/_round_limit/_quote_fok`, lines 323-415 | Tick, minimum size, neg-risk, fee curve, cap rounding, and depth were guessed or stale. | The CLOB rejects the order or actual cost/shares differ from accounting. | Fetch current public CLOB rules per submission, cross-check optional overrides, floor the cap to the exact venue tick, walk depth best-to-worst, enforce minimum shares, and use live fee rate/exponent. |
| H11 | High | `polymarket_trade.py:_balance_from_response/get_balance_allowance`, lines 148-188 | Six-decimal pUSD base units and multiple allowance entries were interpreted incorrectly. | The bot can overstate available funds/allowance and submit guaranteed rejects. | Convert by 1e6, reject malformed/negative values, use the conservative minimum allowance, serialize reads with execution, and include estimated fee in the funds check. |
| H12 | High | `polymarket_trade.py:place_trade`, lines 447-461 | Parallel callers could submit the same signal concurrently. | Dashboard/task races can create duplicate real orders. | Add a non-blocking process execution lock around the entire live placement path. |
| H13 | High | `polymarket_trade.py:_validate_round_end`, lines 417-428 | Timing was validated only before slow preflight/signing. | A signed order can be posted at/after expiry. | Validate the exact aligned window during preflight, before every signature, and again after signing/before POST. |
| H14 | High | `config.py`, lines 25-29 and 78-79; `main_bot.py:run_bot`, lines 248-301 | Repeated entries had no durable per-round exposure ceiling. | A cadence/race/restart bug can drain the wallet while still “following” the strategy. | Preserve repeated entries but cap requested round notional; restore accepted live exposure or confirmed paper exposure from the persistent ledger after restart. |
| H15 | High | `config.py`, lines 29-39 and 95-99; `run_feeds.py:_run_inner finally`, lines 401-410 | Cancel-before-trade and shutdown called account-wide cancel-all by default. | It can cancel unrelated manual/resting orders owned by the same API credentials. | Disable it by default; require both `CANCEL_OPEN_BEFORE_TRADE=1` and `ALLOW_GLOBAL_CANCEL_ALL=1`, explicitly limiting opt-in to a dedicated bot wallet; shutdown obeys the same gate. |
| H16 | High | `config.py`, lines 75-105; `polymarket_trade.py:_get_client`, lines 108-145 | Invalid wallet type/funder combinations, custom credential hosts, or a currently broken type-3 flow could fail unpredictably or leak authenticated traffic. | Orders can be rejected as wrong signer or credentials can be sent to an untrusted endpoint. | Validate signature/funder pairs, block POLY_1271/type 3 for the pinned SDK while upstream issue #70 is open, validate the funder/key shape, and lock authenticated requests to the official host unless explicitly overridden. |
| H17 | High | `feeds/poly_user.py:FillStore.record_trade`, lines 98-139 | Lifecycle replay, cumulative sizes, and contradictory terminal copies were not safely normalized. | Fills can be double-counted, regressed, or left confirmed after a conflicting failure. | Deduplicate by trade ID, enrich missing identity fields, take maximum cumulative size, preserve forward lifecycle, and quarantine conflicting terminal states as `CONFLICT`. |
| H18 | High | `run_feeds.py:fetch_trades`, lines 195-244; `feeds/reconcile.py:run_once`, lines 89-124 | REST reconciliation could query the whole account or invent CONFIRMED when status was absent. | Manual trades become bot positions; nonfinal rows become fills. | Require explicit recent bot condition filters, merge by trade ID, carry only the venue’s actual lifecycle status, and share the user-feed dedup store. |
| H19 | High | `accounting/ledger.py:save/load`, lines 451-585 | Restart lost dedup/exposure state; partial/corrupt JSON or NaN could bypass cash/risk calculations. | Historical trades can be rebooked, balances become NaN, or exposure resets. | Atomic temp+fsync+replace writes; strict schema, finite/range/aggregate/lot/settlement validation; exact seen-set equality; refuse startup when an existing ledger is invalid. |
| H20 | High | `accounting/fees.py:taker_fee`, lines 51-92; `accounting/ledger.py:record_fill`, lines 144-205 | Fees were stale, omitted, or calculated from the limit rather than actual fills. | Cost basis, bankroll, break-even rate, and PnL are overstated. | Use actual fill shares/price, public live market fee curve, five-decimal rounding, current category fallbacks, and never treat a missing/zero legacy field as authoritative zero. |
| H21 | High | `paper_trade.py:parse_book/parse_market_rules/estimate_fok`, lines 103-319 | Paper mode used synthetic fills without exact book, rules, fee, spread, size, or cap behavior. | “Paper PnL” looks profitable even when live FOKs would reject or slip. | Re-read live public rules and the selected-token book per submission; validate all levels, mapping, timestamp, tick/minimum, spread, depth, and fee curve; full-fill or reject exactly like FOK. |
| H22 | High | `paper_trade.py:PaperBroker._place_trade`, lines 484-610 | Paper latency, matching delay, round cutoff, cash, and persistence were not modeled/fail-closed. | Simulated fills occur after expiry or with money that does not exist. | Apply configured plus venue-reported delay, re-fetch the book after delay, recheck cutoff, require cash including fees, persist fill/audit atomically, and make persistence failure fatal. |
| H23 | High | `paper_trade.py:_load_or_create_account/cash_balance`, lines 379-441 | Restart could silently reset starting cash while retaining fills. | Bankroll and cumulative PnL can be arbitrarily rewritten. | Persist account identity separately, reuse it on restart, and refuse a ledger with fills when its account file is missing. |
| H24 | High | `accounting/ledger.py:settle`, lines 280-310 | Settlement could apply twice or remain volatile after a failed save. | Payout is double-counted or disappears on restart. | Skip settled positions; atomically persist settlement; roll back in-memory settlement and retry if durability fails; reopen on a genuinely late fill. |
| H25 | High | `timer.py:seconds_left/window_start/check_clock`, lines 20-89 | Local-time arithmetic, separate samples, timezone boundaries, and clock drift could select the wrong round. | Boundary trades at 300/1/0 seconds can hit a different market than the signal. | Use aligned Unix seconds for identity/countdown, ET only for display, share one timestamp sample, query the CLOB server clock with latency midpoint correction, and fail closed beyond drift tolerance/network failure. |
| H26 | High | `run_feeds.py:_ProcessLock`, lines 46-102 | Two processes could use the same wallet/ledger, and the old lock path could truncate a selected file or fail on Windows. | Duplicate signals/orders and corrupted accounting result. | Add nonblocking POSIX/Windows file locks, append without truncation, use no-follow where available, retain the handle for process lifetime, and refuse a second process. |
| M1 | Medium | `feeds/poly_user.py:PolyUserFeed._session/_sub_loop/_ping_loop`, lines 242-349 | A quiet user socket looked stale, old market filters accumulated, or a connection looked live after close. | Fill recovery can be delayed or unrelated markets can leak into account telemetry. | Judge quiet-channel liveness by PONG, wait for explicit filters, subscribe/unsubscribe on rotation, close on heartbeat failure, and clear socket state on disconnect. |
| M2 | Medium | `feeds/supervisor.py:SupervisedFeed._supervise`, lines 77-101 | One feed exception could crash sibling tasks or retry forever without bounded cadence. | The bot may lose one safety input or enter a hot loop. | Isolate every feed under a cancellable supervisor with bounded jittered backoff and surfaced health/error state. |
| M3 | Medium | `dashboard/probe.py:_telemetry_failed`, lines 32-36; `dashboard/renderer.py`, lines 41-194 | Probe/render exceptions were silently swallowed and stale display values could look authoritative. | Operators may believe feeds/orders/PnL are healthy when instrumentation failed. | Record DASH warnings and renderer errors, show absent values, source accounting from the ledger/broker, and self-throttle slow rendering. |
| M4 | Medium | `main_bot.py:_append_trade`, lines 79-97 | CSV write failure could crash or relabel an already-posted order. | Internal state can diverge after a live submission. | Treat the CSV as non-authoritative display history, surface the error, and keep the durable fill ledger as the source of truth. |
| M5 | Medium | `run_terminal.py:_legacy_run/run`, lines 160-168; `main_bot.py:main`, lines 382-389 | Legacy/direct entrypoints bypassed hardened feeds/accounting or defaulted live. | A user can accidentally run an unaudited/live path. | Delegate every entrypoint to `run_feeds`; paper is the default and live requires `--live`. |
| M6 | Medium | `run_feeds.py:_ledger_loop`, lines 437-468 | Fill ingestion, disk I/O, and balance reconciliation could run in WebSocket callbacks or fail silently. | Receive stalls drop messages; unsaved fills produce wrong restart PnL. | Move work to its own task, save immediately after new fills, periodically fsync, serialize live balance reads, and surface failures. |
| M7 | Medium | `accounting/ledger.py:unrealized/summary`, lines 312-404 | Unrealized PnL used optimistic/missing marks and win rate counted tokens instead of markets. | Equity and performance are overstated, especially when both outcomes were bought. | Mark to a fresh live bid, report unknown total equity if any position is unmarkable, and grade net resolved PnL once per condition. |
| M8 | Medium | `polymarket_trade.py:_safe_error`, lines 39-50; `feeds/health.py:redact`, lines 89-132 | Raw SDK/feed exceptions could print keys, secrets, passphrases, or private-key-looking hex. | Credential compromise enables unauthorized trading. | Redact configured secrets, secret-labeled fields, and 32-byte hex before logs/events/health; never print signer, credentials, signature, or auth payload. |
| M9 | Medium | All 40 Python files; final AST scan | Bare/silent catches, blocking sleeps in async functions, or HTTP calls without timeouts existed. | Failures disappear, event loops stall, or requests hang through a market boundary. | Remove silent catches, surface state/errors, keep blocking SDK/paper latency work in threads, and add explicit timeouts to every `requests` call. Final targeted scan: 0 findings. |
| M10 | Medium | `accounting/fees.py:CATEGORY_THETA`, lines 29-45 | A stale sports fee constant remained in shared accounting utilities. | Non-BTC reuse would report wrong fees/PnL. | Re-verified the current official table and set sports to 0.05; crypto remains 0.07. |
| L1 | Low | `README.md`, `A5_RESTORE.md`, `RESTORE_A5.md` | Old documents told users to launch directly/live and presented historical wallet settings as current. | Operational error can bypass intended safeguards or expose secrets. | Replace with paper-default commands, archival provenance, live FAIL warning, and no credential values. |
| L2 | Low | `.env.example`, lines 1-52 | Example settings encouraged global cancellation and fixed exposure without explaining impact. | Copy/paste can cancel manual orders or unexpectedly cap/expand risk. | Ship placeholders only, disable global cancellation, explain the dedicated-wallet opt-in, and leave the exposure override commented. |
| L3 | Low | `requirements.txt`, lines 1-5 | Dependencies were unbounded/stale and did not explicitly include the transport needed by the SDK. | A future incompatible release or missing SOCKS transport can break signing/API startup. | Pin compatible major ranges and add `socksio`. |
| L4 | Low | Source/package hygiene | Runtime ledgers, keys, virtualenv caches, bytecode, locks, and logs could be packaged accidentally. | Secrets/state leak or another user starts with false PnL. | Final ZIP excludes `.env`, virtualenv/cache/bytecode, locks, ledgers, accounts, audit JSONL, and runtime CSV logs. |

## Critical bugs found

Eleven Critical findings were repaired: expired-round selection; reversed token
mapping; wrong oracle product; invented mid-window strike; permissive order
success; duplicate-prone POST retry; provisional-fill accounting; non-isolated
paper execution; guessed settlement; continued trading after journal failure;
and unrelated-wallet fill admission.

## All fixes applied

The table above is the authoritative fix list. In summary, the project now has
strict current-market identity, label-based token mapping plus independent CLOB
cross-check, exact Chainlink 60-second TWAP handling, supervised/fresh feeds,
atomic validated books, strict FOK acknowledgement and retry semantics,
durable authorized-fill accounting, independent dual-source settlement,
wallet-wide cancellation opt-in, persistent exposure/process locking, a real
live-public-data paper broker, and a one-way paper/live firewall.

## Files changed

Modified:

```text
.env.example
A5_RESTORE.md
ACCOUNTING.md
DASHBOARD.md
FEEDS.md
RESTORE_A5.md
accounting/__init__.py
accounting/fees.py
accounting/ledger.py
accounting/resolution.py
accounting/settlement.py
chainlink.py
chainlink_strike.py
config.py
dashboard/layout.py
dashboard/probe.py
dashboard/renderer.py
dashboard/state.py
feeds/adapters.py
feeds/binance.py
feeds/book.py
feeds/hub.py
feeds/poly_market.py
feeds/poly_user.py
feeds/reconcile.py
feeds/supervisor.py
main_bot.py
market_discovery.py
orderbook.py
polymarket_trade.py
price_ws.py
requirements.txt
run_feeds.py
run_terminal.py
tests_accounting.py
tests_dashboard.py
tests_feeds.py
tests_fixes.py
timer.py
```

Added: `README.md`, `DEEP_AUDIT.md`, `PAPER_MODE.md`,
`paper_trade.py`, and `tests_paper.py`.

Intentionally unchanged: `strategy.py`, `dashboard/__init__.py`,
`dashboard/theme.py`, `dashboard/widgets.py`, `feeds/__init__.py`, and
`feeds/health.py`.

## Tests performed and results

Original 2026-08-12 deterministic verification. **These are historical
figures, not current ones** - read the follow-up totals at the top of this
report instead. In particular the 77,392 dashboard number below is a
pre-2026-08-25 harness and is NOT comparable to later runs; the 2026-08-25
follow-up records 48,929 for the same suite.

Re-measured 2026-09-12 after the hardening patch: `tests_fixes` 404,
`tests_paper` 135, `tests_accounting` 175, `tests_feeds` 255,
`tests_dashboard` 49,406, `tests_bugfix` 48, `tests_hardening` 72,
`run_terminal.py --selftest` PASS, `band_backtest_selftest.py` 7 - all
passing, 0 failed. The dashboard suite's combinatorial matrices (14x13
terminal sizes, 10 hash sizes, 21 UI states) are byte-identical to the
oldest revision in this repository's history and no test function has been
removed, so the count has only moved upward since then.

| Check | Result |
|---|---|
| `python tests_fixes.py` | 50 passed, 0 failed |
| `python tests_paper.py` | 71 passed, 0 failed |
| `python tests_accounting.py` | 135 passed, 0 failed |
| `python tests_feeds.py` | 171 passed, 0 failed |
| `python tests_dashboard.py` | 77,392 passed, 0 failed |
| Total bundled assertions | 77,819 passed, 0 failed |
| `python run_terminal.py --selftest` | PASS at all 11 sizes |
| `python -m compileall -q .` | PASS |
| Targeted AST operational scan | 40 files; 0 bare/silent catches, blocking `time.sleep` in async functions, or `requests` calls without timeout |
| Strategy SHA/truth-table regression | PASS; strategy SHA unchanged |

The final dependency-isolated test run used local import stubs where the
credentialed CLOB SDK was unavailable. It did not fabricate venue data or test
results: deterministic order responses/books/resolutions are explicitly test
fixtures, and localhost WebSocket tests use real socket servers. No live order,
wallet mutation, credential derivation, or 500 resolved-market replay was run.

## SETTLE_ONCHAIN audit (2026-09-12) - documentation only, no behaviour change

`SETTLE_ONCHAIN` is **off by default and stays off**. This section records
exactly what it changes and exactly how far its central assumption is
actually proven, because that assumption was previously carried only by a
one-line source comment.

### What the flag changes

Default settlement (`accounting/resolution.py:fetch`) requires two
independent surfaces to agree: explicit final CLOB winner flags/payouts AND
Gamma's explicit oracle resolution for the exact slug/condition, with
identical payouts. That invariant is untouched by this audit.

With `SETTLE_ONCHAIN=1`, a block ahead of that logic reads the Polygon
Conditional Tokens contract directly. When the chain answers, `fetch`
returns `RESOLVED` from that single source and **returns before
`parse_clob_market` is ever called** - so on this path the CLOB winner flag
is not consulted and Gamma is not fetched. The two-surface agreement does
not apply to it. The motive is latency: the chain resolves ~85s after a
round, the API mirrors ~10 minutes.

### How token ordering is proven - and how far

Payout correctness rests entirely on one mapping. `payouts_for(cid,
slot_tokens)` reads `payoutNumerators(conditionId, i)` for `i` in `0..1` and
zips them **positionally** onto the `token_ids` it is handed. `fetch` builds
that list as:

```python
slot_tokens = [str((row or {}).get("token_id") or "")
               for row in (clob_data.get("tokens") or [])]
```

i.e. the order of the `tokens` array in the CLOB `/markets/{cid}` response.
So the claim being relied on is: **CLOB `tokens[i].token_id` is the token for
on-chain outcome slot `i`.**

What actually backs that claim, precisely:

| | status |
|---|---|
| Asserted in code | yes - a comment in `resolution.py`: "verified across 9 rounds to match the on-chain outcome-slot order" |
| Verified at runtime | **no** - nothing compares the chain's winner to any other surface on this path |
| Guaranteed by the CLOB API contract | **not established** - the ordering is not documented as stable by the venue; the repository cites no specification for it |
| Sample size behind it | 9 rounds, all presumably the same market type |

`payouts_for` does validate a great deal - a 66-char condition id, a
distinct binary token pair, `denominator > 0`, numerators summing to the
denominator, and a split that is cleanly `1/0` or `50/50` - and fails closed
on every one. **None of those checks can detect a reversed order.** A
swapped pair still produces a perfectly well-formed single-winner split; it
simply pays the wrong token. The failure mode is therefore silent and total:
the loser settles at 1.0, the winner at 0.0, and the ledger records it as
final. `Ledger.settle` refuses to settle a position twice, so a wrong payout
written this way is not self-correcting.

Note this is also the one branch where the `50/50` case is inferred rather
than read: `fetch` labels the winner `"50/50"` when every chain payout is
0.5, which is order-independent and therefore unaffected.

### What would actually prove it

Nothing below is implemented, by design - this audit changes no settlement
behaviour.

1. **Cheap runtime cross-check.** When the chain answers AND the CLOB
   response already carries an explicit winner, compare them and fail closed
   on disagreement. `clob_data` is in hand at that point, so this costs no
   extra request. It would turn a silent inversion into a refusal.
2. **Derive the mapping instead of assuming it.** Compute each outcome
   slot's position id from `conditionId` and the index set, and match tokens
   by that rather than by array position. This removes the assumption
   entirely rather than checking it.
3. **Widen the sample.** 9 rounds is not enough to call an undocumented API
   ordering stable. Record chain-vs-CLOB winner agreement over a few hundred
   settled rounds before trusting it unsupervised.

### Operating guidance until then

Leave `SETTLE_ONCHAIN=0` (the default) for any run whose PnL matters.
`SETTLE_TRUST_GAMMA` carries a smaller version of the same caveat: it lets a
`PENDING` (merely lagging) CLOB surface be settled from Gamma alone, though
it still fails closed on `UNKNOWN`. Both are listed, off, in `.env.example`.

## Remaining risks

1. The pinned `py-clob-client-v2` has an open type-3/POLY_1271 API-key
   derivation defect. Type 3 is blocked, while Polymarket’s current onboarding
   recommends it for new deposit wallets. Migrating to the newer unified SDK
   needs a separate credentialed compatibility project.
2. Live credentials, funder/signature pairing, pUSD allowance/balance, current
   response objects, user WebSocket lifecycle, REST trade reconciliation, and
   delayed FOK behavior still need real-venue validation.
3. Public-book paper fills cannot reproduce private matcher queue position,
   movement during a real network/signing trip, individual-maker fee rounding,
   exchange outage behavior, or every source of network jitter.
4. This bot records resolution payout in PnL but does not redeem outcome tokens.
   Wallet pUSD reconciliation will show redemption/manual activity as external
   movement.
5. FOK market orders should never rest, so account order heartbeats are not
   started. An unexpected resting-order response is treated as ambiguous and
   blocks the round. Any future GTC/GTD path would require authenticated
   five-second order heartbeats.
6. A contradictory terminal fill is quarantined in the in-memory feed store.
   The protocol defines terminal states, so a later contradiction should not
   occur; if it did after a prior CONFIRMED record had already been durably
   ingested, operator investigation/wallet reconciliation would still be
   required.
7. Availability, legal eligibility, geofencing, capital risk, strategy edge,
   and market risk are outside code correctness. Passing tests do not establish
   profitability.
8. A process crash after the venue accepts a POST but before its order ID is
   durably authorized can still leave an unattributable fill and reopen the
   round after restart. Closing this requires a credential-validated
   write-ahead signed-order hash/intent protocol.
9. Durable REST recovery is deliberately bounded to two hours and is not an
   unbounded wallet scan. A longer outage needs an operator-led historical
   reconciliation before live trading resumes.
10. The installed CLOB SDK has a five-second timeout per REST page, but does
    not expose an aggregate pagination deadline. Pathological endless
    pagination remains a controlled-live-test requirement.
11. Fee-inclusive exposure restoration, retry-time signal/book freshness, and
    exact paper/live minimum-size parity remain follow-up hardening items.

## Readiness verdict

- Paper-mode readiness: **PASS** for code-level, credential-free live-public-data
  simulation. Start with `python run_feeds.py --paper --dash`. A long-running
  public-network soak is still recommended.
- Live-trading readiness: **FAIL** pending the credentialed tests below. Do not
  run unattended or assume production safety from deterministic tests.

## Requires real credentials/network testing

Before changing the live verdict, run all of these with a dedicated, minimally
funded wallet:

1. Validate account type, funder, signer, derived API key, balance, and every
   required allowance without logging secrets.
2. Observe at least one complete current/next BTC five-minute rotation and
   confirm Gamma/CLOB token mapping and start/end timestamps.
3. Soak Binance, RTDS, market WS, and user WS through forced disconnects,
   silence, reconnect, resubscription, and REST resync.
4. Submit one minimum-size canary FOK; verify the exact SDK response,
   delayed/no-fill behavior, fresh retry signature, user-stream terminal state,
   actual fill price/shares/fees, and REST reconciliation.
5. Restart inside the same round; verify accepted exposure and dedup state are
   restored and no duplicate order/fill is created.
6. Let the canary market resolve; verify CLOB and Gamma agreement, one-time
   settlement, payout, realized PnL, and later wallet redemption movement.
7. Perform a controlled shutdown during a pending order/position and verify no
   unrelated wallet orders are canceled.

## Protocol references

- https://docs.polymarket.com/market-data/chainlink-twap
- https://docs.polymarket.com/market-data/realtime-data
- https://docs.polymarket.com/trading/place-orders
- https://docs.polymarket.com/concepts/order-lifecycle
- https://docs.polymarket.com/trading/manage-orders
- https://docs.polymarket.com/trading/fees
- https://docs.polymarket.com/api-reference/markets/get-clob-market-info
- https://docs.polymarket.com/concepts/resolution
- https://github.com/Polymarket/py-clob-client-v2
- https://github.com/Polymarket/py-clob-client-v2/issues/70
