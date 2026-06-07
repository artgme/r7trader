"""
main.py — BackTraderTuner entry point.

Workflow
--------
1. Load OHLCV data from a TradingView CSV.
2. Run a grid-search optimisation over MomentumV8 strategy parameters.
3. Print the best parameter set and its performance metrics.
4. Re-run once with those best parameters (full detail: order log + trade list).
5. Plot a 3-D parameter surface (vol_multiplier × price_move_pct → profit).
6. Plot the strategy chart (candlestick + buy/sell arrows).
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import backtrader as bt
import backtrader.analyzers as btanalyzers
import matplotlib.pyplot as plt

from data_loaders import load_tradingview_csv
from strategies import MomentumV8Strategy
from plotting import print_trade_list, plot_parameter_surface, plot_candlestick_trades
from tuner_io import save_tuned_params

# ═══════════════════════════════════════════════ configuration ═══════════════

DATA_FILE     = 'data/NASDAQ_RKLB, 10_4754f.csv'   # ← put your TradingView CSV here
SYMBOL        = 'RKLB'                              # ← symbol label for tuned_configs.py
TIMEFRAME_KEY = '10m'                               # ← timeframe label for tuned_configs.py
TIMEFRAME     = bt.TimeFrame.Minutes                # match your chart resolution
COMPRESSION   = 10                                  # bars per unit (10 = 10-min bars)
INITIAL_CASH = 10_000.0                   # starting capital in USD
COMMISSION   = 1.0                        # flat $1 per order execution

# Parameter grid for optimisation — MomentumV8Strategy
VOL_LEN_VALS        = range(5, 20, 5)               # [5, 10, 15]
VOL_MULTIPLIER_VALS = [0.6, 0.8, 1.0, 1.1, 1.2, 1.5, 1.8, 2.0, 2.1, 2.3]    # 5 values
PRICE_MOVE_PCT_VALS = [0.01, 0.05, 0.08, 0.10, 0.15, 0.20, 0.4, 0.6, 1.2]  # 5 values

# ═══════════════════════════════════════════════ commission class ═════════════


class FixedCommission(bt.CommInfoBase):
    """Flat dollar commission per order execution (independent of size/price)."""

    params = (
        ('commission', COMMISSION),
        ('stocklike',  True),
        ('commtype',   bt.CommInfoBase.COMM_FIXED),
    )

    def _getcommission(self, size, price, pseudoexec):
        return self.p.commission   # always $1, regardless of size or price


# ═══════════════════════════════════════════════ cerebro factories ════════════


def _configure(cerebro: bt.Cerebro) -> None:
    """Apply settings shared by optimisation and single runs."""
    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.broker.addcommissioninfo(FixedCommission())
    # Invest 95 % of available cash per trade (leaves buffer for commission)
    cerebro.addsizer(bt.sizers.PercentSizer, percents=95)
    cerebro.addanalyzer(btanalyzers.SharpeRatio,   _name='sharpe',
                        riskfreerate=0.0, annualize=True)
    cerebro.addanalyzer(btanalyzers.DrawDown,       _name='drawdown')
    cerebro.addanalyzer(btanalyzers.TradeAnalyzer,  _name='trade_analyzer')
    cerebro.addanalyzer(btanalyzers.Returns,        _name='returns')


def make_opt_cerebro(data_feed: bt.feeds.PandasData) -> bt.Cerebro:
    # optreturn=False → full strategy instances returned (needed for final_value
    # and analyzer access); works with maxcpus=1. For maxcpus=0 (multi-process)
    # you may need optreturn=True and a custom ValueCapture analyzer instead.
    cerebro = bt.Cerebro(optreturn=False)
    cerebro.adddata(data_feed)
    _configure(cerebro)
    cerebro.optstrategy(
        MomentumV8Strategy,
        vol_len=VOL_LEN_VALS,
        vol_multiplier=VOL_MULTIPLIER_VALS,
        price_move_pct=PRICE_MOVE_PCT_VALS,
        printlog=False,
    )
    return cerebro


def make_single_cerebro(
    data_feed:      bt.feeds.PandasData,
    vol_len:        int,
    vol_multiplier: float,
    price_move_pct: float,
) -> bt.Cerebro:
    cerebro = bt.Cerebro()
    cerebro.adddata(data_feed)
    _configure(cerebro)
    cerebro.addstrategy(
        MomentumV8Strategy,
        vol_len=vol_len,
        vol_multiplier=vol_multiplier,
        price_move_pct=price_move_pct,
        printlog=True,
    )
    return cerebro


# ═══════════════════════════════════════════════ result helpers ═══════════════


def print_performance(strat, label: str = 'PERFORMANCE') -> None:
    """Print a summary panel for a completed strategy run."""
    final_value = strat.final_value
    profit      = final_value - INITIAL_CASH
    ret_pct     = profit / INITIAL_CASH * 100

    print(f'\n{"═"*62}')
    print(f'  {label}')
    print(f'{"═"*62}')
    print(f'  Initial Cash  : ${INITIAL_CASH:>12,.2f}')
    print(f'  Final Value   : ${final_value:>12,.2f}')
    print(f'  Net Profit    : ${profit:>+12,.2f}  ({ret_pct:>+.1f} %)')

    sharpe = strat.analyzers.sharpe.get_analysis().get('sharperatio')
    print(f'  Sharpe Ratio  : {sharpe:>12.3f}' if sharpe else
          f'  Sharpe Ratio  : {"N/A":>12}')

    dd = strat.analyzers.drawdown.get_analysis()
    print(f'  Max Drawdown  : {dd.max.drawdown:>11.2f} %')

    ta    = strat.analyzers.trade_analyzer.get_analysis()
    total = ta.get('total',  {}).get('total', 0)
    won   = ta.get('won',    {}).get('total', 0)
    lost  = ta.get('lost',   {}).get('total', 0)
    wr    = won / total * 100 if total else 0.0
    print(f'  Total Trades  : {total:>12}')
    print(f'  Won / Lost    : {won:>6} / {lost}')
    print(f'  Win Rate      : {wr:>11.1f} %')
    print(f'{"═"*62}\n')


# ═══════════════════════════════════════════════ main ════════════════════════


def main() -> None:

    # ── 1. Load data ──────────────────────────────────────────────────────────
    print(f'\nLoading data: {DATA_FILE}')
    data_opt = load_tradingview_csv(DATA_FILE, timeframe=TIMEFRAME, compression=COMPRESSION)

    # ── 2. Optimisation ───────────────────────────────────────────────────────
    n = (len(list(VOL_LEN_VALS)) *
         len(list(VOL_MULTIPLIER_VALS)) *
         len(list(PRICE_MOVE_PCT_VALS)))
    print(f'\n{"═"*62}')
    print(f'  OPTIMISATION  —  {n} parameter combinations')
    print(f'{"═"*62}')

    opt_cerebro  = make_opt_cerebro(data_opt)
    opt_results  = opt_cerebro.run(maxcpus=1)
    print(f'\nOptimisation complete: {len(opt_results)} runs.\n')

    # ── 3. Find best result ───────────────────────────────────────────────────
    best_value = -float('inf')
    best_strat = None

    for run in opt_results:
        strat = run[0]
        if strat.final_value > best_value:
            best_value = strat.final_value
            best_strat = strat

    bp = best_strat.params
    print(f'\n{"═"*62}')
    print('  BEST PARAMETERS FOUND')
    print(f'{"═"*62}')
    print(f'  Vol Len        : {bp.vol_len}')
    print(f'  Vol Multiplier : {bp.vol_multiplier}')
    print(f'  Price Move %   : {bp.price_move_pct}')

    print_performance(best_strat, 'BEST PARAMETERS — OPTIMISATION RESULT')

    # ── 3b. Persist best parameters ───────────────────────────────────────────
    save_tuned_params(
        strategy_name='MomentumV8Strategy',
        symbol=SYMBOL,
        timeframe=TIMEFRAME_KEY,
        params={
            'vol_len':        bp.vol_len,
            'vol_multiplier': bp.vol_multiplier,
            'price_move_pct': bp.price_move_pct,
        },
    )

    # ── 4. Detailed single run with best parameters ───────────────────────────
    print('─' * 62)
    print('  Re-running with best parameters (full order + trade log)')
    print('─' * 62 + '\n')

    data_best    = load_tradingview_csv(DATA_FILE, timeframe=TIMEFRAME, compression=COMPRESSION)
    best_cerebro = make_single_cerebro(
        data_best,
        vol_len=bp.vol_len,
        vol_multiplier=bp.vol_multiplier,
        price_move_pct=bp.price_move_pct,
    )
    best_results   = best_cerebro.run()
    best_run_strat = best_results[0]

    print_performance(best_run_strat, 'BEST PARAMETERS — DETAILED RUN')

    # ── 5. Trade list ─────────────────────────────────────────────────────────
    print_trade_list(best_run_strat)

    # ── 6. 3-D parameter surface (vol_multiplier × price_move_pct) ───────────
    plt.ion()   # make all plt.show() calls non-blocking (incl. backtrader's)
    print('Generating parameter surface plot (vol_multiplier × price_move_pct) …')
    plot_parameter_surface(
        opt_results,
        param1_name='vol_multiplier',
        param2_name='price_move_pct',
        initial_cash=INITIAL_CASH,
    )

    # ── 7. Strategy chart ─────────────────────────────────────────────────────
    print('Generating strategy chart …')
    best_cerebro.plot(
        style='candlestick',
        barup='#26a69a',
        bardown='#ef5350',
        volume=True,
        plotdist=0.1,
    )

    # ── 8. Candlestick + individual trades ───────────────────────────────────
    print('Generating candlestick + trades chart …')
    plot_candlestick_trades(
        best_run_strat,
        title=(
            f'Best Run — vol_len={bp.vol_len}  '
            f'vol_mult={bp.vol_multiplier}  '
            f'price_move={bp.price_move_pct}%'
        ),
        save_path='candlestick_trades.png',
    )

    input('\nAll charts open — press Enter to exit …')
    plt.close('all')


if __name__ == '__main__':
    main()
