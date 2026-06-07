"""
plotting.py — visualisation helpers for BackTraderTuner.

Public API
----------
print_trade_list(strategy)
    Pretty-print every completed trade (entry/exit date, price, direction, PnL).

plot_parameter_surface(results, param1_name, param2_name, initial_cash)
    3-D surface: two strategy parameters on X/Y axes, final portfolio value on Z.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 – registers 3-D projection


# ────────────────────────────────────────────────────────── trade table ──────

def print_trade_list(strategy) -> None:
    """Print a formatted table of every completed trade recorded by the strategy."""
    trades = strategy.trade_log
    if not trades:
        print('\nNo completed trades.\n')
        return

    # Column format string
    fmt = (
        '{:>4}  {:<12}  {:<12}  {:<5}  {:>11}  {:>11}'
        '  {:>10}  {:>10}  {:>10}'
    )
    header = fmt.format(
        '#', 'Entry Date', 'Exit Date', 'Dir',
        'Entry $', 'Exit $', 'Size', 'Gross P&L', 'Net P&L',
    )
    sep = '─' * len(header)

    print(f'\n{sep}')
    print('TRADE LOG')
    print(sep)
    print(header)
    print(sep)

    total_gross = total_net = 0.0
    for i, t in enumerate(trades, 1):
        entry_dt = str(t['entry_date']) if t['entry_date'] else 'N/A'
        exit_dt  = str(t['exit_date'])  if t['exit_date']  else 'N/A'
        exit_px  = t['exit_price']      if t['exit_price'] is not None else float('nan')
        pnl_sign = '+' if t['gross_pnl'] >= 0 else ''
        net_sign = '+' if t['net_pnl']   >= 0 else ''

        print(fmt.format(
            i, entry_dt, exit_dt, t['direction'],
            f"{t['entry_price']:.4f}",
            f"{exit_px:.4f}",
            f"{t['size']:.4f}",
            f"{pnl_sign}{t['gross_pnl']:.2f}",
            f"{net_sign}{t['net_pnl']:.2f}",
        ))
        total_gross += t['gross_pnl']
        total_net   += t['net_pnl']

    print(sep)
    print(fmt.format(
        'SUM', '', '', '', '', '', '',
        f"{'+' if total_gross >= 0 else ''}{total_gross:.2f}",
        f"{'+' if total_net   >= 0 else ''}{total_net:.2f}",
    ))
    print(f'{sep}\n')


# ──────────────────────────────────────────────────── parameter surface ──────

def plot_parameter_surface(
    results,
    param1_name: str,
    param2_name: str,
    initial_cash: float = 10_000.0,
    save_path: str = 'parameter_surface.png',
) -> None:
    """
    3-D surface plot: final portfolio value as a function of two parameters.

    For combinations that also vary a third parameter the *maximum* final value
    across those extra combinations is projected onto the surface (best-case
    per cell).

    Args:
        results      : raw list-of-lists from cerebro.run(optreturn=False)
        param1_name  : strategy parameter name → x-axis
        param2_name  : strategy parameter name → y-axis
        initial_cash : used for the profit colour-bar annotation
        save_path    : PNG output path (also displayed interactively)
    """
    # Collect data points
    points: list[tuple] = []
    for run in results:
        strat = run[0]
        p1  = getattr(strat.params, param1_name)
        p2  = getattr(strat.params, param2_name)
        val = strat.final_value
        points.append((p1, p2, val))

    if not points:
        print('plot_parameter_surface: no results to plot.')
        return

    p1_vals = sorted(set(p[0] for p in points))
    p2_vals = sorted(set(p[1] for p in points))

    # Per cell: keep the maximum final value (best of the remaining parameters)
    grid: dict[tuple, float] = {}
    for p1, p2, val in points:
        key = (p1, p2)
        if key not in grid or val > grid[key]:
            grid[key] = val

    X, Y = np.meshgrid(p1_vals, p2_vals)
    Z = np.array(
        [[grid.get((p1, p2), np.nan) for p1 in p1_vals]
         for p2 in p2_vals]
    )

    # ── figure ────────────────────────────────────────────────────────────────
    fig = plt.figure(figsize=(14, 9))
    ax: Axes3D = fig.add_subplot(111, projection='3d')

    surf = ax.plot_surface(
        X, Y, Z,
        cmap='RdYlGn',
        alpha=0.80,
        edgecolor='none',
        antialiased=True,
    )

    # Scatter the individual data points for readability
    ax.scatter(
        [p[0] for p in points],
        [p[1] for p in points],
        [p[2] for p in points],
        c='black', s=14, zorder=5, label='tested combinations',
    )

    # Mark the best cell
    best_val  = max(grid.values())
    best_cell = max(grid, key=grid.get)
    ax.scatter(
        [best_cell[0]], [best_cell[1]], [best_val],
        c='gold', s=120, marker='*', zorder=6, label='best combination',
    )

    ax.set_xlabel(param1_name, labelpad=12, fontsize=11)
    ax.set_ylabel(param2_name, labelpad=12, fontsize=11)
    ax.set_zlabel('Final Portfolio Value ($)', labelpad=12, fontsize=11)
    ax.set_title(
        f'Profit Surface  ·  {param1_name}  vs  {param2_name}\n'
        f'(z = best final value per cell across remaining parameters)',
        fontsize=12, pad=18,
    )
    ax.legend(loc='upper left', fontsize=9)

    cbar = fig.colorbar(surf, ax=ax, shrink=0.45, aspect=12, pad=0.10)
    cbar.set_label('Final Portfolio Value ($)', fontsize=10)

    # Annotate break-even plane
    ax.plot_surface(
        X, Y,
        np.full_like(Z, initial_cash),
        alpha=0.15, color='grey',
    )

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    print(f'Parameter surface saved → {save_path}')
    plt.show(block=False)


# ──────────────────────────────────────────────── candlestick + trades ──────

def plot_candlestick_trades(
    strategy,
    title: str = 'Candlestick + Trades',
    save_path: str = 'candlestick_trades.png',
) -> None:
    """
    Dark-themed candlestick chart with entry/exit markers for every trade.

    Extracts OHLCV data directly from the strategy's data feed and overlays
    green up-triangles (entries) and red down-triangles (exits) at the
    executed price on the matching bar.

    Args:
        strategy  : completed backtrader Strategy instance
        title     : chart title
        save_path : PNG output path (also shown interactively)
    """
    df = strategy.data._dataname[['open', 'high', 'low', 'close', 'volume']].copy()
    df.index = pd.to_datetime(df.index)
    n = len(df)

    # ── layout ────────────────────────────────────────────────────────────────
    fig, (ax_price, ax_vol) = plt.subplots(
        2, 1, figsize=(16, 9),
        gridspec_kw={'height_ratios': [4, 1]},
        sharex=True,
    )
    fig.patch.set_facecolor('#131722')
    for ax in (ax_price, ax_vol):
        ax.set_facecolor('#131722')
        ax.tick_params(colors='#d1d4dc')
        for spine in ax.spines.values():
            spine.set_edgecolor('#363c4e')

    # ── candlesticks (numeric x = bar index) ─────────────────────────────────
    bar_w = 0.6
    for i, (_, row) in enumerate(df.iterrows()):
        bull  = row['close'] >= row['open']
        color = '#26a69a' if bull else '#ef5350'
        body_bot = min(row['open'], row['close'])
        body_h   = abs(row['close'] - row['open']) or (row['high'] - row['low']) * 0.01

        ax_price.add_patch(mpatches.Rectangle(
            (i - bar_w / 2, body_bot), bar_w, body_h,
            facecolor=color, edgecolor=color, linewidth=0.5,
        ))
        ax_price.plot([i, i], [row['low'], row['high']], color=color, linewidth=0.8)
        ax_vol.bar(i, row['volume'], width=bar_w, color=color, alpha=0.6)

    ax_price.set_xlim(-1, n)
    ax_price.set_ylim(df['low'].min() * 0.998, df['high'].max() * 1.002)
    ax_vol.set_xlim(-1, n)

    # ── x-axis tick labels (≈10 evenly spaced) ───────────────────────────────
    tick_step   = max(1, n // 10)
    tick_idxs   = list(range(0, n, tick_step))
    tick_labels = [df.index[i].strftime('%Y-%m-%d\n%H:%M') for i in tick_idxs]
    ax_vol.set_xticks(tick_idxs)
    ax_vol.set_xticklabels(tick_labels, fontsize=7, color='#d1d4dc')

    # ── map a trade date + price to the nearest bar index ────────────────────
    index_dates = df.index.date  # numpy array of datetime.date

    def _bar_idx(trade_date, price) -> int | None:
        positions = np.where(index_dates == trade_date)[0]
        if not positions.size:
            return None
        prices = df.iloc[positions]['close'].values
        return int(positions[np.argmin(np.abs(prices - price))])

    # ── entry / exit markers ─────────────────────────────────────────────────
    long_entry_xs,  long_entry_ys  = [], []
    short_entry_xs, short_entry_ys = [], []
    long_exit_xs,   long_exit_ys   = [], []
    short_exit_xs,  short_exit_ys  = [], []

    for t in strategy.trade_log:
        is_long = t['direction'] == 'Long'

        ei = _bar_idx(t['entry_date'], t['entry_price'])
        if ei is not None:
            (long_entry_xs if is_long else short_entry_xs).append(ei)
            (long_entry_ys if is_long else short_entry_ys).append(t['entry_price'])

        if t['exit_price'] is not None:
            xi = _bar_idx(t['exit_date'], t['exit_price'])
            if xi is not None:
                (long_exit_xs if is_long else short_exit_xs).append(xi)
                (long_exit_ys if is_long else short_exit_ys).append(t['exit_price'])

    if long_entry_xs:
        ax_price.scatter(long_entry_xs, long_entry_ys, marker='^', color='#00e676',
                         s=110, zorder=5, label='Long Entry')
    if short_entry_xs:
        ax_price.scatter(short_entry_xs, short_entry_ys, marker='v', color='#ff1744',
                         s=110, zorder=5, label='Short Entry')
    if long_exit_xs:
        ax_price.scatter(long_exit_xs, long_exit_ys, marker='x', color='#00e676',
                         s=80, zorder=5, label='Long Exit', linewidths=1.5)
    if short_exit_xs:
        ax_price.scatter(short_exit_xs, short_exit_ys, marker='x', color='#ff1744',
                         s=80, zorder=5, label='Short Exit', linewidths=1.5)

    has_markers = any([long_entry_xs, short_entry_xs, long_exit_xs, short_exit_xs])

    # ── cosmetics ─────────────────────────────────────────────────────────────
    ax_price.set_title(title, color='#d1d4dc', fontsize=13, pad=10)
    ax_price.set_ylabel('Price', color='#d1d4dc', fontsize=10)
    ax_vol.set_ylabel('Volume', color='#d1d4dc', fontsize=10)
    ax_price.grid(color='#363c4e', linewidth=0.5, alpha=0.6)
    ax_vol.grid(color='#363c4e', linewidth=0.5, alpha=0.6)

    if has_markers:
        ax_price.legend(facecolor='#1e222d', labelcolor='#d1d4dc', fontsize=9)

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches='tight',
                facecolor=fig.get_facecolor())
    print(f'Candlestick chart saved → {save_path}')
    plt.show(block=False)
