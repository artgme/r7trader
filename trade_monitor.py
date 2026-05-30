"""
trade_monitor.py — Trade analytics dashboard.

Reads the CSV trade log produced by Mozg and displays:
  - Summary stats : total trades, win rate, total P&L, avg win/loss, profit factor
  - Equity curve  : cumulative P&L over completed round-trips
  - Trade table   : every completed entry/exit pair with individual P&L

Refreshes automatically every 5 seconds so it stays live while Mozg is running.
Runs on http://127.0.0.1:8052 — independent of the live chart on 8051.

Usage
-----
    python trade_monitor.py                         # uses default CSV
    python trade_monitor.py trades_sol_v8.csv       # custom CSV path
"""

import logging
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objs as go
from dash import Dash, Input, Output, dcc, html

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

PORT     = 8052
_DARK    = '#1e1e1e'
_PANEL   = '#2a2a2a'
_GREEN   = '#26a69a'
_RED     = '#ef5350'
_TEXT    = '#ccc'
_MUTED   = '#888'


# ─── Data loading & trade pairing ─────────────────────────────────────────────

# Read the Mozg CSV; return an empty DataFrame if the file is missing or empty.
def _load_csv(path: str) -> pd.DataFrame:
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, parse_dates=['timestamp'])
    except Exception as e:
        logger.warning('Could not read CSV: %s', e)
        return pd.DataFrame()


# Pair consecutive entry/exit rows into completed round-trip trade dicts.
# An open entry with no matching exit is kept pending and excluded from results.
def _compute_trades(df: pd.DataFrame) -> list:
    trades     = []
    open_trade = None

    for _, row in df.iterrows():
        action = row['action']
        price  = float(row['price'])
        size   = float(row['size'])

        if action in ('enter_long', 'enter_short'):
            open_trade = {
                'entry_time':  row['timestamp'],
                'symbol':      row['symbol'],
                'direction':   'Long' if action == 'enter_long' else 'Short',
                'entry_price': price,
                'size':        size,
            }

        elif action.startswith('exit_') and open_trade is not None:
            ep  = open_trade['entry_price']
            pnl = (
                (price - ep) * size if open_trade['direction'] == 'Long'
                else (ep - price) * size
            )
            trades.append({
                'entry_time':  str(open_trade['entry_time'])[:19],
                'exit_time':   str(row['timestamp'])[:19],
                'symbol':      open_trade['symbol'],
                'direction':   open_trade['direction'],
                'entry_price': round(ep, 4),
                'exit_price':  round(price, 4),
                'size':        size,
                'pnl':         round(pnl, 4),
                'result':      'Win' if pnl > 0 else 'Loss',
            })
            open_trade = None

    return trades


# Compute summary statistics from a list of completed trades.
def _summary(trades: list) -> dict:
    if not trades:
        return {'total': 0, 'win_rate': 0.0, 'total_pnl': 0.0,
                'avg_win': 0.0, 'avg_loss': 0.0, 'profit_factor': '—'}

    pnls   = [t['pnl'] for t in trades]
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        'total':         len(trades),
        'win_rate':      round(len(wins) / len(trades) * 100, 1),
        'total_pnl':     round(sum(pnls), 4),
        'avg_win':       round(gross_win  / len(wins),   4) if wins   else 0.0,
        'avg_loss':      round(gross_loss / len(losses), 4) if losses else 0.0,
        'profit_factor': round(gross_win / gross_loss, 2) if gross_loss else '—',
    }


# ─── Plotly figures ───────────────────────────────────────────────────────────

# Build a dark-themed cumulative P&L line chart from the list of completed trades.
def _equity_figure(trades: list) -> go.Figure:
    if not trades:
        return go.Figure(layout=go.Layout(
            paper_bgcolor=_DARK, plot_bgcolor=_DARK,
            font=dict(color=_MUTED),
            annotations=[dict(text='No completed trades yet.',
                              showarrow=False, font=dict(size=16, color=_MUTED))],
        ))

    labels   = [f"#{i+1}  {t['exit_time']}" for i, t in enumerate(trades)]
    cum_pnl  = []
    running  = 0.0
    colors   = []
    for t in trades:
        running += t['pnl']
        cum_pnl.append(round(running, 4))
        colors.append(_GREEN if t['pnl'] > 0 else _RED)

    fig = go.Figure()

    # shaded area under the equity curve
    fig.add_trace(go.Scatter(
        x=list(range(1, len(trades) + 1)),
        y=cum_pnl,
        mode='lines+markers',
        line=dict(color=_GREEN, width=2),
        marker=dict(color=colors, size=8, line=dict(color='white', width=1)),
        fill='tozeroy',
        fillcolor='rgba(38,166,154,0.12)',
        hovertext=labels,
        hoverinfo='text+y',
        name='Equity',
    ))

    # zero reference line
    fig.add_hline(y=0, line=dict(color=_MUTED, width=1, dash='dot'))

    fig.update_layout(
        uirevision='equity',
        paper_bgcolor=_DARK, plot_bgcolor=_DARK,
        font=dict(color=_TEXT),
        xaxis=dict(title='Trade #', showgrid=False, color=_MUTED),
        yaxis=dict(title='Cumulative P&L ($)', showgrid=True,
                   gridcolor='#333', color=_MUTED, zeroline=False),
        margin=dict(l=60, r=20, t=20, b=40),
        showlegend=False,
    )
    return fig


