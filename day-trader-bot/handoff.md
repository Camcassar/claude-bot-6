# HANDOFF — Day Trader Bot (ORB + VWAP, Bybit) + trader.dev scouting

## VWAP BAND STRATEGY BUILT (2026-07-10, single file)

New standalone strategy `vwap_strategy.py` (repo ROOT, deliberately ONE
self-contained file: theory + indicators + signal + backtester + sweeps +
walk-forward + live bot + optional dashboard DB writer). Authored by Opus by
pulling the reusable pieces out of the ORB bot's VWAP work (session-anchored
VWAP, right-side-of-VWAP bias, volume confirmation, ATR filter, risk sizing,
breakeven, flat-at-close). Hypothesis: intraday mean-reversion around session
VWAP measured in volume-weighted sigma bands; two modes (`revert` default,
`reclaim` momentum). Self-tested on SYNTHETIC data only (cloud env had no
Bybit egress) — `python vwap_strategy.py selftest` = ALL PASSED. Real
validation pending on the Mac: `backtest` / `sweep` / `walkforward`
subcommands auto-fetch ETH/SOL/etc from Bybit public API. Wires into the
localhost:8080 command centre via `--db <path>` (schema matches src/bot/db.py).
Caught+fixed a real blow-up bug (gap-onto-stop -> ~0 stop distance -> giant
size -> -95R) with a min_stop_pct floor; guard lives in both the live and
backtest signal paths.

## VELOCITY-Z V3 BUILT (2026-06-12, Claude Code session)

Re-reverse-engineered v2 and found a **critical bug: v2 never passed its
interval to the exchange, so the "ETH 1h" strategy was actually running on
the ORB bot's 5-minute candles** — all v2 backtest evidence was for a
strategy the live bot wasn't trading. v3 (velocity_bot.py) fixes that and
adds, from offline analysis of the 136 verified trades
(scouting/analyze_losses.py — new):

