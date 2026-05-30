import logging
import queue
import threading

import pandas as pd
import plotly.graph_objs as go
from dash import Dash, Input, Output, dcc, html

logger = logging.getLogger(__name__)


class DashPlotter:
    def __init__(self, title: str = 'Live Chart', host: str = '127.0.0.1', port: int = 8050, display_hours: float = 0, display_bars: int = 0):
        self.title = title
        self.host = host
        self.port = port
        self.display_hours = display_hours  # 0 = show all data
        self.display_bars = display_bars    # 0 = show all; >0 clips to last N candles (overrides display_hours)
        self._queue: queue.Queue = queue.Queue()
        self._df: pd.DataFrame = pd.DataFrame()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._app = self._build_app()

    # Creates the Dash app with an H3 title, a candlestick Graph, and a 1-second Interval
    # that fires the `refresh` callback to pull new bars from the queue and redraw the chart.
    def _build_app(self) -> Dash:
        app = Dash(__name__)
        app.layout = html.Div([
            html.H3(self.title, style={'fontFamily': 'sans-serif', 'margin': '12px 16px 0'}),
            dcc.Graph(id='chart', style={'height': '75vh'}),
            dcc.Interval(id='interval', interval=1000, n_intervals=0),
        ])

        @app.callback(Output('chart', 'figure'), Input('interval', 'n_intervals'))
        def refresh(_n):
            self._drain_queue()
            with self._lock:
                df = self._df.copy()
            if df.empty:
                return go.Figure(layout=go.Layout(
                    paper_bgcolor='#1e1e1e', plot_bgcolor='#1e1e1e',
                    font=dict(color='#aaa'),
                    annotations=[dict(text='Waiting for data…', showarrow=False, font=dict(size=18, color='#666'))],
                ))
            return _candlestick_figure(df, self.display_hours, self.display_bars)

        return app

    # Drains all pending bar dicts from the thread-safe queue, appends them to the internal
    # DataFrame, deduplicates on `date` (keeping the latest value), and re-sorts by time.
    def _drain_queue(self):
        rows = []
        while True:
            try:
                rows.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not rows:
            return
        with self._lock:
            self._df = pd.concat(
                [self._df, pd.DataFrame(rows)],
                ignore_index=True,
            ).drop_duplicates(subset=['date'], keep='last').sort_values('date').reset_index(drop=True)

    # Enqueues a single OHLCV bar dict; safe to call from any thread at any time.
    def push(self, date, open_: float, high: float, low: float, close: float, volume: float = 0.0):
        self._queue.put({'date': date, 'open': open_, 'high': high, 'low': low, 'close': close, 'volume': volume})

    # Convenience wrapper: iterates an iterable of bar objects (must have .date/.open/.high/.low/.close)
    # and calls push() for each one.
    def push_bars(self, bars):
        for bar in bars:
            self.push(bar.date, bar.open, bar.high, bar.low, bar.close, getattr(bar, 'volume', 0.0))
        logger.info('Queued %d bars for plotting', len(bars))

    # Launches the Dash/Flask server in a background daemon thread so it doesn't block the caller.
    # Calling start() a second time while the server is running is a no-op.
    def start(self):
        if self._thread and self._thread.is_alive():
            logger.warning('Dash plotter already running')
            return
        self._thread = threading.Thread(target=self._run_server, daemon=True, name='dash-plotter')
        self._thread.start()
        logger.info('Dash server starting at http://%s:%s', self.host, self.port)

    # Entry point for the daemon thread: mutes verbose werkzeug logs, then blocks inside
    # app.run() for the lifetime of the process (debug and reloader both off for threading safety).
    def _run_server(self):
        logging.getLogger('werkzeug').setLevel(logging.WARNING)
        self._app.run(host=self.host, port=self.port, debug=False, use_reloader=False)


# Builds a dark-themed Plotly candlestick figure from a DataFrame with columns
# [date, open, high, low, close].  When display_hours > 0, the x-axis is clamped to
# the most recent N hours so the chart doesn't compress historical data into view.
def _candlestick_figure(df: pd.DataFrame, display_hours: float = 0, display_bars: int = 0) -> go.Figure:
    # display_bars takes priority: slice the DataFrame to the most recent N rows
    if display_bars > 0:
        df = df.iloc[-display_bars:]
    fig = go.Figure(data=[go.Candlestick(
        x=df['date'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        increasing_line_color='#26a69a',
        decreasing_line_color='#ef5350',
    )])
    xaxis = dict(showgrid=False, color='#888')
    if display_hours > 0:
        end = pd.Timestamp(df['date'].max())
        start = end - pd.Timedelta(hours=display_hours)
        xaxis['range'] = [start, end]
    fig.update_layout(
        xaxis_rangeslider_visible=False,
        paper_bgcolor='#1e1e1e',
        plot_bgcolor='#1e1e1e',
        font=dict(color='#ccc'),
        xaxis=xaxis,
        yaxis=dict(showgrid=True, gridcolor='#333', color='#888'),
        margin=dict(l=50, r=20, t=20, b=40),
    )
    return fig
