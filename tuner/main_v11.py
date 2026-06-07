"""
main_v11.py — BackTraderTuner entry point for MomentumV11Strategy.

Workflow
--------
1. Load OHLCV data from a TradingView CSV.
2. Run a grid-search optimisation over MomentumV11Strategy parameters.
3. Print the best parameter set and its performance metrics.
4. Re-run once with those best parameters (full detail: order log + trade list).
5. Plot a 3-D parameter surface (vol_multiplier × price_move_pct → profit).
6. Plot the strategy chart (candlestick + buy/sell arrows).

Key V11 additions vs V8
-----------------------
- ADX regime filter: fixed at adx_threshold=16.0 (not optimised).
- Body-quality filter: min_body_quality_long and min_body_quality_short scanned over the same values.
- Trail-activation gate: trail only starts after price moves trail_activate_pct %
  in favour (matches Pine's trail_points / trail_offset behaviour).
- Fixed-qty sizing: 10 shares per trade (matches Pine's default_qty_value=10).

Optimised parameters (6 axes)
------------------------------
  vol_multiplier    — how much above average volume must be
  price_move_pct    — minimum candle body size (%)
  min_body_quality  — doji / weak-candle filter (0 = off, 0.5 = strict); same value for long and short
  trail_activate_pct — profit % before trailing stop activates
  trail_distance_pct — how far (%) the trail sits behind the best close
  stop_loss_pct     — hard stop distance (%)
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import backtrader as bt
import backtrader.analyzers as btanalyzers
import matplotlib.pyplot as plt

from data_loaders import load_tradingview_csv
from strategies import MomentumV11Strategy
from plotting import print_trade_list, plot_parameter_surface, plot_candlestick_trades
from tuner_io import save_tuned_params

# ═══════════════════════════════════════════════ configuration ═══════════════

# DATA_FILE    = 'data/NASDAQ_RKLB, 10_4754f.csv'   # ← put your TradingView CSV here
DATA_FILE     = 'data/NASDAQ_RKLB, 30_c205c.csv'   # ← put your TradingView CSV here
SYMBOL        = 'RKLB'                              # ← symbol label for tuned_configs.py
TIMEFRAME_KEY = '30m'                               # ← timeframe label for tuned_configs.py
TIMEFRAME     = bt.TimeFrame.Minutes                # match your chart resolution
COMPRESSION   = 10                                  # bars per unit (10 = 10-min bars)
INITIAL_CASH = 10_000.0                           # starting capital in USD
COMMISSION   = 1.5                                # flat $1.50 per order (Pine default)
FIXED_STAKE  = 10                                 # shares per trade (Pine default_qty_value)

ADX_THRESHOLD = 16.0                              # fixed — not optimised

# Parameter grid for optimisation — MomentumV11Strategy
# 3 × 3 × 3 × 3 × 3 × 3 = 729 combinations
VOL_MULTIPLIER_VALS   = [1.2, 1.7, 2.3, 2.5]
PRICE_MOVE_PCT_VALS   = [0.3, 0.7, 1.1, 1.8, 2.8]
MIN_BODY_QUALITY_VALS = [0.0, 0.2, 0.3]       # 0.0 = filter off; same values for long and short
TRAIL_ACTIVATE_VALS   = [0.2, 0.5, 0.8]       # % profit to activate trail
TRAIL_DISTANCE_VALS   = [0.1, 0.15, 0.25]     # % trail sits behind best close
STOP_LOSS_PCT_VALS    = [0.1, 0.2, 0.4]

# ═══════════════════════════════════════════════ progress analyzer ════════════


class _OptProgress(bt.Analyzer):
    """Prints a progress bar to stdout after each optimisation run completes."""

    _count = [0]
    _total = [0]

    @classmethod
    def reset(cls, total: int) -> None:
        cls._count[0] = 0
        cls._total[0] = total

    def stop(self):
        _OptProgress._count[0] += 1
        n = _OptProgress._count[0]
        t = _OptProgress._total[0]
        filled = int(30 * n / t)
        bar = '█' * filled + '░' * (30 - filled)
        print(f'\r  [{bar}] {n:>5}/{t}  ({n / t * 100:5.1f} %)', end='', flush=True)
        if n == t:
            print()


# ═══════════════════════════════════════════════ commission class ═════════════


class FixedCommission(bt.CommInfoBase):
    """Flat dollar commission per order execution (independent of size/price)."""

    params = (
        ('commission', COMMISSION),
        ('stocklike',  True),
        ('commtype',   bt.CommInfoBase.COMM_FIXED),
    )

    def _getcommission(self, size, price, pseudoexec):
        return self.p.commission


# ═══════════════════════════════════════════════ cerebro factories ════════════


def _configure(cerebro: bt.Cerebro) -> None:
    """Apply settings shared by optimisation and single runs."""
    cerebro.broker.setcash(INITIAL_CASH)
    cerebro.broker.addcommissioninfo(FixedCommission())
    cerebro.addsizer(bt.sizers.FixedSize, stake=FIXED_STAKE)
    cerebro.addanalyzer(btanalyzers.SharpeRatio,   _name='sharpe',
                        riskfreerate=0.0, annualize=True)
    cerebro.addanalyzer(btanalyzers.DrawDown,       _name='drawdown')
    cerebro.addanalyzer(btanalyzers.TradeAnalyzer,  _name='trade_analyzer')
    cerebro.addanalyzer(btanalyzers.Returns,        _name='returns')


def make_opt_cerebro(data_feed: bt.feeds.PandasData) -> bt.Cerebro:
    cerebro = bt.Cerebro(optreturn=False)
    cerebro.adddata(data_feed)
    _configure(cerebro)
    cerebro.addanalyzer(_OptProgress)
    cerebro.optstrategy(
        MomentumV11Strategy,
        adx_threshold=[ADX_THRESHOLD],          # fixed; list wrapper required by optstrategy
        vol_multiplier=VOL_MULTIPLIER_VALS,
        price_move_pct=PRICE_MOVE_PCT_VALS,
        min_body_quality_long=MIN_BODY_QUALITY_VALS,
        min_body_quality_short=MIN_BODY_QUALITY_VALS,
        trail_activate_pct=TRAIL_ACTIVATE_VALS,
        trail_distance_pct=TRAIL_DISTANCE_VALS,
        stop_loss_pct=STOP_LOSS_PCT_VALS,
        printlog=False,
    )
    return cerebro


def make_single_cerebro(
    data_feed:          bt.feeds.PandasData,
    vol_multiplier:     float,
    price_move_pct:     float,
    min_body_quality:   float,   # applied to both long and short
    trail_activate_pct: float,
    trail_distance_pct: float,
    stop_loss_pct:      float,
) -> bt.Cerebro:
    cerebro = bt.Cerebro()
    cerebro.adddata(data_feed)
    _configure(cerebro)
    cerebro.addstrategy(
        MomentumV11Strategy,
        adx_threshold=ADX_THRESHOLD,
        vol_multiplier=vol_multiplier,
        price_move_pct=price_move_pct,
        min_body_quality_long=min_body_quality,
        min_body_quality_short=min_body_quality,
        trail_activate_pct=trail_activate_pct,
        trail_distance_pct=trail_distance_pct,
        stop_loss_pct=stop_loss_pct,
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
    # optstrategy generates the full cartesian product of long × short quality values;
    # we only use runs where they are equal, but all combinations are still executed.
    n = (len(VOL_MULTIPLIER_VALS) *
         len(PRICE_MOVE_PCT_VALS) *
         len(MIN_BODY_QUALITY_VALS) ** 2 *
         len(TRAIL_ACTIVATE_VALS) *
         len(TRAIL_DISTANCE_VALS) *
         len(STOP_LOSS_PCT_VALS))
    print(f'\n{"═"*62}')
    print(f'  OPTIMISATION  —  {n} parameter combinations')
    print(f'  ADX threshold fixed at {ADX_THRESHOLD}')
    print(f'{"═"*62}')

    opt_cerebro = make_opt_cerebro(data_opt)
    _OptProgress.reset(n)
    opt_results = opt_cerebro.run(maxcpus=1)
    print(f'\nOptimisation complete: {len(opt_results)} runs.\n')

    # ── 3. Find best result ───────────────────────────────────────────────────
    best_value = -float('inf')
    best_strat = None

    for run in opt_results:
        strat = run[0]
        # Only consider runs where both quality params were set to the same value
        if strat.params.min_body_quality_long != strat.params.min_body_quality_short:
            continue
        if strat.final_value > best_value:
            best_value = strat.final_value
            best_strat = strat

    bp = best_strat.params
    print(f'\n{"═"*62}')
    print('  BEST PARAMETERS FOUND')
    print(f'{"═"*62}')
    print(f'  Vol Multiplier    : {bp.vol_multiplier}')
    print(f'  Price Move %      : {bp.price_move_pct}')
    print(f'  Min Body Quality  : {bp.min_body_quality_long}  (long = short)')
    print(f'  Trail Activate %  : {bp.trail_activate_pct}')
    print(f'  Trail Distance %  : {bp.trail_distance_pct}')
    print(f'  Stop Loss %       : {bp.stop_loss_pct}')
    print(f'  ADX Threshold     : {bp.adx_threshold}  (fixed)')

    print_performance(best_strat, 'BEST PARAMETERS — OPTIMISATION RESULT')

    # ── 3b. Persist best parameters ───────────────────────────────────────────
    save_tuned_params(
        strategy_name='MomentumV11Strategy',
        symbol=SYMBOL,
        timeframe=TIMEFRAME_KEY,
        params={
            'vol_multiplier':       bp.vol_multiplier,
            'price_move_pct':       bp.price_move_pct,
            'adx_threshold':        bp.adx_threshold,
            'min_body_quality_long':  bp.min_body_quality_long,
            'min_body_quality_short': bp.min_body_quality_short,
            'trail_activate_pct':   bp.trail_activate_pct,
            'trail_distance_pct':   bp.trail_distance_pct,
            'stop_loss_pct':        bp.stop_loss_pct,
        },
    )

    # ── 4. Detailed single run with best parameters ───────────────────────────
    print('─' * 62)
    print('  Re-running with best parameters (full order + trade log)')
    print('─' * 62 + '\n')

    data_best    = load_tradingview_csv(DATA_FILE, timeframe=TIMEFRAME, compression=COMPRESSION)
    best_cerebro = make_single_cerebro(
        data_best,
        vol_multiplier=bp.vol_multiplier,
        price_move_pct=bp.price_move_pct,
        min_body_quality=bp.min_body_quality_long,
        trail_activate_pct=bp.trail_activate_pct,
        trail_distance_pct=bp.trail_distance_pct,
        stop_loss_pct=bp.stop_loss_pct,
    )
    best_results   = best_cerebro.run()
    best_run_strat = best_results[0]

    print_performance(best_run_strat, 'BEST PARAMETERS — DETAILED RUN')

    # ── 5. Trade list ─────────────────────────────────────────────────────────
    print_trade_list(best_run_strat)

    # ── 6. 3-D parameter surface (vol_multiplier × price_move_pct) ───────────
    plt.ion()
    print('Generating parameter surface plot (vol_multiplier × price_move_pct) …')
    plot_parameter_surface(
        opt_results,
        param1_name='vol_multiplier',
        param2_name='price_move_pct',
        initial_cash=INITIAL_CASH,
        save_path='parameter_surface_v11.png',
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
            f'V11 Best Run — vol_mult={bp.vol_multiplier}  '
            f'price_move={bp.price_move_pct}%  '
            f'body_q={bp.min_body_quality_long}  '
            f'trail={bp.trail_activate_pct}/{bp.trail_distance_pct}%  '
            f'stop={bp.stop_loss_pct}%'
        ),
        save_path='candlestick_trades_v11.png',
    )

    input('\nAll charts open — press Enter to exit …')
    plt.close('all')


if __name__ == '__main__':
    main()
