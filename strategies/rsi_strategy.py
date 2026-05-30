import backtrader as bt


class RSIStrategy(bt.Strategy):
    """
    RSI mean-reversion strategy (long-only).

    Entry : buy when RSI falls below `oversold`.
    Exit  : sell when RSI rises above `overbought`.
    """

    params = (
        ('rsi_period',  14),
        ('oversold',    30),
        ('overbought',  70),
        ('printlog', False),
    )

    # ------------------------------------------------------------------ init --

    def __init__(self):
        self.rsi = bt.indicators.RSI(
            self.data.close,
            period=self.p.rsi_period,
            safediv=True,
        )
        self.order = None
        self.trade_log = []

        # Scratch space populated in notify_order; consumed in notify_trade
        self._entry_size  = 0
        self._entry_date  = None
        self._exit_price  = None
        self._exit_date   = None

    # -------------------------------------------------------- order callback --

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
                    print(f'{dt}  BUY   price={px:>10.4f}  '
                          f'size={sz:>10.4f}  comm={cm:.2f}')
            else:
                self._exit_price = px
                self._exit_date  = dt
                if self.p.printlog:
                    print(f'{dt}  SELL  price={px:>10.4f}  '
                          f'size={sz:>10.4f}  comm={cm:.2f}')

        elif order.status in [order.Canceled, order.Margin, order.Rejected]:
            if self.p.printlog:
                print(f'Order {order.Status[order.status]}')

        self.order = None

    # -------------------------------------------------------- trade callback --

    def notify_trade(self, trade):
        if not trade.isclosed:
            return

        record = {
            'entry_date':  self._entry_date,
            'exit_date':   self._exit_date,
            'entry_price': trade.price,         # BT preserves this after close
            'exit_price':  self._exit_price,
            'size':        abs(self._entry_size),
            'direction':   'Long' if self._entry_size >= 0 else 'Short',
            'gross_pnl':   trade.pnl,
            'net_pnl':     trade.pnlcomm,
        }
        self.trade_log.append(record)

        if self.p.printlog:
            print(f'  -> CLOSED  '
                  f'gross={trade.pnl:>+10.2f}  '
                  f'net={trade.pnlcomm:>+10.2f}\n')

    # -------------------------------------------------------------- strategy --

    def next(self):
        if self.order:
            return

        if not self.position:
            if self.rsi < self.p.oversold:
                self.order = self.buy()
        else:
            if self.rsi > self.p.overbought:
                self.order = self.sell()

    # --------------------------------------------------------- end of series --

    def stop(self):
        # Stored here so optimization runs can access it without broker reference
        self.final_value = self.broker.getvalue()
        if self.p.printlog:
            print(f'\nRSI({self.p.rsi_period})  '
                  f'oversold={self.p.oversold}  '
                  f'overbought={self.p.overbought}  '
                  f'→  final value = ${self.final_value:,.2f}')
