"""
control_panel.py — Interactive trading control panel.

Replaces test_mozg_kraken.py for interactive use.  Start and stop engines
from the browser instead of editing source code.

Runs on http://127.0.0.1:8053

Usage
-----
    python control_panel.py
"""

import importlib
import inspect
import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

import backtrader as bt
from dash import ALL, Dash, Input, Output, State, callback_context, dcc, html
from dash.exceptions import PreventUpdate

from configs import get_params, get_symbols, get_timeframes
from mozg import Mozg

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

PORT     = 8053
DASH_URL = 'http://127.0.0.1:8051'   # live chart — set to None if not running

_DARK  = '#1e1e1e'
_PANEL = '#2a2a2a'
_GREEN = '#26a69a'
_RED   = '#ef5350'
_TEXT  = '#ccc'
_MUTED = '#888'


# ─── Strategy discovery ───────────────────────────────────────────────────────

# Scan the strategies/ folder and return {ClassName: class} for every
# bt.Strategy subclass found.  Called once at import time.
def _discover_strategies() -> dict[str, type]:
    result = {}
    strat_dir = Path('strategies')
    if not strat_dir.exists():
        return result
    for path in sorted(strat_dir.glob('*.py')):
        if path.stem.startswith('_'):
            continue
        try:
            mod = importlib.import_module(f'strategies.{path.stem}')
            for name, obj in inspect.getmembers(mod, inspect.isclass):
                if issubclass(obj, bt.Strategy) and obj is not bt.Strategy:
                    result[name] = obj
        except Exception as e:
            logger.warning('Could not load %s: %s', path.stem, e)
    return result

STRATEGIES: dict[str, type] = _discover_strategies()


# ─── Engine registry ──────────────────────────────────────────────────────────

_registry: dict[str, dict] = {}   # uid → {engine, thread, cfg, started_at}
_reg_lock  = threading.Lock()
_activity:  list[str] = []        # last 50 log messages shown in the UI


def _log(msg: str) -> None:
    ts    = datetime.now(timezone.utc).strftime('%H:%M:%S')
    entry = f'[{ts}]  {msg}'
    logger.info(msg)
    _activity.append(entry)
    if len(_activity) > 50:
        _activity.pop(0)


def _uid(symbol: str, strategy_name: str) -> str:
    return f"{symbol.replace('/', '_')}_{strategy_name}"


# Connect and launch a Mozg engine in a daemon thread.
# Returns the uid on success, None on failure.
def _start_engine(symbol: str, timeframe: str, strategy_name: str,
                  size: float, broker: str, paper_mode: bool,
                  order_type: str = 'market',
                  ibkr_exchange: str = 'SMART',
                  ibkr_currency: str = 'USD') -> str | None:
    uid = _uid(symbol, strategy_name)

    with _reg_lock:
        existing = _registry.get(uid)
        if existing and existing['thread'].is_alive():
            _log(f'Engine {uid} is already running.')
            return None

    strategy_cls = STRATEGIES.get(strategy_name)
    if not strategy_cls:
        _log(f'Unknown strategy: {strategy_name}')
        return None

    params = get_params(strategy_name, symbol, timeframe)
    trail_pct = params.get('trail_stop_pct')
    tp_price  = params.get('take_profit_price')
    params['printlog'] = True

    safe_sym = symbol.replace('/', '_').lower()
    csv_path = f'logs/trades_{safe_sym}_{strategy_name.lower()}.csv'

    engine = Mozg(
        symbol          = symbol,
        timeframe       = timeframe,
        strategy_class  = strategy_cls,
        strategy_params = params,
        broker_type     = broker,
        trade_size      = size,
        history_limit   = 100,
        poll_interval_s = 10,
        dash_url        = DASH_URL,
        csv_path        = csv_path,
        paper_mode      = paper_mode,
        order_type      = order_type,
        ibkr_exchange   = ibkr_exchange,
        ibkr_currency   = ibkr_currency,
    )

    if not engine.connect():
        _log(f'Connection failed for {symbol}.')
        return None

    thread = threading.Thread(
        target = lambda: engine.run(handle_sigint=False),
        daemon = True,
        name   = f'mozg-{uid}',
    )

    with _reg_lock:
        _registry[uid] = {
            'engine':     engine,
            'thread':     thread,
            'cfg': {
                'symbol':        symbol,
                'timeframe':     timeframe,
                'strategy':      strategy_name,
                'broker':        broker,
                'paper':         paper_mode,
                'size':          size,
                'trail_pct':     trail_pct,
                'tp_price':      tp_price,
                'order_type':    order_type,
                'ibkr_exchange': ibkr_exchange,
                'ibkr_currency': ibkr_currency,
            },
            'started_at': datetime.now(timezone.utc).strftime('%H:%M:%S'),
        }

    thread.start()
    mode = 'PAPER' if paper_mode else 'LIVE'
    _log(f'[{mode}] Started {symbol} {timeframe} | {strategy_name} | {broker} | size={size}')
    return uid


