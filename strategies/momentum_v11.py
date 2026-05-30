"""
strategies/momentum_v11.py — MomentumV11Strategy

High-volume candle momentum with ADX regime filter, body-quality filter,
trail-activation threshold, and fixed-qty sizing.
Ported from Pine Script "VR11 - regimeFilter".

Logic
-----
Entry long  : bullish candle (close > open by >= price_move_pct %)
              AND volume > vol_multiplier × SMA(volume, vol_len)
              AND long body quality  >= min_body_quality_long
              AND ADX > adx_threshold  (trending regime only)
              AND (optional) close > EMA(close, ema_len)

Entry short : mirror of long using bearish candle + short body quality.

Exit        : whichever triggers first —
                • hard stop loss : entry ± entry_price * stop_loss_pct / 100
                • trailing stop  : activates only after price moves
                  trail_activate_pct % in favour; then trails
                  trail_distance_pct % behind the best close seen

Body quality = body_size / candle_range (0 = doji, 1 = full momentum candle).
Regime filter: ADX > adx_threshold → trending; below → choppy, no trades.

Notes
-----
- One trade open at a time; one entry per bar (enforced by BackTrader's next()).
- Trail activation and distance are fixed in $ at entry time (≈ Pine's
  trail_points / trail_offset computed from entry price).
- Direction control: allow_long / allow_short let you trade one or both sides.
"""

import backtrader as bt
import pandas as pd