# ─── Layout helpers ───────────────────────────────────────────────────────────

# Render one summary stat card (label + big number).
def _stat_card(label: str, value, color: str = _TEXT) -> html.Div:
    return html.Div([
        html.Div(label, style={'fontSize': '11px', 'color': _MUTED,
                               'textTransform': 'uppercase', 'letterSpacing': '1px'}),
        html.Div(str(value), style={'fontSize': '26px', 'fontWeight': 'bold',
                                    'color': color, 'marginTop': '4px'}),
    ], style={
        'background': _PANEL, 'padding': '16px 24px', 'borderRadius': '8px',
        'minWidth': '130px', 'textAlign': 'center',
    })


# Render the trade table as a plain HTML table with dark styling.
def _trade_table(trades: list) -> html.Table:
    header_style = {
        'padding': '8px 12px', 'textAlign': 'left',
        'color': _MUTED, 'fontSize': '12px',
        'borderBottom': '1px solid #444',
    }
    cell_style = {'padding': '7px 12px', 'fontSize': '13px'}

    headers = ['#', 'Symbol', 'Dir', 'Entry time', 'Entry $',
               'Exit time', 'Exit $', 'Size', 'P&L', 'Result']

    rows = []
    for i, t in enumerate(reversed(trades), 1):   # newest first
        pnl_color = _GREEN if t['pnl'] > 0 else _RED
        rows.append(html.Tr([
            html.Td(len(trades) - i + 1,     style=cell_style),
            html.Td(t['symbol'],             style=cell_style),
            html.Td(t['direction'],          style=cell_style),
            html.Td(t['entry_time'],         style=cell_style),
            html.Td(f"{t['entry_price']:.4f}", style=cell_style),
            html.Td(t['exit_time'],          style=cell_style),
            html.Td(f"{t['exit_price']:.4f}", style=cell_style),
            html.Td(t['size'],               style=cell_style),
            html.Td(f"{t['pnl']:+.4f}",
                    style={**cell_style, 'color': pnl_color, 'fontWeight': 'bold'}),
            html.Td(t['result'],
                    style={**cell_style, 'color': pnl_color}),
        ], style={'borderBottom': '1px solid #2e2e2e'}))

    return html.Table(
        [html.Thead(html.Tr([html.Th(h, style=header_style) for h in headers])),
         html.Tbody(rows)],
        style={'width': '100%', 'borderCollapse': 'collapse',
               'fontFamily': 'monospace', 'color': _TEXT},
    )


# ─── Dash app ─────────────────────────────────────────────────────────────────

def _build_app(csv_path: str) -> Dash:
    app = Dash(__name__)

    app.layout = html.Div(style={'background': _DARK, 'minHeight': '100vh',
                                 'padding': '16px', 'fontFamily': 'sans-serif'}, children=[
        html.H3('Trade Monitor', style={'color': _TEXT, 'margin': '0 0 4px 4px'}),
        html.Div(csv_path, style={'color': _MUTED, 'fontSize': '12px',
                                  'margin': '0 0 16px 4px'}),

        # auto-refresh every 5 seconds
        dcc.Interval(id='interval', interval=5_000, n_intervals=0),

        # summary stat cards
        html.Div(id='stats-row', style={
            'display': 'flex', 'gap': '12px', 'flexWrap': 'wrap', 'marginBottom': '16px',
        }),

        # equity curve
        dcc.Graph(id='equity-chart', style={'height': '35vh', 'marginBottom': '16px'},
                  config={'displayModeBar': False}),

        # trade table
        html.Div(id='trade-table', style={
            'background': _PANEL, 'borderRadius': '8px',
            'padding': '8px', 'overflowX': 'auto',
        }),
    ])

    @app.callback(
        Output('stats-row',    'children'),
        Output('equity-chart', 'figure'),
        Output('trade-table',  'children'),
        Input('interval',      'n_intervals'),
    )
    def refresh(_n):
        df     = _load_csv(csv_path)
        trades = _compute_trades(df) if not df.empty else []
        stats  = _summary(trades)

        pnl_color = _GREEN if stats['total_pnl'] >= 0 else _RED

        cards = [
            _stat_card('Trades',        stats['total']),
            _stat_card('Win Rate',       f"{stats['win_rate']}%",
                       _GREEN if stats['win_rate'] >= 50 else _RED),
            _stat_card('Total P&L',      f"${stats['total_pnl']:+.2f}", pnl_color),
            _stat_card('Avg Win',        f"${stats['avg_win']:.2f}",  _GREEN),
            _stat_card('Avg Loss',       f"${stats['avg_loss']:.2f}", _RED),
            _stat_card('Profit Factor',  stats['profit_factor']),
        ]

        table = (_trade_table(trades) if trades
                 else html.Div('No completed trades yet.',
                               style={'color': _MUTED, 'padding': '24px',
                                      'textAlign': 'center'}))

        return cards, _equity_figure(trades), table

    return app


def main(csv_path: str = 'trades_sol_v8.csv') -> None:
    logger.info('Trade monitor → http://127.0.0.1:%s  (CSV: %s)', PORT, csv_path)
    app = _build_app(csv_path)
    app.run(host='127.0.0.1', port=PORT, debug=False)


if __name__ == '__main__':
    path = sys.argv[1] if len(sys.argv) > 1 else 'trades_sol_v8.csv'
    main(path)
