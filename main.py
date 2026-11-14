"""Washout-bounce rider for cbBTC (agent-fun/bars-replay/v1).

The bot reads one 1-minute bar of CBBTC/USDC per batch from the hello stream
and answers each batch with at most one order. It is long only, starts with
1,000 USDC of paper money, and trades only the sharp washout-bounce pattern:

  * Washout: the close has fallen at least 80 bps over the last 45 bars.
    In a falling tape most 1-minute moves are noise, so the bot waits for a
    fall big enough that the bounce after it is worth the ~26 bps a full-size
    round trip costs in fees, slippage and pool impact.
  * Turn: the 5-bar return crosses upward through +20 bps. The cross is the
    confirmation that sellers are exhausted; buying the fall itself would
    catch a falling knife, so the bot only enters on the first bar the
    short-term return turns positive again.
  * Exit: take profit at +150 bps over the fill bar's open, stop loss at
    -20 bps under it, a trailing stop of 30 bps under the best close since
    entry, or a 120-bar time stop, whichever comes first. The levels are
    measured against the raw open the fill traded at, not the filled price,
    so the fee, slippage and impact the entry paid do not move them.

All decisions use integer basis-point math on the closes the hello stream
has sent. No randomness, no wall clock, no network, no files outside this
repository: the same bars always produce the same orders.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))

import makefun_bars

SYMBOL = "CBBTC"

# --- entry: the washout and the turn that confirms it (basis points) ---
FALL_BARS = 45          # washout lookback, in bars
FALL_BPS = -75          # the 45-bar fall that marks a washout
TURN_BARS = 5           # turn-confirmation lookback, in bars
TURN_BPS = 20           # 5-bar return that must be crossed upward
# --- exit: take profit, stop loss, trailing stop, time stop ---
TAKE_PROFIT_BPS = 150
STOP_LOSS_BPS = 20
TRAIL_BPS = 25
MAX_HOLD_BARS = 120
# --- pacing ---
COOLDOWN_BARS = 30      # bars of rest after an exit before a new entry
RETRY_BARS = 5          # bars a rejected buy may be retried on (level entry)
MAX_SLIPPAGE_BPS = 100  # 1% past the close the order was decided on


def _bps(delta, base):
    """delta / base in basis points, truncated toward zero (ints only)."""
    if base == 0:
        return 0
    q = abs(delta) * 10000 // abs(base)
    return q if (delta >= 0) == (base >= 0) else -q


class Rider:
    """The strategy's memory: closes seen, the open position, the cooldown."""

    def __init__(self):
        self.closes = []            # closes, oldest first, ints at price_scale
        self.prev_turn = None       # previous bar's TURN_BARS return, in bps
        self.entry_price = None     # fill price of the open position
        self.peak = None            # highest close since entry
        self.hold_bars = 0          # bars held, counted from the entry fill
        self.cooldown_until = -1    # seq before which no new entry is allowed
        self.retry_until = -1       # seq until which a rejected buy retries

    # -- fills: learn what the last reply's orders did -------------------
    # A buy's fill price carries the fee, slippage and pool impact the
    # fill paid, so it is the wrong ruler for the exit levels: the levels
    # are measured against the raw open of the bar the fill traded at
    # (the reference price the replay filled against), which the batch
    # carries beside the fill. The fill price itself is kept only as a
    # fallback when that bar is absent.
    def _fold_fills(self, state, fills, open_ref):
        for fill in fills or []:
            if fill.get("symbol") != SYMBOL:
                continue
            if fill.get("status") != "filled":
                if fill.get("side") == "buy":
                    self.retry_until = state.seq + RETRY_BARS
                continue
            if fill.get("side") == "buy":
                self.entry_price = open_ref if open_ref is not None else int(fill["price"])
                self.peak = self.entry_price
                self.hold_bars = 0
            elif fill.get("side") == "sell":
                self.entry_price = None
                self.peak = None
                self.hold_bars = 0
                self.cooldown_until = state.seq + COOLDOWN_BARS

    def _entry_price_fallback(self, state, held):
        """Fill price from the position's cost when no fill was seen."""
        if held is None or held.qty <= 0:
            return None
        quote_scale = state.hello["capital"]["quote_scale"]
        qty_scale = state.symbols[SYMBOL]["qty_scale"]
        price_scale = state.symbols[SYMBOL]["price_scale"]
        return held.cost * 10 ** (qty_scale + price_scale) // (
            held.qty * 10 ** quote_scale
        )

    # -- one batch --------------------------------------------------------
    def on_batch(self, state, batch):
        fresh = [b for b in batch.get("bars", []) if b.get("symbol") == SYMBOL]
        open_ref = int(fresh[0]["o"]) if fresh else None
        self._fold_fills(state, batch.get("fills", []), open_ref)
        if state.warming_up:
            return []
        if not fresh:
            return []  # no new bar for this symbol: nothing to decide on
        close = int(fresh[0]["c"])
        self.closes.append(close)

        held = state.positions.get(SYMBOL)
        holding = held is not None and held.qty > 0
        if holding:
            return self._manage(state, held, close)
        return self._maybe_enter(state, close)

    def _manage(self, state, held, close):
        """Held: update the peak and sell when an exit rule fires."""
        if self.entry_price is None:
            self.entry_price = self._entry_price_fallback(state, held)
            self.peak = self.entry_price
            self.hold_bars = 0
        if self.entry_price is None:
            return []
        if close > self.peak:
            self.peak = close
        self.hold_bars += 1
        gain = _bps(close - self.entry_price, self.entry_price)
        best = _bps(self.peak - self.entry_price, self.entry_price)
        if (
            gain >= TAKE_PROFIT_BPS
            or gain <= -STOP_LOSS_BPS
            or best - gain >= TRAIL_BPS
            or self.hold_bars >= MAX_HOLD_BARS
        ):
            return [
                {
                    "side": "sell",
                    "symbol": SYMBOL,
                    "qty": held.qty,
                    "max_slippage_bps": MAX_SLIPPAGE_BPS,
                }
            ]
        return []

    def _maybe_enter(self, state, close):
        """Flat: enter on a confirmed washout bounce, once per cooldown."""
        turn = None
        fall = None
        if len(self.closes) > TURN_BARS:
            turn = _bps(
                close - self.closes[-TURN_BARS - 1], self.closes[-TURN_BARS - 1]
            )
        if len(self.closes) > FALL_BARS:
            fall = _bps(
                close - self.closes[-FALL_BARS - 1], self.closes[-FALL_BARS - 1]
            )
        edge = (
            turn is not None
            and self.prev_turn is not None
            and self.prev_turn < TURN_BPS <= turn
        )
        self.prev_turn = turn
        if fall is None or fall > FALL_BPS:
            return []
        if state.seq < self.cooldown_until:
            return []
        retry = turn is not None and turn >= TURN_BPS and state.seq <= self.retry_until
        if not (edge or retry):
            return []
        min_notional = int(state.hello["fees"]["min_notional_quote_units"])
        if state.cash < min_notional:
            return []
        return [
            {
                "side": "buy",
                "symbol": SYMBOL,
                "notional_quote_units": state.cash,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }
        ]


def on_batch(state, batch):
    return _RIDER.on_batch(state, batch)


_RIDER = Rider()

makefun_bars.run(on_batch)
