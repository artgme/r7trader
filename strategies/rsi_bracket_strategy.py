import backtrader as bt


class RSIBracketStrategy(bt.Strategy):
    """
    RSI mean-reversion strategy with IBKR bracket exit support.

    Entry : buy when RSI falls below `oversold`.
    Exit  : sell when RSI rises above `overbought`  (backtrader simulation).
            In live IBKR trading mozg replaces the entry order with a bracket
            order — trailing stop + optional take profit — so the real exit is
            handled server-side by IBKR without needing the RSI sell signal.

    Bracket params (declared here so BT accepts them from configs; the actual
    bracket logic lives in mozg._ibkr_data_worker):

        trail_stop_pct    — trailing stop distance in %.
                            Required for bracket ordering; omit to fall back
                            to a plain market order.
        limit_price       — fixed limit price for the entry order.
                            None (default) → market order.
        take_profit_price — fixed absolute price for the take-profit leg.
                            None (default) → trailing stop only, no TP.
    """

    params = (
        ('rsi_period',        14),
        ('oversold',          30),
        ('overbought',        70),
        ('trail_stop_pct',    None),   # % trailing stop — used by mozg for bracket
        ('limit_price',       None),   # absolute entry limit — used by mozg for bracket
        ('take_profit_price', None),   # absolute TP price   — used by mozg for bracket
        ('printlog',          False),
    )

    def __init__(self):
        self.rsi = bt.indicators.RSI(
            self.data.close,
            period=self.p.rsi_period,
            safediv=True,
        )
        self.order = None
        self.trade_log = []

        self._entry_size = 0
        self._entry_date = None
        self._exit_price = None
        self._exit_date  = None

    def notify_order(self, order):
        if order.status in [order.Submitted, order.Accepted]:
            return

        if order.status == order.Completed:
            dt = self.data.datetime.date(0)
            px = order.executed.price
            sz = order.executed.size
            cm = order.executed.comm

            if order.isbuy():
                self._entry_size = sz
                self._entry_date = dt
                if self.p.printlog:
                    print(f'{dt}  BUY   price={px:>10.4f}  size={sz:>10.4f}  comm={cm:.2f}')
            else:
                self._exit_price = px
                self._exit_date  = dt
                if self.p.printlog:
                    print(f'{dt}  SELL  price={px:>10.4f}  size={sz:>10.4f}  comm={cm:.2f}')

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            if self.p.printlog:
                print(f'Order {order.Status[order.status]}')

        self.order = None

    def notify_trade(self, trade):
        if not trade.isclosed:
            return

        record = {
            'entry_date':  self._entry_date,
            'exit_date':   self._exit_date,
            'entry_price': trade.price,
            'exit_price':  self._exit_price,
            'size':        abs(self._entry_size),
            'direction':   'Long' if self._entry_size >= 0 else 'Short',
            'gross_pnl':   trade.pnl,
            'net_pnl':     trade.pnlcomm,
        }
        self.trade_log.append(record)

        if self.p.printlog:
            print(f'  -> CLOSED  gross={trade.pnl:>+10.2f}  net={trade.pnlcomm:>+10.2f}\n')

    def next(self):
        if self.order:
            return

        if not self.position:
            if self.rsi < self.p.oversold:
                self.order = self.buy()
        else:
            if self.rsi > self.p.overbought:
                self.order = self.sell()

    def stop(self):
        self.final_value = self.broker.getvalue()
        if self.p.printlog:
            trail = f'{self.p.trail_stop_pct}%' if self.p.trail_stop_pct is not None else 'none'
            tp    = self.p.take_profit_price if self.p.take_profit_price is not None else 'none'
            lmt   = self.p.limit_price       if self.p.limit_price       is not None else 'market'
            print(
                f'\nRSIBracket({self.p.rsi_period})  '
                f'oversold={self.p.oversold}  overbought={self.p.overbought}  '
                f'trail={trail}  tp={tp}  entry={lmt}  '
                f'→  final value = ${self.final_value:,.2f}'
            )
