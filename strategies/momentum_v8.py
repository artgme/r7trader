"""
strategies/momentum_v8.py — MomentumV8Strategy

High-volume candle momentum strategy with trailing stop and hard stop loss.
Ported from Pine Script "VR8".

Logic
-----
Entry long  : bullish candle (close > open by >= price_move_pct %)
              AND volume > vol_multiplier × SMA(volume, vol_len)
              AND (optional) close > EMA(close, ema_len)

Entry short : bearish candle (close < open by >= price_move_pct %)
              AND same volume condition
              AND (optional) close < EMA(close, ema_len)

Exit        : whichever triggers first —
                • trailing stop  : trail_best ± entry_price * trail_stop_pct / 100
                • hard stop loss : entry_price ± entry_price * stop_loss_pct  / 100

Entry gate  : no new entries at or after entry_cutoff_hour:entry_cutoff_min
              in US/Eastern (New York) time.

Notes
-----
- Only one trade open at a time (entry requires flat position).
- Direction control: allow_long / allow_short let you trade one or both sides.
- Trail distance is fixed in dollars at entry (= entry_price * trail_stop_pct %),
  matching the Pine Script trail_offset / trail_points behaviour.
"""

import backtrader as bt
import pandas as pd


class MomentumV8Strategy(bt.Strategy):
    """
    Volume-momentum strategy, long and/or short.

    Contract with the rest of the framework (same as RSIStrategy):
    - self.trade_log   : list of completed-trade dicts
    - self.final_value : portfolio value set in stop()
    """

    params = (
        # ── Volume filter ──────────────────────────────────────────────────
        ('vol_len',           10),    # SMA period for average volume
        ('vol_multiplier',   1.1),    # volume must exceed multiplier × avg
        # ── Price-move filter ──────────────────────────────────────────────
        ('price_move_pct',   0.1),    # minimum candle body move (%)
        # ── Exit ──────────────────────────────────────────────────────────
        ('trail_stop_pct',   0.1),    # trailing stop distance (% of entry price)
        ('stop_loss_pct',    0.2),    # hard stop loss distance (% of entry price)
        # ── EMA trend filter ──────────────────────────────────────────────
        ('use_ema_filter', False),
        ('ema_len',           50),
        # ── Direction control ─────────────────────────────────────────────
        ('allow_long',      True),
        ('allow_short',     True),
        # ── Intraday entry cutoff (US/Eastern time) ───────────────────────
        ('entry_cutoff_hour', 15),    # no entries at or after this NY hour …
        ('entry_cutoff_min',  30),    # … and minute
        ('printlog',       False),
    )

    # ------------------------------------------------------------------ init --

    def __init__(self):
        self.avg_vol = bt.indicators.SMA(self.data.volume, period=self.p.vol_len)
        self.ema     = bt.indicators.EMA(self.data.close,  period=self.p.ema_len)

        self.order     = None
        self.trade_log = []

        # Per-trade tracking (reset on each new entry)
        self._entry_price  = None
        self._entry_date   = None
        self._entry_size   = 0       # positive = long, negative = short
        self._exit_price   = None
        self._exit_date    = None
        self._trail_best   = None    # highest close (long) / lowest close (short)
        self._trail_amount = None    # fixed trail distance in $ (set at entry)
        self._is_opening   = False   # True while the pending order is an entry

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
                self._entry_price  = px
                self._entry_date   = dt
                self._entry_size   = sz
                self._trail_best   = px
                self._trail_amount = px * self.p.trail_stop_pct / 100
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

        if open_ == 0:
            return

        high_volume = self.data.volume[0] > self.p.vol_multiplier * self.avg_vol[0]
        price_chg   = (close - open_) / open_ * 100

        long_filter  = not self.p.use_ema_filter or close > self.ema[0]
        short_filter = not self.p.use_ema_filter or close < self.ema[0]

        long_cond  = (self.p.allow_long  and high_volume
                      and price_chg >  self.p.price_move_pct and long_filter)
        short_cond = (self.p.allow_short and high_volume
                      and price_chg < -self.p.price_move_pct and short_filter)

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
            # ── exits: trailing stop or hard stop ─────────────────────────
            self._is_opening = False
            if self._entry_size > 0:   # long position
                if close > self._trail_best:
                    self._trail_best = close
                trail_stop = self._trail_best - self._trail_amount
                hard_stop  = self._entry_price * (1 - self.p.stop_loss_pct / 100)
                if close <= max(trail_stop, hard_stop):
                    self.order = self.close()
            else:                       # short position
                if close < self._trail_best:
                    self._trail_best = close
                trail_stop = self._trail_best + self._trail_amount
                hard_stop  = self._entry_price * (1 + self.p.stop_loss_pct / 100)
                if close >= min(trail_stop, hard_stop):
                    self.order = self.close()

    # --------------------------------------------------------------- end -----

    def stop(self):
        self.final_value = self.broker.getvalue()
        if self.p.printlog:
            print(f'\nMomentumV8  →  final value = ${self.final_value:,.2f}')
