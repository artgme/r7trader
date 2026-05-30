"""
trade_monitor.py — Trade analytics dashboard.

Auto-discovers all CSV trade logs in the logs/ folder and displays:
  - Dropdown  : "All Files (Combined)" or any individual log file
  - Stats row : total trades, win rate, total P&L, avg win/loss, profit factor
  - Equity curve : cumulative P&L over completed round-trips
  - Trade table  : every completed entry/exit pair with individual P&L

Refreshes automatically every REFRESH_INTERVAL_S seconds.
Runs on http://127.0.0.1:8052 — independent of the live chart on 8051.

Usage
-----
    python trade_monitor.py              # scans default logs/ folder
    python trade_monitor.py path/to/logs # custom folder
"""

import logging
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objs as go
from dash import Dash, Input, Output, dcc, html

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

PORT               = 8052
REFRESH_INTERVAL_S = 15     # how often the dashboard re-reads the CSV files (seconds)
LOGS_DIR           = 'logs' # folder that is scanned for *.csv trade logs

_ALL_LABEL = '__all__'      # sentinel value for the "combined" dropdown option

_DARK  = '#1e1e1e'
_PANEL = '#2a2a2a'
_GREEN = '#26a69a'
_RED   = '#ef5350'
_TEXT  = '#ccc'
_MUTED = '#888'


# ─── Data helpers ─────────────────────────────────────────────────────────────

# Return sorted list of CSV file paths found in the logs folder.
def _find_csv_files(logs_dir: str) -> list[Path]:
    d = Path(logs_dir)
    if not d.exists():
        return []
    return sorted(d.glob('*.csv'))


# Read one CSV; return empty DataFrame on missing/empty/corrupt file.
def _load_csv(path: Path) -> pd.DataFrame:
    if not path.exists() or path.stat().st_size == 0:
        return pd.DataFrame()
    try:
        return pd.read_csv(path, parse_dates=['timestamp'])
    except Exception as e:
        logger.warning('Could not read %s: %s', path, e)
        return pd.DataFrame()


# Pair consecutive entry/exit rows in one DataFrame into completed trades.
# Trades from different symbols are paired independently so cross-symbol
# entries never accidentally match each other.
def _compute_trades(df: pd.DataFrame) -> list[dict]:
    trades     = []
    open_trade = None

    for _, row in df.iterrows():
        action = row['action']
        price  = float(row['price'])
        size   = float(row['size'])
        sym    = row.get('symbol', '')

        if action in ('enter_long', 'enter_short'):
            open_trade = {
                'entry_time':  row['timestamp'],
                'symbol':      sym,
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


# Load trades from a single file or combine all files in the folder.
# Returns (trades_list, source_label).
def _load_trades(selection: str, logs_dir: str) -> tuple[list[dict], str]:
    if selection == _ALL_LABEL:
        all_trades = []
        for path in _find_csv_files(logs_dir):
            df = _load_csv(path)
            if not df.empty:
                all_trades.extend(_compute_trades(df))
        # sort combined trades chronologically
        all_trades.sort(key=lambda t: t['entry_time'])
        return all_trades, 'All Files (Combined)'

    path = Path(selection)
    df   = _load_csv(path)
    trades = _compute_trades(df) if not df.empty else []
    return trades, path.name


# Compute summary statistics from a list of completed trades.
def _summary(trades: list) -> dict:
    if not trades:
        return {'total': 0, 'win_rate': 0.0, 'total_pnl': 0.0,
                'avg_win': 0.0, 'avg_loss': 0.0, 'profit_factor': '—'}

    pnls       = [t['pnl'] for t in trades]
    wins       = [p for p in pnls if p > 0]
    losses     = [p for p in pnls if p <= 0]
    gross_win  = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        'total':         len(trades),
        'win_rate':      round(len(wins) / len(trades) * 100, 1),
        'total_pnl':     round(sum(pnls), 4),
        'avg_win':       round(gross_win  / len(wins),   4) if wins   else 0.0,
        'avg_loss':      round(gross_loss / len(losses), 4) if losses else 0.0,
        'profit_factor': round(gross_win / gross_loss, 2)   if gross_loss else '—',
    }


# ─── Plotly figures ───────────────────────────────────────────────────────────

