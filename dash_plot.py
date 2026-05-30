import logging
import queue
import threading
from collections import defaultdict

import pandas as pd
import plotly.graph_objs as go
from plotly.subplots import make_subplots
from flask import jsonify
from flask import request as flask_request
from dash import Dash, Input, Output, dcc, html

logger = logging.getLogger(__name__)

# Visual style for each of the 12 standard trading actions.
# Keys are the action strings accepted by the /trade endpoint.
# symbol: Plotly marker symbol; color: hex; size: px; label: legend entry.
# Long/short variants of the same action share a colour family but differ in symbol
# (filled vs open) so they remain distinguishable when both appear on the same chart.
MARKER_STYLES: dict[str, dict] = {
    # ── entries ─────────────────────────────────────────────────────────────
    'enter_long':          dict(symbol='triangle-up',   color='#00c853', size=14, label='Enter Long'),
    'enter_short':         dict(symbol='triangle-down', color='#d50000', size=14, label='Enter Short'),
    # ── long exits ──────────────────────────────────────────────────────────
    'exit_long_sl':        dict(symbol='x',             color='#ef5350', size=13, label='Exit Long – Stop Loss'),
    'exit_long_tp':        dict(symbol='star',          color='#69f0ae', size=14, label='Exit Long – Take Profit'),
    'exit_long_tsl':       dict(symbol='circle',        color='#ff9800', size=12, label='Exit Long – Trailing Stop'),
    'exit_long_special':   dict(symbol='diamond',       color='#00bcd4', size=12, label='Exit Long – Special'),
    # ── short exits ─────────────────────────────────────────────────────────
    'exit_short_sl':       dict(symbol='cross',         color='#ef5350', size=13, label='Exit Short – Stop Loss'),
    'exit_short_tp':       dict(symbol='star-open',     color='#69f0ae', size=14, label='Exit Short – Take Profit'),
    'exit_short_tsl':      dict(symbol='circle-open',   color='#ff9800', size=12, label='Exit Short – Trailing Stop'),
    'exit_short_special':  dict(symbol='diamond-open',  color='#00bcd4', size=12, label='Exit Short – Special'),
    # ── reversals ───────────────────────────────────────────────────────────
    'reverse_short_long':  dict(symbol='arrow-up',      color='#ce93d8', size=16, label='Reverse → Long'),
    'reverse_long_short':  dict(symbol='arrow-down',    color='#ce93d8', size=16, label='Reverse → Short'),
}

_WAITING = go.Figure(layout=go.Layout(
    paper_bgcolor='#1e1e1e', plot_bgcolor='#1e1e1e',
    font=dict(color='#aaa'),
    annotations=[dict(text='Waiting for data…', showarrow=False,
                      font=dict(size=18, color='#666'))],
))


