# Washout-Bounce Rider — cbBTC 24h race bot

A Python bot for the make.fun `btc-race-24h-20261113-2314` bounty. It speaks
`agent-fun/bars-replay/v1`: one `hello`, one 1-minute CBBTC/USDC `bar` per
`batch`, one reply with `orders` per batch. Standard library only, plus the
kit's Python SDK shim vendored under `vendor/` (byte-identical copy of
`sdk/makefun_bars.py`). No network, no wall clock, no files outside this
repository; all decisions are integer basis-point math on the closes the
`hello` stream has sent, so two runs on the same bars produce identical
`orders`.

## Strategy

The dev window is a slow downtrend (-4.9% over 24h) with two sharp V-bounces.
Trend-following bleeds on whipsaws there (the kit's EMA-cross baseline loses
~$109); the edge is buying the bounce after a washout, not the trend.

1. **Washout** — the close has fallen at least **75 bps over the last 45 bars**.
2. **Turn** — the **5-bar return crosses upward through +20 bps**. The cross is
   the confirmation that sellers are exhausted; the bot never buys the fall
   itself.
3. **Entry** — full cash (100% of equity) on the confirming bar, filled at the
   next bar's open. One position at a time.
4. **Exit** (whichever comes first, decided on each bar's close, filled at the
   next open):
   - take profit **+150 bps** over the fill bar's raw open,
   - stop loss **-20 bps** under it (a genuine bounce does not retest its
     entry, so a small dip means the read was wrong),
   - trailing stop **25 bps** under the best close since entry,
   - time stop after **120 bars** held.
5. **Cooldown** — 30 bars of rest after any exit, so one washout cannot be
   re-bought while it keeps falling. A buy the replay rejects (e.g. slippage)
   may be retried for 5 bars on the level condition.

Exit levels are measured against the raw open of the bar the entry filled at,
not the filled price, so the fee, slippage and pool impact the entry paid do
not move them.

## Parameters (`main.py`)

| Parameter | Value |
|---|---|
| `FALL_BARS` / `FALL_BPS` | 45 bars / -75 bps |
| `TURN_BARS` / `TURN_BPS` | 5 bars / +20 bps |
| `TAKE_PROFIT_BPS` | +150 |
| `STOP_LOSS_BPS` | -20 |
| `TRAIL_BPS` | 25 |
| `MAX_HOLD_BARS` | 120 |
| `COOLDOWN_BARS` | 30 |
| `MAX_SLIPPAGE_BPS` | 100 |

## Dev-window grade (local replay)

Graded locally with a faithful re-implementation of the kit's replay
(`contracts/bars-protocol.ts` + `sim/bars/engine.ts` fill math: 4 bps fee,
2 bps slippage, constant-product impact against each bar's open depth,
flatten at the last close). The bot was run twice; both runs produced
byte-identical replies.

| Metric | Value |
|---|---|
| Net PnL | **+$7.86** |
| Return | +0.79% |
| Trades | 6 (3 round trips) |
| Max drawdown | 0.74% |
| Rejected orders | 0 |

Trade log (decided bar → fill bar): buy 254 → stop-loss 256 (-$5.13);
buy 414 → take-profit 467 (+$10.75); buy 991 → trailing stop 1008 (+$1.85).
For comparison on the same replay: buy-and-hold -$52.59, momentum -$49.23,
EMA-cross -$109.38, hold-cash $0.00.

## Robustness

Neighbouring parameter sets give the same 3 trades and +$5.31…+$7.86
(45-bar fall -70…-75 bps, 5-bar turn 18…22 bps, trail 25…30 bps, take-profit
135…150 bps), so the result is a plateau, not a single tuned point. The
result is sensitive to missing the big bounce (a 45-bar fall threshold of
-85 bps or a turn threshold of 15 bps skips it), which is the known risk of
a 3-trade strategy on one window; the race window is a different 24 hours,
and the washout-bounce pattern is the recurring BTC microstructure the
strategy is built on, not a curve-fit to one bar.
