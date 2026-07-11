# THE DAY TRADER REPLICA — Implementation & Design Plan
### (the "playbook engine": code that works a trading day the way a full-time day trader does)

*Authored by Fable, 2026-07-11. One file, on purpose. This is the plan we
build from; nothing in here is implemented yet unless marked DONE.*

---

## 0. What we're actually building (one paragraph)

Not another single-strategy bot. A **day-trader replica**: software that runs
the same *workday* a professional discretionary day trader runs — pre-market
prep, classify the day, pick the right play from a playbook, execute with
strict risk, manage the trade, flatten by session close, journal the result,
review the week, bench what stops working. Individual strategies (ORB, VWAP
bands, Velocity) become **plays in the book**, not the product. The product
is the routine.

## 1. Honest framing (read first)

- **Nobody can promise "profitable."** Most day traders lose. What code CAN
  replicate is the part that separates surviving traders from blown accounts:
  process, risk discipline, and honest review. The edge itself must be proven
  in backtests + testnet before a dollar is risked, and re-proven while live.
- Current inventory: 3 strategies coded, **0 validated on real data** in this
  environment (network-blocked). ORB+VWAP has one 90-day walk-forward from
  June (modest edge, PF 1.20). VWAP Bands is machinery-tested only.
  Velocity-Z holds ~34h — that's swing, not day trading; it stays a separate
  bot, not a play in this book.
- Every phase below has an **exit gate**. If a gate fails, we stop and fix,
  not push forward. That rule is the whole point of replicating a pro.

## 2. Architecture — the pieces of a trader's day

All of this lives in **one file: `daytrader.py`** (same convention as
`vwap_strategy.py` — you always know where everything is), with one SQLite DB
and one config block at the top. Estimated ~2,000 lines. Sections:

### 2.1 The Playbook (pluggable setups)
Each play is a small class with the same interface:
`scan(symbols) -> candidates`, `signal(candles) -> Signal|None`,
`manage(position, candle) -> action`, plus a declared **day-type affinity**
and its own validated parameters.

Launch plays (all reusing code we already have):
| Play | Source | Fires when | Direction |
|---|---|---|---|
| ORB breakout | day-trader-bot/strategy.py | trend/expansion days | long+short |
| VWAP band fade | vwap_strategy.py `revert` | range/balance days | long+short |
| VWAP reclaim | vwap_strategy.py `reclaim` | trend days, post-pullback | long+short |

The book is open-ended — new plays get added ONLY after passing the Phase-3
validation gate on their own.

### 2.2 Pre-market scanner (what a trader does before the open)
Before each session (00:00 & 13:30 UTC): pull the symbol universe
(SOL, ETH, BTC, AVAX + configurable), compute overnight range vs ATR, volume
vs 20-day norm, gap size. Output a ranked **watchlist of 1–3 symbols per
play**. No scan pass → no trading that session (pros skip dead days).

### 2.3 Day-type classifier (read the tape first)
Simple, deterministic, computed 30–60 min into the session: opening range
width vs ATR, early volume, gap-and-go vs gap-fade behaviour →
**TREND / RANGE / NEWS-CHOP**. Trend days enable breakout+reclaim plays,
range days enable fade plays, chop days = reduced size or flat. This gate is
what stops the classic failure mode: fading a trend day all day long.

### 2.4 Risk manager (the prop-desk layer — non-negotiable rules)
- 0.5–1.0% equity risk per trade (starts at 0.25% when live, see ramp)
- Hard daily stop: −2% realized → flat + no new trades until next day
- Max 2 concurrent positions; **correlation guard**: SOL+ETH+BTC count as
  one crypto bet — combined risk capped at 1.5%
- Weekly circuit breaker: −5% week → 2-day pause (velocity-bot pattern)
- Equity floor: below 75% high-water → engine refuses to trade (v3 pattern)
- All state persisted (velocity_state.json pattern) — a restart can never
  disarm a limit. Server-side brackets on every entry (crash-safe).