def _equity_figure(trades: list, label: str) -> go.Figure:
    if not trades:
        return go.Figure(layout=go.Layout(
            paper_bgcolor=_DARK, plot_bgcolor=_DARK,
            font=dict(color=_MUTED),
            annotations=[dict(text='No completed trades yet.',
                              showarrow=False, font=dict(size=16, color=_MUTED))],
        ))

    labels  = [f"#{i+1}  {t['exit_time']}  {t['symbol']}" for i, t in enumerate(trades)]
    cum_pnl = []
    running = 0.0
    colors  = []
    for t in trades:
        running += t['pnl']
        cum_pnl.append(round(running, 4))
        colors.append(_GREEN if t['pnl'] > 0 else _RED)

    fig = go.Figure()
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
    fig.add_hline(y=0, line=dict(color=_MUTED, width=1, dash='dot'))
    fig.update_layout(
        uirevision=label,
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

def _stat_card(label: str, value, color: str = _TEXT) -> html.Div:
    return html.Div([
        html.Div(label, style={'fontSize': '11px', 'color': _MUTED,
                               'textTransform': 'uppercase', 'letterSpacing': '1px'}),
        html.Div(str(value), style={'fontSize': '26px', 'fontWeight': 'bold',
                                    'color': color, 'marginTop': '4px'}),
    ], style={'background': _PANEL, 'padding': '16px 24px', 'borderRadius': '8px',
              'minWidth': '130px', 'textAlign': 'center'})


def _trade_table(trades: list) -> html.Table:
    hstyle = {'padding': '8px 12px', 'textAlign': 'left', 'color': _MUTED,
              'fontSize': '12px', 'borderBottom': '1px solid #444'}
    cstyle = {'padding': '7px 12px', 'fontSize': '13px'}

    headers = ['#', 'Symbol', 'Dir', 'Entry time', 'Entry $',
               'Exit time', 'Exit $', 'Size', 'P&L', 'Result']
    rows = []
    for i, t in enumerate(reversed(trades), 1):
        pc = _GREEN if t['pnl'] > 0 else _RED
        rows.append(html.Tr([
            html.Td(len(trades) - i + 1,       style=cstyle),
            html.Td(t['symbol'],               style=cstyle),
            html.Td(t['direction'],            style=cstyle),
            html.Td(t['entry_time'],           style=cstyle),
            html.Td(f"{t['entry_price']:.4f}", style=cstyle),
            html.Td(t['exit_time'],            style=cstyle),
            html.Td(f"{t['exit_price']:.4f}",  style=cstyle),
            html.Td(t['size'],                 style=cstyle),
            html.Td(f"{t['pnl']:+.4f}",
                    style={**cstyle, 'color': pc, 'fontWeight': 'bold'}),
            html.Td(t['result'], style={**cstyle, 'color': pc}),
        ], style={'borderBottom': '1px solid #2e2e2e'}))

    return html.Table(
        [html.Thead(html.Tr([html.Th(h, style=hstyle) for h in headers])),
         html.Tbody(rows)],
        style={'width': '100%', 'borderCollapse': 'collapse',
               'fontFamily': 'monospace', 'color': _TEXT},
    )


# Per-file summary table: one row per CSV file + a Total row at the bottom.
def _file_summary_table(logs_dir: str) -> html.Div:
    csv_files = _find_csv_files(logs_dir)
    if not csv_files:
        return html.Div()

    hstyle = {'padding': '8px 14px', 'textAlign': 'left', 'color': _MUTED,
              'fontSize': '12px', 'borderBottom': '1px solid #444',
              'textTransform': 'uppercase', 'letterSpacing': '1px'}
    cstyle = {'padding': '8px 14px', 'fontSize': '13px'}

    headers = ['File', 'Trades', 'P&L ($)', 'Win Rate']
    rows    = []
    totals  = {'trades': 0, 'pnl': 0.0, 'wins': 0}

    for path in csv_files:
        df     = _load_csv(path)
        trades = _compute_trades(df) if not df.empty else []
        stats  = _summary(trades)

        totals['trades'] += stats['total']
        totals['pnl']    += stats['total_pnl']
        totals['wins']   += int(round(stats['total'] * stats['win_rate'] / 100)) if stats['total'] else 0

        pc = _GREEN if stats['total_pnl'] >= 0 else _RED
        wc = _GREEN if stats['win_rate'] >= 50 else _RED

        rows.append(html.Tr([
            html.Td(path.name,                        style=cstyle),
            html.Td(stats['total'],                   style=cstyle),
            html.Td(f"${stats['total_pnl']:+.4f}",
                    style={**cstyle, 'color': pc, 'fontWeight': 'bold'}),
            html.Td(f"{stats['win_rate']}%",
                    style={**cstyle, 'color': wc}),
        ], style={'borderBottom': '1px solid #2e2e2e'}))

    # Total row
    total_wr  = round(totals['wins'] / totals['trades'] * 100, 1) if totals['trades'] else 0.0
    total_pc  = _GREEN if totals['pnl'] >= 0 else _RED
    total_wc  = _GREEN if total_wr >= 50 else _RED
    rows.append(html.Tr([
        html.Td('TOTAL', style={**cstyle, 'fontWeight': 'bold', 'color': _TEXT}),
        html.Td(totals['trades'],
                style={**cstyle, 'fontWeight': 'bold'}),
        html.Td(f"${totals['pnl']:+.4f}",
                style={**cstyle, 'color': total_pc, 'fontWeight': 'bold'}),
        html.Td(f"{total_wr}%",
                style={**cstyle, 'color': total_wc, 'fontWeight': 'bold'}),
    ], style={'borderTop': '1px solid #555'}))

    table = html.Table(
        [html.Thead(html.Tr([html.Th(h, style=hstyle) for h in headers])),
         html.Tbody(rows)],
        style={'width': '100%', 'borderCollapse': 'collapse',
               'fontFamily': 'monospace', 'color': _TEXT},
    )
    return html.Div(table,
                    style={'background': _PANEL, 'borderRadius': '8px',
                           'padding': '8px', 'overflowX': 'auto',
                           'marginBottom': '16px'})


# ─── Dash app ─────────────────────────────────────────────────────────────────

def _build_app(logs_dir: str) -> Dash:
    app = Dash(__name__)

    app.layout = html.Div(
        style={'background': _DARK, 'minHeight': '100vh',
               'padding': '16px', 'fontFamily': 'sans-serif'},
        children=[
            html.Div([
                html.H3('Trade Monitor',
                        style={'color': _TEXT, 'margin': '0', 'display': 'inline-block'}),
                dcc.Dropdown(
                    id='file-select',
                    options=[],
                    value=_ALL_LABEL,
                    clearable=False,
                    style={'width': '280px', 'marginLeft': '24px',
                           'verticalAlign': 'middle', 'display': 'inline-block'},
                ),
            ], style={'display': 'flex', 'alignItems': 'center', 'marginBottom': '16px'}),

            dcc.Interval(id='interval',
                         interval=REFRESH_INTERVAL_S * 1000, n_intervals=0),

            html.Div(id='stats-row',
                     style={'display': 'flex', 'gap': '12px',
                            'flexWrap': 'wrap', 'marginBottom': '16px'}),

            dcc.Graph(id='equity-chart', style={'height': '35vh', 'marginBottom': '16px'},
                      config={'displayModeBar': False}),

            html.Div(id='file-summary'),

            html.Div(id='trade-table',
                     style={'background': _PANEL, 'borderRadius': '8px',
                            'padding': '8px', 'overflowX': 'auto'}),
        ],
    )

    @app.callback(
        Output('file-select',  'options'),
        Output('file-select',  'value'),
        Output('stats-row',    'children'),
        Output('equity-chart', 'figure'),
        Output('file-summary', 'children'),
        Output('trade-table',  'children'),
        Input('interval',      'n_intervals'),
        Input('file-select',   'value'),
    )
    def refresh(_n, selected):
        # build dropdown options: "All" first, then one entry per discovered file
        csv_files = _find_csv_files(logs_dir)
        options = [{'label': 'All Files (Combined)', 'value': _ALL_LABEL}] + [
            {'label': p.name, 'value': str(p)} for p in csv_files
        ]

        # keep current selection if still valid; otherwise fall back to "All"
        valid_values = {o['value'] for o in options}
        sel = selected if selected in valid_values else _ALL_LABEL

        trades, label = _load_trades(sel, logs_dir)
        stats = _summary(trades)

        pnl_color = _GREEN if stats['total_pnl'] >= 0 else _RED
        cards = [
            _stat_card('Trades',       stats['total']),
            _stat_card('Win Rate',     f"{stats['win_rate']}%",
                       _GREEN if stats['win_rate'] >= 50 else _RED),
            _stat_card('Total P&L',    f"${stats['total_pnl']:+.2f}", pnl_color),
            _stat_card('Avg Win',      f"${stats['avg_win']:.2f}",  _GREEN),
            _stat_card('Avg Loss',     f"${stats['avg_loss']:.2f}", _RED),
            _stat_card('Profit Factor', stats['profit_factor']),
        ]

        table = (_trade_table(trades) if trades
                 else html.Div('No completed trades yet.',
                               style={'color': _MUTED, 'padding': '24px',
                                      'textAlign': 'center'}))

        return options, sel, cards, _equity_figure(trades, label), _file_summary_table(logs_dir), table

    return app


def main(logs_dir: str = LOGS_DIR) -> None:
    Path(logs_dir).mkdir(parents=True, exist_ok=True)
    logger.info('Trade monitor → http://127.0.0.1:%s  (logs: %s/)', PORT, logs_dir)
    app = _build_app(logs_dir)
    app.run(host='127.0.0.1', port=PORT, debug=False)


if __name__ == '__main__':
    folder = sys.argv[1] if len(sys.argv) > 1 else LOGS_DIR
    main(folder)