def _stop_engine(uid: str) -> None:
    with _reg_lock:
        entry = _registry.get(uid)
    if entry:
        entry['engine'].stop()
        _log(f'Stopped engine: {uid}')


# ─── Dash app ─────────────────────────────────────────────────────────────────

def _input_label(text: str) -> html.Div:
    return html.Div(text, style={'color': _MUTED, 'fontSize': '11px',
                                 'marginBottom': '4px', 'textTransform': 'uppercase',
                                 'letterSpacing': '1px'})


def _field(label: str, control) -> html.Div:
    return html.Div([_input_label(label), control],
                    style={'marginRight': '14px'})


def _build_app() -> Dash:
    strategy_options = [{'label': k, 'value': k} for k in sorted(STRATEGIES)]
    first_strategy   = sorted(STRATEGIES)[0] if STRATEGIES else None
    first_symbols    = get_symbols(first_strategy) if first_strategy else []
    first_symbol     = first_symbols[0] if first_symbols else None
    first_timeframes = get_timeframes(first_strategy, first_symbol) if first_symbol else []

    input_style = {
        'background': '#333', 'color': _TEXT, 'border': '1px solid #444',
        'borderRadius': '4px', 'padding': '7px 10px', 'width': '100%',
    }

    app = Dash(__name__, suppress_callback_exceptions=True)

    app.layout = html.Div(
        style={'background': _DARK, 'minHeight': '100vh',
               'padding': '20px', 'fontFamily': 'sans-serif'},
        children=[
            html.H3('Control Panel',
                    style={'color': _TEXT, 'margin': '0 0 20px 0'}),

            dcc.Interval(id='cp-interval', interval=2000, n_intervals=0),

            # ── Add Symbol form ───────────────────────────────────────────────
            html.Div([
                html.Div('Add Symbol',
                         style={'color': _MUTED, 'fontSize': '11px', 'letterSpacing': '1px',
                                'textTransform': 'uppercase', 'marginBottom': '14px'}),
                html.Div([
                    _field('Strategy',
                           dcc.Dropdown(id='inp-strategy', clearable=False,
                                        options=strategy_options, value=first_strategy,
                                        style={'width': '210px', 'color': '#000'})),
                    _field('Symbol',
                           dcc.Dropdown(id='inp-symbol', clearable=False,
                                        options=[{'label': s, 'value': s} for s in first_symbols],
                                        value=first_symbol,
                                        style={'width': '140px', 'color': '#000'})),
                    _field('Timeframe',
                           dcc.Dropdown(id='inp-timeframe', clearable=False,
                                        options=[{'label': t, 'value': t} for t in first_timeframes],
                                        value=first_timeframes[0] if first_timeframes else None,
                                        style={'width': '90px', 'color': '#000'})),
                    _field('Size',
                           dcc.Input(id='inp-size', type='number', value=1.0,
                                     min=0.0001, step=0.0001,
                                     style={**input_style, 'width': '80px'})),
                    _field('Broker',
                           dcc.Dropdown(id='inp-broker', clearable=False,
                                        options=[{'label': 'ccxt (Kraken)', 'value': 'ccxt'},
                                                 {'label': 'IBKR',          'value': 'ibkr'}],
                                        value='ccxt',
                                        style={'width': '150px', 'color': '#000'})),
                    _field('Paper mode',
                           dcc.RadioItems(id='inp-paper',
                                          options=[{'label': ' Yes', 'value': 'yes'},
                                                   {'label': ' No',  'value': 'no'}],
                                          value='yes',
                                          inline=True,
                                          style={'color': _TEXT, 'marginTop': '6px'})),
                    _field('Order type',
                           dcc.RadioItems(id='inp-order-type',
                                          options=[{'label': ' Market', 'value': 'market'},
                                                   {'label': ' Limit',  'value': 'limit'}],
                                          value='market',
                                          inline=True,
                                          style={'color': _TEXT, 'marginTop': '6px'})),
                    _field('IBKR Exchange',
                           dcc.Input(id='inp-ibkr-exchange', type='text', value='SMART',
                                     placeholder='SMART',
                                     style={**input_style, 'width': '90px'})),
                    _field('IBKR Currency',
                           dcc.Input(id='inp-ibkr-currency', type='text', value='USD',
                                     placeholder='USD',
                                     style={**input_style, 'width': '70px'})),
                    html.Div(
                        html.Button('Add Symbol', id='btn-add', n_clicks=0,
                                    style={'background': _GREEN, 'color': 'white',
                                           'border': 'none', 'borderRadius': '6px',
                                           'padding': '9px 22px', 'cursor': 'pointer',
                                           'fontWeight': 'bold', 'fontSize': '13px'}),
                        style={'marginTop': '18px'},
                    ),
                ], style={'display': 'flex', 'alignItems': 'flex-end',
                          'flexWrap': 'wrap', 'gap': '4px'}),

                html.Div(id='add-feedback',
                         style={'color': _MUTED, 'fontSize': '12px', 'marginTop': '10px'}),
            ], style={'background': _PANEL, 'padding': '18px', 'borderRadius': '8px',
                      'marginBottom': '16px'}),

            # ── Running engines table ─────────────────────────────────────────
            html.Div([
                html.Div('Running Engines',
                         style={'color': _MUTED, 'fontSize': '11px', 'letterSpacing': '1px',
                                'textTransform': 'uppercase', 'marginBottom': '12px'}),
                html.Div(id='engine-table'),
            ], style={'background': _PANEL, 'padding': '18px', 'borderRadius': '8px',
                      'marginBottom': '16px'}),

            # ── Activity log ──────────────────────────────────────────────────
            html.Div([
                html.Div('Activity Log',
                         style={'color': _MUTED, 'fontSize': '11px', 'letterSpacing': '1px',
                                'textTransform': 'uppercase', 'marginBottom': '10px'}),
                html.Div(id='activity-log'),
            ], style={'background': _PANEL, 'padding': '18px', 'borderRadius': '8px'}),
        ],
    )

    # ── Strategy → symbol cascade ─────────────────────────────────────────────
    @app.callback(
        Output('inp-symbol', 'options'),
        Output('inp-symbol', 'value'),
        Input('inp-strategy', 'value'),
    )
    def update_symbols(strategy_name):
        symbols = get_symbols(strategy_name) if strategy_name else []
        options = [{'label': s, 'value': s} for s in symbols]
        value   = symbols[0] if symbols else None
        return options, value

    # ── Strategy + symbol → timeframe cascade ─────────────────────────────────
    @app.callback(
        Output('inp-timeframe', 'options'),
        Output('inp-timeframe', 'value'),
        Input('inp-strategy', 'value'),
        Input('inp-symbol',   'value'),
    )
    def update_timeframes(strategy_name, symbol):
        tfs     = get_timeframes(strategy_name, symbol) if strategy_name and symbol else []
        options = [{'label': t, 'value': t} for t in tfs]
        value   = tfs[0] if tfs else None
        return options, value

    # ── Add button ────────────────────────────────────────────────────────────
    @app.callback(
        Output('add-feedback', 'children'),
        Input('btn-add', 'n_clicks'),
        State('inp-symbol',   'value'),
        State('inp-timeframe','value'),
        State('inp-strategy', 'value'),
        State('inp-size',     'value'),
        State('inp-broker',        'value'),
        State('inp-paper',         'value'),
        State('inp-order-type',    'value'),
        State('inp-ibkr-exchange', 'value'),
        State('inp-ibkr-currency', 'value'),
        prevent_initial_call=True,
    )
    def on_add(_n, symbol, timeframe, strategy, size, broker, paper, order_type,
               ibkr_exchange, ibkr_currency):
        if not symbol or not strategy:
            return 'Please fill in all fields.'
        uid = _start_engine(
            symbol.strip(), timeframe, strategy,
            float(size or 1.0), broker, paper == 'yes',
            order_type or 'market',
            ibkr_exchange or 'SMART',
            (ibkr_currency or 'USD').upper(),
        )
        if uid:
            return f'✓ Engine started: {uid}'
        return '✗ Failed to start engine — check terminal for details.'

    # ── Engine table + activity log (interval + stop buttons) ─────────────────
    @app.callback(
        Output('engine-table',  'children'),
        Output('activity-log',  'children'),
        Input('cp-interval',    'n_intervals'),
        Input({'type': 'stop-btn', 'index': ALL}, 'n_clicks'),
    )
    def refresh(_, _stop_clicks):
        # detect which stop button was clicked (if any)
        ctx = callback_context
        if ctx.triggered:
            prop = ctx.triggered[0]['prop_id']
            val  = ctx.triggered[0]['value']
            if 'stop-btn' in prop and val:
                uid = json.loads(prop.split('.')[0])['index']
                _stop_engine(uid)

        # ── engine table ──────────────────────────────────────────────────────
        with _reg_lock:
            entries = list(_registry.items())

        if not entries:
            engine_table = html.Div('No engines running.  Add a symbol above.',
                                    style={'color': _MUTED, 'padding': '8px',
                                           'fontStyle': 'italic'})
        else:
            hstyle = {'padding': '8px 12px', 'color': _MUTED, 'fontSize': '11px',
                      'borderBottom': '1px solid #444', 'textTransform': 'uppercase',
                      'letterSpacing': '0.5px'}
            cstyle = {'padding': '8px 12px', 'fontSize': '13px', 'color': _TEXT}

            headers = ['Symbol', 'TF', 'Strategy', 'Broker',
                       'Exchange', 'Currency', 'Paper', 'Size', 'Position',
                       'T-SL', 'TP', 'Entry', 'Status', 'Started', '']
            rows = []
            for uid, entry in entries:
                cfg    = entry['cfg']
                engine = entry['engine']
                alive  = entry['thread'].is_alive()
                pos    = engine.position

                pos_color    = _GREEN if pos == 'long' else (_RED if pos == 'short' else _MUTED)
                status_color = _GREEN if alive else _RED

                trail_pct = cfg.get('trail_pct')
                tp_price  = cfg.get('tp_price')

                rows.append(html.Tr([
                    html.Td(cfg['symbol'],    style=cstyle),
                    html.Td(cfg['timeframe'], style=cstyle),
                    html.Td(cfg['strategy'],  style=cstyle),
                    html.Td(cfg['broker'],    style=cstyle),
                    html.Td(cfg.get('ibkr_exchange', '—') if cfg['broker'] == 'ibkr' else '—', style=cstyle),
                    html.Td(cfg.get('ibkr_currency', '—') if cfg['broker'] == 'ibkr' else '—', style=cstyle),
                    html.Td('Yes' if cfg['paper'] else '⚠ LIVE',
                            style={**cstyle,
                                   'color': _MUTED if cfg['paper'] else _RED}),
                    html.Td(cfg['size'],      style=cstyle),
                    html.Td(pos.upper(),
                            style={**cstyle, 'color': pos_color, 'fontWeight': 'bold'}),
                    html.Td(f'{trail_pct}%' if trail_pct is not None else '—', style=cstyle),
                    html.Td(str(tp_price)   if tp_price  is not None else '—', style=cstyle),
                    html.Td('LMT' if cfg.get('order_type') == 'limit' else 'MKT', style=cstyle),
                    html.Td('Running' if alive else 'Stopped',
                            style={**cstyle, 'color': status_color}),
                    html.Td(entry['started_at'], style=cstyle),
                    html.Td(
                        html.Button(
                            'Stop',
                            id={'type': 'stop-btn', 'index': uid},
                            n_clicks=0,
                            style={'background': _RED, 'color': 'white', 'border': 'none',
                                   'borderRadius': '4px', 'padding': '4px 14px',
                                   'cursor': 'pointer', 'fontWeight': 'bold'},
                        ) if alive else html.Span('—', style={'color': _MUTED}),
                        style=cstyle,
                    ),
                ], style={'borderBottom': '1px solid #2e2e2e'}))

            engine_table = html.Table(
                [html.Thead(html.Tr([html.Th(h, style=hstyle) for h in headers])),
                 html.Tbody(rows)],
                style={'width': '100%', 'borderCollapse': 'collapse',
                       'fontFamily': 'monospace'},
            )

        # ── activity log ──────────────────────────────────────────────────────
        items = [
            html.Div(msg, style={'fontSize': '12px', 'color': _MUTED,
                                 'fontFamily': 'monospace', 'padding': '2px 0'})
            for msg in reversed(_activity[-20:])
        ]
        activity = (items or
                    [html.Div('No activity yet.',
                              style={'color': _MUTED, 'fontStyle': 'italic',
                                     'padding': '4px'})])

        return engine_table, activity

    return app


def main() -> None:
    Path('logs').mkdir(exist_ok=True)
    if not STRATEGIES:
        logger.warning('No strategies found in strategies/ — check the folder.')
    else:
        logger.info('Strategies loaded: %s', list(STRATEGIES))
    logger.info('Control panel → http://127.0.0.1:%s', PORT)
    _log('Control panel started.')
    app = _build_app()
    app.run(host='127.0.0.1', port=PORT, debug=False)


if __name__ == '__main__':
    main()