- persistent risk state (velocity_state.json): breaker/cooldown/PnL cursor
  survive restarts (v2's breaker was disarmed by any redeploy)
- 24h same-side cooldown after a loss (worst bucket in trade list: PF 0.60)
- slippage-buffered sizing (trade #52 gapped -4.32% vs the -2.58% bracket)
- stop-and-reverse on opposite signal (the original does this — trade #35
  exits exactly at #36's entry; ALLOW_FLIP, default on)
- equity floor kill switch (75% of high-water mark)
- tick-size-conformant SL/TP prices

New: backtest_velocity.py (train/OOS ablation table, auto-fetches ETH 1h —
run it on the Mac, this cloud env can't reach Bybit) and test_velocity.py
(22 offline tests, all passing; ORB's 28 still pass).
NEXT: 1) run `python backtest_velocity.py` locally and drop any rule that
degrades the OOS column; 2) testnet velocity v3 + ORB side by side.
Full details in scouting/REPORT.md ("Velocity-Z v3" section).

## VELOCITY-Z V2 BUILT (2026-06-12)

Reverse-engineered Hilbert F36's theory from its 136 verified trades and
built an improved bot: **velocity_bot.py** (ETHUSDT 1h, velocity z-score
momentum + 400h trend filter + 2% risk sizing + bracket TP 5.5%/SL 2.5% +
circuit breaker). Full analysis in scouting/REPORT.md; signal fitting in
scouting/fit_signal.py; variant tests in scouting/improve.py (train 2024-25
/ OOS 2026 methodology — most filters were overfit and rejected).
OOS 2026: +23.5%, max DD 15.5%, PF 1.61. Replica recall vs original: 74%.
Runs with the same .env / exchange.py as the ORB bot. NEXT: testnet both
bots side by side.



## SCOUTING UPDATE (2026-06-10/11)

Screened trader.dev / strategyfactory.ai library (27,188 strategies) with 3
agents via public API `https://mcp-api.strategyfactory.ai/strategies/search`
(sort=profit|sharpe|recent|winrate|drawdown; symbol=XXXUSDT works; limit max 25).
~95% of the board is junk (martingale "100% win" grids, <50-trade lucky runs,
duplicate re-uploads). Survivors after screen (trades>=100, >=60d window,
PF 1.2-5, DD<=30%):

**WINNER — "Hilbert F36 ETH 1h — minVelZ2 TP5.5" (Joshua Afonso)**
ETHUSDT 1h, Jan 2024→Jun 2026 (2.4yr), +664.6%, PF 2.28, 136 trades,
49.3% win, TP +5.5% / SL -2.5% fixed brackets, long+short.
FORENSICALLY VERIFIED against real Bybit data (scouting/verify_hilbert.py):
all 136 entries inside real bars, all exits reachable, zero impossible fills
(report times are Brisbane UTC+10 → shift -10h to UTC).
True closed-trade max DD 11.7% (report claims 7.9% — understated).
Per-year: 2024 PF 2.38, 2025 PF 2.10, 2026 PF 1.86 — consistent but mild decay;
Apr-May 2026 was a losing streak (-$5.4k over 6 trades). Trade list:
scouting/hilbert_f36_trades.csv. Report:
https://mcp-api.trader.dev/backtest/01KTHTH5Z5PEGXZ86B023W0RMP

Runners-up: BTC V11 (BTCUSDT 1h, PF 2.31, DD 22%, 145 trades, 2.4yr),
F40d walk-forward family (ETH/BTC 1h, PF 1.4-1.7, 240-340 trades, honest
walk-forward author), DemonDays TEMA (SOL 5m, PF 1.5-1.6, 90d only).

**BLOCKER: strategy source code (Pine) is paid-tier only on trader.dev.**
Options: (a) Cam upgrades (Starter $9.99/mo) → port Pine to Python here →
validate in our backtester → testnet; (b) reverse-engineer Hilbert velocity
z-score entry from the 136 verified entry timestamps (uncertain).
Cam's trader.dev API key unlocks report viewing (he pastes it himself —
Claude must never enter it). Key was exposed in chat; Cam to rotate it.



Context file for resuming work in Claude Code or any future session.
Last updated: 2026-06-10.

## What this is

Cam's second trading bot (first = AVAX EMA 5/20 cross bot, hosted on
Railway). This one automates a full day-trader workflow: **Opening Range
Breakout + VWAP bias + volume confirmation** on **SOLUSDT perps**, Bybit v5
API, 5-minute candles, polling every 30s. Built and verified 2026-06-10 in
Cowork; runs on Cam's Mac against **Bybit testnet** first.

## Status

- [x] All code written and compiling
- [x] 28/28 unit tests passing (`python test_strategy.py`)
- [x] Backtested on 90 days real SOL 5m data (bundled: `sol_5m_history.csv`)
- [x] Params tuned via walk-forward split (see below)
- [ ] Cam to create Bybit **testnet** API keys → `.env` → run `python bot.py`
- [ ] Observe on testnet ~1-2 weeks
- [ ] Go live: Bybit **subaccount** keys + `TESTNET=false` (no code changes)
- [ ] Deploy to Railway next to AVAX bot (Dockerfile ready; free trial won't
      cover 24/7 — needs Hobby plan ~$5/mo). Railway connects via custom
      connector: Settings → Connectors → `https://mcp.railway.com/mcp`
- [ ] Wire into Cam's "command centre" alongside the AVAX bot

## Files (all in this folder)

| File | What it does |
|---|---|
| `bot.py` | Main loop. State machine: session detect → opening range → breakout signal → market order with server-side SL/TP → breakeven move at +1R → flatten at session close. Adopts existing position on restart. |
| `strategy.py` | Pure signal logic, zero API calls (deliberately, for backtestability). `OpeningRange`, `Signal` dataclasses; `current_session`, `build_opening_range`, `range_is_tradeable`, `check_breakout`, `should_move_to_breakeven`. |
| `indicators.py` | `ema`, `atr` (Wilder), `session_vwap` (anchored), `avg_volume`. Pure functions over candle dicts `{ts,open,high,low,close,volume}`, oldest→newest. |
| `risk.py` | `position_size` (risk-based, floors to qty step — careful: float modulo bug was fixed here, keep the `math.floor(round(...))` form). `DayTracker` persists daily PnL/trade counts to `bot_state.json` so restarts don't reset the daily loss limit. |
| `exchange.py` | All Bybit calls (pybit `unified_trading.HTTP`). Market orders carry `stopLoss`/`takeProfit` so a bot crash never leaves an unprotected position. `get_closed_pnl` feeds the daily loss tracker. |
| `config.py` | Every tunable. Secrets via `.env` (python-dotenv). |
| `backtest.py` | Replays the exact strategy over history. `--days N` fetches from Bybit public API (no keys); `--csv file` replays offline. Fees modeled at 0.055% taker both sides. Conservative intracandle ordering: stop checked before TP. |
| `test_strategy.py` | 28 synthetic-candle tests: indicators, session windows, range build, all signal accept/reject paths, breakeven, sizing. |
| `sol_5m_history.csv` | 90 days SOL 5m candles (Mar–Jun 2026), 25,920 rows, for offline replay. |
| `.env.example`, `requirements.txt`, `Dockerfile`, `README.md` | Setup/deploy. |

## Strategy parameters (current defaults in config.py)

- Sessions (UTC): 00:00 and 13:30; opening range = first 30 min (6×5m);
  session window 6h, flat at close
- Entry: 5m close beyond range ±, volume ≥ **2.0×** 20-candle avg, right side
  of session VWAP, range 0.5–3.0× ATR(14)
- Exits: SL opposite range side, TP **3R**, breakeven at +1R
- Risk: 1%/trade, max 1 long + 1 short per session, daily stop −3%, leverage cap 3x

## Backtest evidence (and its limits)

90d SOLUSDT, $10k: **+14.25%, 116 trades, 24% win rate, PF 1.20**.
Param sweep (27 combos) + 45d/45d walk-forward: vol=2.0/TP=3R/BE=1R was the
only combo positive in both halves (+12.4% / +3.0%). Neighbouring combos
mostly negative → edge is modest and parameter-sensitive; 90 days is a small
sample. Sweep code is inline-runnable (itertools.product over config values,
then `run_backtest`). Backtest is intentionally pessimistic but is NOT
tick-accurate: entries fill at candle close, no slippage modeled.

## Known gaps / next ideas

- No slippage model in backtest; testnet will show real fill behaviour
- `RANGE_MIN_ATR` 0.5 vs 1.0 made no difference in sweep (no SOL session
  range that narrow) — re-check on quieter symbols
- Could add: Telegram/Slack alerts on entry/exit, equity curve logging for
  the command centre, second symbol via env var (code is symbol-agnostic)
- Bot loop assumption: only ONE bot instance per Bybit (sub)account per
  symbol — `get_position`/`close_position` aren't multi-position aware
  (positionIdx=0, one-way mode required)

## How to resume

```bash
cd day-trader-bot
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python test_strategy.py                      # should print 28 passed
python backtest.py --csv sol_5m_history.csv  # should print +14.25%
cp .env.example .env                         # add testnet keys, then:
python bot.py
```
