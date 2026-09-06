"""Offline analysis of tuning-grid results saved by tuner1.py / tuner1_parallel.py.

The tuners write one CSV row per grid combo to tuning_logs/ (see logging_functions.log_tuning_csv).
This module reloads that CSV and plots/summarizes it without re-running any backtests, so a
finished sweep can be explored in a later session. Point CSV_PATH at the log you want to look at.
"""

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from logging_functions import load_tuning_log

CSV_PATH = Path('tuning_logs/tuning_20260905_1747_10m_parallel.csv')

# Performance metrics a saved tuning log carries (see logging_functions._TUNING_SCORE_COLS) —
# any of these is a valid z_metric / y_metric. 'combined_score' is tuner1's headline ranking blend.
METRICS = ('total_pnl', 'win_rate', 'expectancy', 'max_drawdown', 'profit_factor',
           'sharpe', 'sortino', 'recovery_factor', 'combined_score')
PARAMS = ('vol_len', 'vol_multiplier', 'price_move_pct', 'trail_stop_pct',
          'body_ratio_threshold', 'take_profit_pct')
LOWER_IS_BETTER = ('max_drawdown',)  # for these, "best" means the smallest value


# Usage: plot_3d(CSV_PATH, 'VOYG', 'vol_multiplier', 'trail_stop_pct', 'expectancy')
def plot_3d(csv_path: Path, ticker: str, param_x: str, param_y: str, z_metric: str) -> None:
    """3D surface of 2 tuned parameters (x, y) against a chosen performance metric (z_metric, any
    of METRICS), read straight from a saved tuning-log CSV. Each grid point is the best z_metric
    found across all values of the other tuned parameters for that (x, y) combination. Builds the
    figure but doesn't show it — call plt.show() once after plotting everything so none block."""
    if z_metric not in METRICS:
        raise ValueError(f"z_metric must be one of {METRICS}")

    results_by_ticker = load_tuning_log(csv_path)
    if ticker not in results_by_ticker:
        raise ValueError(f"{ticker} not in {csv_path} — has {sorted(results_by_ticker)}")
    results = results_by_ticker[ticker]
    pick = min if z_metric in LOWER_IS_BETTER else max

    best: dict[tuple, float] = {}
    for r in results:
        v = r[z_metric]
        if not np.isfinite(v):  # inf profit_factor / recovery_factor, or NaN from an older log
            continue
        key = (r[param_x], r[param_y])
        best[key] = v if key not in best else pick(best[key], v)

    if not best:
        raise ValueError(f"no finite {z_metric} rows for {ticker} — does this log predate the {z_metric} column?")
    xs = sorted({x for x, y in best})
    ys = sorted({y for x, y in best})
    X, Y = np.meshgrid(xs, ys)
    Z = np.array([[best.get((x, y), np.nan) for x in xs] for y in ys])  # nan where a cell had no finite row

    fig = plt.figure(figsize=(9, 7))
    ax = fig.add_subplot(projection='3d')
    surf = ax.plot_surface(X, Y, Z, cmap='RdYlGn')
    ax.set_xlabel(param_x)
    ax.set_ylabel(param_y)
    ax.set_zlabel(z_metric)
    ax.set_title(f'{ticker}: best {z_metric} by {param_x} / {param_y}')
    fig.colorbar(surf, label=z_metric)


# Usage: plot_2d(CSV_PATH, 'VOYG', 'vol_multiplier', 'expectancy', fit_deg=2)
def plot_2d(csv_path: Path, ticker: str, param_x: str, y_metric: str, fit_deg: int = 2) -> None:
    """2D scatter of one tuned parameter (param_x, one of PARAMS) against one performance metric
    (y_metric, one of METRICS), read straight from a saved tuning-log CSV. Every grid combo is one
    dot (the other params vary along each column of dots), with the per-x mean and best lines
    overlaid and, if fit_deg > 0, a degree-fit_deg polynomial least-squares curve through the raw
    cloud. Builds the figure but doesn't show it — call plt.show() once after plotting everything."""
    if y_metric not in METRICS:
        raise ValueError(f"y_metric must be one of {METRICS}")

    results_by_ticker = load_tuning_log(csv_path)
    if ticker not in results_by_ticker:
        raise ValueError(f"{ticker} not in {csv_path} — has {sorted(results_by_ticker)}")

    rows = results_by_ticker[ticker]
    x_all = np.array([r[param_x] for r in rows], dtype=float)
    y_all = np.array([r[y_metric] for r in rows], dtype=float)
    finite = np.isfinite(x_all) & np.isfinite(y_all)  # drop inf (profit_factor / recovery_factor) and NaN
    x_all, y_all = x_all[finite], y_all[finite]
    if x_all.size == 0:
        raise ValueError(f"no finite ({param_x}, {y_metric}) rows for {ticker} — does this log predate the {y_metric} column?")
    pick = min if y_metric in LOWER_IS_BETTER else max

    grouped: dict[float, list[float]] = {}
    for xv, yv in zip(x_all, y_all):
        grouped.setdefault(xv, []).append(yv)
    xs = sorted(grouped)
    means = [float(np.mean(grouped[x])) for x in xs]
    bests = [pick(grouped[x]) for x in xs]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(x_all, y_all, s=6, alpha=0.15, color='steelblue', label=f'all combos (n={len(x_all)})')
    ax.plot(xs, means, marker='o', color='black', label='mean per x')
    ax.plot(xs, bests, marker='^', color='green', label='best per x')
    if fit_deg > 0:
        coefs = np.polyfit(x_all, y_all, fit_deg)
        grid = np.linspace(x_all.min(), x_all.max(), 200)
        ax.plot(grid, np.polyval(coefs, grid), '--', color='crimson', label=f'poly fit (deg {fit_deg})')
    ax.set_xlabel(param_x)
    ax.set_ylabel(y_metric)
    ax.set_title(f'{ticker}: {y_metric} by {param_x}')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)


def main():
    results_by_ticker = load_tuning_log(CSV_PATH)
    for ticker in results_by_ticker:
        plot_3d(CSV_PATH, ticker, 'vol_multiplier', 'trail_stop_pct', 'expectancy')
        plot_2d(CSV_PATH, ticker, 'vol_multiplier', 'total_pnl')
    plt.show()  # blocks once, here, after every figure has been built


if __name__ == '__main__':
    main()