### 2.5 Execution engine
Reuse `day-trader-bot/exchange.py` (Bybit v5, already fixed: explicit
interval, tick-size rounding, position-side PnL). One-way mode, one
subaccount, market-in with attached SL/TP, breakeven at +1R, flatten at
session close.

### 2.6 The journal (what pros write down, automated)
Every trade row: play name, symbol, day-type call, entry reason string,
R-multiple result, MAE/MFE, session. End-of-day auto-summary. Writes to the
**command-centre SQLite (localhost:8080 schema — src/bot/db.py)** so it
lists on your dashboard next to the AVAX bot, plus a human-readable
`journal.md`.

### 2.7 The weekly review (the self-improvement loop)
Cron/manual command: per-play expectancy, PF, win% by day-type over trailing
30/90 days. Rules-based bench system: play under PF 1.0 over its last 30
trades → size halved; still failing after 20 more → benched (paper-trades
only until it re-qualifies). This automates the discipline traders fail at:
killing a dying edge.

## 3. Phased delivery plan

**Phase 1 — Skeleton + plays ported (build)**
`daytrader.py`: config, playbook interface, the 3 plays wrapped, scanner,
day-type classifier, risk manager, journal. Offline `selftest` on synthetic
data (same pattern as vwap_strategy.py).
*Gate: selftest ALL PASSED; every play fires long AND short in synth.*

**Phase 2 — The unified backtester (this is the big one)**
Replays the ENTIRE day loop — scanner, classifier, playbook, risk manager —
over multi-symbol historical data simultaneously (not per-strategy in
isolation). Fees + slippage model. Then the "hell of a lot of backtests":
parameter sweeps per play, K-fold walk-forward, per-day-type attribution,
and a Monte-Carlo reshuffle of the trade sequence for realistic drawdown
bands. **Needs market data: either you allowlist `api.bybit.com` for this
environment, or we run on your Mac and you paste results.**
*Gate (per play, out-of-sample): PF ≥ 1.3, ≥ 100 trades, max DD ≤ 15%,
positive expectancy in at least 2 of 3 walk-forward folds. Plays that fail
launch benched, not deleted.*

**Phase 3 — Testnet month (paper trading, like a funded-account tryout)**
Full engine on Bybit testnet 2–4 weeks. Slippage audit: live fills vs
backtest assumptions. Dashboard integration verified on localhost:8080.
*Gate: engine uptime clean, no risk-rule violations, live expectancy within
1 std-dev of backtest.*

**Phase 4 — Live ramp (the way desks actually ramp a trader)**
Bybit subaccount, small balance. Risk 0.25%/trade for first 30 trades →
0.5% next 30 → 1.0% only if realized expectancy still matches. Any daily
stop breach or unexplained divergence → back one ramp level. Deploy to
Railway beside the AVAX bot once stable.
*Gate to full size: 60+ live trades, equity curve within backtest bands.*

## 4. What I need from you (the only blockers)

1. **Market data access** — allowlist `api.bybit.com` in this environment's
   network settings, OR run the backtest commands on your Mac when I hand
   them over. Phase 2 is impossible without one of these.
2. **Risk numbers confirmed** — the plan assumes: $ account size you choose,
   1% max/trade, −2% daily, −5% weekly. Say the word if different.
3. **Symbol universe** — default SOL/ETH/BTC/AVAX perps. Add/remove freely.
4. (Nothing else. trader.dev is not needed — it's a Pine-strategy
   marketplace we scout, not a backtester our Python can run on.)

## 5. What we already have that plugs straight in (no rework)

- Bybit exchange wrapper with the v3 bug fixes — execution layer ✓
- ORB strategy + 28 tests — play #1 ✓
- VWAP bands one-file bot + selftest — plays #2 and #3 ✓
- Risk patterns proven in Velocity v3 (persistent state, breaker, floor) ✓
- Dashboard SQLite schema — journal target ✓
- Backtest engines in both bots — merge into the unified one ✓

*End of plan. Next action when you approve: Phase 1 build of `daytrader.py`.*