class DashPlotter:
    def __init__(self, title: str = 'Live Chart', host: str = '127.0.0.1',
                 port: int = 8050, display_hours: float = 0, display_bars: int = 0):
        self.title        = title
        self.host         = host
        self.port         = port
        self.display_hours = display_hours  # 0 = show all data
        self.display_bars  = display_bars   # 0 = show all; >0 clips to last N candles

        self._queue:  queue.Queue              = queue.Queue()
        self._dfs:    dict[str, pd.DataFrame]  = {}   # one DataFrame per symbol
        self._trades: list[dict]               = []   # markers from POST /trade
        self._lock    = threading.Lock()
        self._thread: threading.Thread | None  = None
        self._app = self._build_app()

    # Creates the Dash layout with a symbol dropdown, candlestick+volume chart,
    # and a 1-second Interval.  The refresh callback populates the dropdown options
    # dynamically as new symbols arrive, and filters the chart to the selected one.
    def _build_app(self) -> Dash:
        app = Dash(__name__)
        app.layout = html.Div([
            html.Div([
                html.H3(self.title,
                        style={'fontFamily': 'sans-serif', 'margin': '12px 0 0 16px',
                               'display': 'inline-block'}),
                dcc.Dropdown(
                    id='symbol-select',
                    options=[],
                    value=None,
                    clearable=False,
                    style={'width': '180px', 'marginLeft': '24px',
                           'verticalAlign': 'middle', 'display': 'inline-block'},
                ),
            ], style={'display': 'flex', 'alignItems': 'center', 'marginBottom': '4px'}),
            dcc.Graph(id='chart', style={'height': '75vh'}, config={
                'modeBarButtonsToAdd': [
                    'drawline',
                    'drawopenpath',
                    'drawrect',
                    'drawcircle',
                    'eraseshape',
                ],
                'scrollZoom': True,
            }),
            dcc.Interval(id='interval', interval=1000, n_intervals=0),
        ])

        @app.callback(
            Output('chart',         'figure'),
            Output('symbol-select', 'options'),
            Output('symbol-select', 'value'),
            Input('interval',       'n_intervals'),
            Input('symbol-select',  'value'),
        )
        def refresh(_n, selected):
            self._drain_queue()
            with self._lock:
                dfs    = {k: v.copy() for k, v in self._dfs.items()}
                trades = list(self._trades)

            options = [{'label': s, 'value': s} for s in sorted(dfs)]

            # pick a valid symbol: keep current selection or fall back to first available
            sym = selected if selected in dfs else (sorted(dfs)[0] if dfs else None)

            if sym is None or dfs.get(sym, pd.DataFrame()).empty:
                return _WAITING, options, sym

            sym_trades = [t for t in trades if t.get('symbol', sym) == sym]
            fig = _candlestick_figure(dfs[sym], self.display_hours,
                                      self.display_bars, sym_trades, sym)
            return fig, options, sym

        # Any external process can POST
        #   {"action": "enter_long", "price": 73500.0, "date": "<ISO>", "symbol": "SOL/USD"}
        # to place a marker on the chart.  "symbol" defaults to the first available symbol.
        @app.server.route('/trade', methods=['POST'])
        def add_trade():
            data = flask_request.get_json(silent=True)
            if not data:
                return jsonify({'error': 'JSON body required'}), 400
            for field in ('action', 'price', 'date'):
                if field not in data:
                    return jsonify({'error': f"'{field}' is required"}), 400
            if data['action'] not in MARKER_STYLES:
                return jsonify({'error': f"unknown action '{data['action']}'; "
                                         f"valid: {list(MARKER_STYLES)}"}), 400
            with self._lock:
                # default symbol to first available if caller didn't specify
                if 'symbol' not in data:
                    data['symbol'] = sorted(self._dfs)[0] if self._dfs else 'default'
                self._trades.append(data)
            return jsonify({'status': 'ok'})

        # Returns the most recent close price for a symbol.
        # Optional query param: ?symbol=SOL/USD  (defaults to first available)
        @app.server.route('/last_price', methods=['GET'])
        def last_price():
            sym = flask_request.args.get('symbol')
            with self._lock:
                if not self._dfs:
                    return jsonify({'error': 'no data yet'}), 503
                if sym is None:
                    sym = sorted(self._dfs)[0]
                df = self._dfs.get(sym, pd.DataFrame())
                if df.empty:
                    return jsonify({'error': f'no data for {sym}'}), 503
                row = df.iloc[-1]
            return jsonify({'price': float(row['close']), 'date': str(row['date'])})

        return app

    # Drains all pending bar dicts from the queue and appends them to the correct
    # per-symbol DataFrame, deduplicating on `date` and re-sorting by time.
    def _drain_queue(self):
        rows_by_sym: dict[str, list] = defaultdict(list)
        while True:
            try:
                item = self._queue.get_nowait()
                sym  = item.pop('symbol', 'default')
                rows_by_sym[sym].append(item)
            except queue.Empty:
                break
        if not rows_by_sym:
            return
        with self._lock:
            for sym, rows in rows_by_sym.items():
                existing = self._dfs.get(sym, pd.DataFrame())
                self._dfs[sym] = pd.concat(
                    [existing, pd.DataFrame(rows)],
                    ignore_index=True,
                ).drop_duplicates(subset=['date'], keep='last').sort_values('date').reset_index(drop=True)

    # Enqueue a single OHLCV bar; safe to call from any thread at any time.
    # symbol identifies which per-symbol DataFrame the bar belongs to.
    def push(self, date, open_: float, high: float, low: float, close: float,
             volume: float = 0.0, symbol: str = 'default'):
        self._queue.put({'symbol': symbol, 'date': date, 'open': open_,
                         'high': high, 'low': low, 'close': close, 'volume': volume})

    # Convenience wrapper for ib_insync bar lists (must have .date/.open/.high/.low/.close).
    def push_bars(self, bars, symbol: str = 'default'):
        for bar in bars:
            self.push(bar.date, bar.open, bar.high, bar.low, bar.close,
                      getattr(bar, 'volume', 0.0), symbol=symbol)
        logger.info('Queued %d bars for %s', len(bars), symbol)

    # Launches the Dash/Flask server in a background daemon thread.
    # Calling start() a second time while the server is running is a no-op.
    def start(self):
        if self._thread and self._thread.is_alive():
            logger.warning('Dash plotter already running')
            return
        self._thread = threading.Thread(target=self._run_server, daemon=True, name='dash-plotter')
        self._thread.start()
        logger.info('Dash server starting at http://%s:%s', self.host, self.port)

    # Entry point for the daemon thread; mutes verbose werkzeug logs.
    def _run_server(self):
        logging.getLogger('werkzeug').setLevel(logging.WARNING)
        self._app.run(host=self.host, port=self.port, debug=False, use_reloader=False)