class MomentumV11Strategy(bt.Strategy):
    """
    Volume-momentum + ADX regime filter strategy (long and/or short).

    Contract with the rest of the framework (same as MomentumV8Strategy):
    - self.trade_log   : list of completed-trade dicts
    - self.final_value : portfolio value set in stop()
    """

    params = (
        # ── Volume filter ──────────────────────────────────────────────────
        ('vol_len',                  20),   # SMA period for average volume
        ('vol_multiplier',          1.7),   # volume must exceed multiplier × avg
        # ── Price-move filter ─────────────────────────────────────────────
        ('price_move_pct',          1.1),   # minimum candle body move (%, e.g. 1.1 = 1.1 %)
        # ── Body-quality filter (0.0 = off, 0.3 = remove weak candles) ───
        ('min_body_quality_long',   0.3),   # (close-open)/(high-low) threshold
        ('min_body_quality_short',  0.3),   # (open-close)/(high-low) threshold
        # ── ADX regime filter ─────────────────────────────────────────────
        ('adx_len',                  14),   # ADX smoothing period
        ('adx_threshold',           16.0),  # min ADX to consider market trending
        # ── Exit ──────────────────────────────────────────────────────────
        ('trail_activate_pct',      0.3),   # profit % needed before trail starts
        ('trail_distance_pct',      0.15),  # trail distance % behind best close
        ('stop_loss_pct',           0.2),   # hard stop loss distance (%)
        # ── EMA trend filter ──────────────────────────────────────────────
        ('use_ema_filter',         False),
        ('ema_len',                  30),
        # ── Direction control ─────────────────────────────────────────────
        ('allow_long',              True),
        ('allow_short',             True),
        # ── Intraday entry cutoff (US/Eastern time) ───────────────────────
        ('entry_cutoff_hour',        15),   # no entries at or after this NY hour …
        ('entry_cutoff_min',         30),   # … and minute
        ('printlog',               False),
    )

    # ------------------------------------------------------------------ init --

    def __init__(self):
        self.avg_vol = bt.indicators.SMA(self.data.volume, period=self.p.vol_len)
        self.ema     = bt.indicators.EMA(self.data.close,  period=self.p.ema_len)
        self.adx     = bt.indicators.AverageDirectionalMovementIndex(period=self.p.adx_len)

        self.order     = None
        self.trade_log = []

        # Per-trade state (reset on each new entry via notify_order)
        self._entry_price       = None
        self._entry_date        = None
        self._entry_size        = 0       # positive = long, negative = short
        self._exit_price        = None
        self._exit_date         = None
        self._hard_stop         = None    # fixed hard stop price
        self._trail_activated   = False   # True once price moved enough in our favour
        self._trail_best        = None    # best close seen after activation
        self._activation_amount = None   # $ the price must move to activate trail
        self._trail_distance    = None   # $ the trail trails behind the best close
        self._is_opening        = False  # True while the pending order is an entry

    # ---------------------------------------------------------------- helpers --

    def _bar_time_ny(self) -> tuple[int, int]:
        """Return (hour, minute) of the current bar in US/Eastern time."""
        dt = self.data.datetime.datetime(0)
        ts = pd.Timestamp(dt).tz_localize('UTC').tz_convert('America/New_York')
        return ts.hour, ts.minute

    def _entry_allowed(self) -> bool:
        h, m = self._bar_time_ny()
        return (h < self.p.entry_cutoff_hour or
                (h == self.p.entry_cutoff_hour and m < self.p.entry_cutoff_min))

    # -------------------------------------------------------- order callback --

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status == order.Completed:
            dt = self.data.datetime.date(0)
            px = order.executed.price
            sz = order.executed.size   # positive for buy, negative for sell
            cm = order.executed.comm

            if self._is_opening:
                self._entry_price       = px
                self._entry_date        = dt
                self._entry_size        = sz
                self._trail_best        = px
                self._trail_activated   = False
                self._activation_amount = px * self.p.trail_activate_pct / 100
                self._trail_distance    = px * self.p.trail_distance_pct  / 100
                self._hard_stop = (
                    px * (1 - self.p.stop_loss_pct / 100) if sz > 0
                    else px * (1 + self.p.stop_loss_pct / 100)
                )
            else:
                self._exit_price = px
                self._exit_date  = dt

            if self.p.printlog:
                side = 'BUY ' if order.isbuy() else 'SELL'
                print(f'{dt}  {side}  price={px:>10.4f}'
                      f'  size={abs(sz):>10.4f}  comm={cm:.2f}')

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            if self.p.printlog:
                print(f'Order {order.Status[order.status]}')

        self.order = None

    # -------------------------------------------------------- trade callback --

    def notify_trade(self, trade):
        if not trade.isclosed:
            return

        self.trade_log.append({
            'entry_date':  self._entry_date,
            'exit_date':   self._exit_date,
            'entry_price': trade.price,
            'exit_price':  self._exit_price,
            'size':        abs(self._entry_size),
            'direction':   'Long' if self._entry_size > 0 else 'Short',
            'gross_pnl':   trade.pnl,
            'net_pnl':     trade.pnlcomm,
        })

        if self.p.printlog:
            print(f'  -> CLOSED  gross={trade.pnl:>+10.2f}'
                  f'  net={trade.pnlcomm:>+10.2f}\n')

    # ------------------------------------------------------------- strategy --

    def next(self):
        if self.order:
            return

        close = self.data.close[0]
        open_ = self.data.open[0]
        high  = self.data.high[0]
        low   = self.data.low[0]

        if open_ == 0:
            return

        candle_range = high - low + 0.0001
        price_chg    = (close - open_) / open_ * 100

        high_volume        = self.data.volume[0] > self.p.vol_multiplier * self.avg_vol[0]
        long_body_quality  = (close - open_) / candle_range
        short_body_quality = (open_ - close) / candle_range

        long_filter  = not self.p.use_ema_filter or close > self.ema[0]
        short_filter = not self.p.use_ema_filter or close < self.ema[0]

        in_trending_regime = self.adx[0] > self.p.adx_threshold

        long_cond = (
            self.p.allow_long
            and high_volume
            and price_chg         >  self.p.price_move_pct
            and long_body_quality  > self.p.min_body_quality_long
            and long_filter
            and in_trending_regime
        )
        short_cond = (
            self.p.allow_short
            and high_volume
            and price_chg          < -self.p.price_move_pct
            and short_body_quality  > self.p.min_body_quality_short
            and short_filter
            and in_trending_regime
        )

        if not self.position:
            # ── entry ─────────────────────────────────────────────────────
            if not self._entry_allowed():
                return
            if long_cond:
                self._is_opening = True
                self.order = self.buy()
            elif short_cond:
                self._is_opening = True
                self.order = self.sell()

        else:
            # ── exits: trailing stop (with activation gate) + hard stop ──
            self._is_opening = False

            if self._entry_size > 0:   # long position
                # Activate trail once price moves enough in our favour
                if not self._trail_activated:
                    if close >= self._entry_price + self._activation_amount:
                        self._trail_activated = True
                        self._trail_best = close

                if self._trail_activated:
                    if close > self._trail_best:
                        self._trail_best = close
                    trail_stop = self._trail_best - self._trail_distance
                    stop = max(trail_stop, self._hard_stop)
                else:
                    stop = self._hard_stop

                if close <= stop:
                    self.order = self.close()

            else:   # short position
                if not self._trail_activated:
                    if close <= self._entry_price - self._activation_amount:
                        self._trail_activated = True
                        self._trail_best = close

                if self._trail_activated:
                    if close < self._trail_best:
                        self._trail_best = close
                    trail_stop = self._trail_best + self._trail_distance
                    stop = min(trail_stop, self._hard_stop)
                else:
                    stop = self._hard_stop

                if close >= stop:
                    self.order = self.close()

    # --------------------------------------------------------------- end -----

    def stop(self):
        self.final_value = self.broker.getvalue()
        if self.p.printlog:
            print(f'\nMomentumV11  →  final value = ${self.final_value:,.2f}')