# Builds a dark-themed Plotly figure with a candlestick panel (top 75%) and a
# volume panel (bottom 25%) sharing the same x-axis.  Trade markers for the
# selected symbol are overlaid on the candlestick panel.
def _candlestick_figure(
    df: pd.DataFrame,
    display_hours: float = 0,
    display_bars: int = 0,
    trades: list[dict] | None = None,
    symbol: str = '',
) -> go.Figure:
    if display_bars > 0:
        df = df.iloc[-display_bars:]

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        row_heights=[0.75, 0.25],
        vertical_spacing=0.02,
    )

    fig.add_trace(go.Candlestick(
        x=df['date'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
        name='Price',
    ), row=1, col=1)

    vol_colors = ['#26a69a' if c >= o else '#ef5350'
                  for o, c in zip(df['open'], df['close'])]
    fig.add_trace(go.Bar(
        x=df['date'], y=df['volume'],
        marker_color=vol_colors, name='Volume', showlegend=False,
    ), row=2, col=1)

    xaxis_style = dict(showgrid=False, color='#888')
    if display_hours > 0:
        end   = pd.Timestamp(df['date'].max())
        start = end - pd.Timedelta(hours=display_hours)
        xaxis_style['range'] = [start, end]

    # group markers by action; filter to the visible time window
    if trades:
        visible_start = pd.Timestamp(df['date'].min())
        by_action: dict[str, list] = defaultdict(list)
        for t in trades:
            ts = pd.Timestamp(t['date'])
            if display_bars == 0 or ts >= visible_start:
                by_action[t['action']].append((ts, float(t['price'])))
        for action, points in by_action.items():
            style = MARKER_STYLES[action]
            xs, ys = zip(*points)
            fig.add_trace(go.Scatter(
                x=list(xs), y=list(ys), mode='markers',
                name=style['label'],
                marker=dict(symbol=style['symbol'], color=style['color'],
                            size=style['size'], line=dict(color='white', width=1)),
            ), row=1, col=1)

    fig.update_layout(
        uirevision=symbol or 'lock',   # per-symbol so zoom resets only on symbol switch
        xaxis_rangeslider_visible=False,
        paper_bgcolor='#1e1e1e', plot_bgcolor='#1e1e1e',
        font=dict(color='#ccc'),
        xaxis=xaxis_style,
        yaxis=dict(showgrid=True, gridcolor='#333', color='#888'),
        yaxis2=dict(showgrid=False, color='#888'),
        margin=dict(l=50, r=20, t=20, b=40),
        legend=dict(bgcolor='rgba(0,0,0,0)', font=dict(size=11)),
    )
    return fig
