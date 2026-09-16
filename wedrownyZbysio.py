import logging

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - [%(filename)s] - %(message)s"
)
logger = logging.getLogger(__name__)
logging.getLogger('ibkr').setLevel(logging.INFO)
logging.getLogger('matplotlib').setLevel(logging.WARNING)
logging.getLogger('ibapi').setLevel(logging.WARNING)  # silence the IB API's per-second socket/queue debug spam

import datetime
import importlib
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import matplotlib.pyplot as plt
import mplfinance as mpf
from ibkr import IBKRGateway
import positions_observer as po
import params_lookup
from signal_checks import check_vol_price_body, check_vol_price_body_dir, scan_trailing_stop, scan_take_profit
import time
from logging_functions import init_trade_log, make_fill_handler, init_signal_log, log_signal_csv, EXCHANGE_TZ
from common import RED, GREEN, YELLOW, BLUE, CYAN, WHITE, RESET, timeframe_to_seconds

CLIENT_ID=79

CONFIG_MODULE = 'tuner1_found_params6'  # swap to e.g. 'tuner1_found_params' to trade tuner-found params instead

CHECK_INTERVAL = 100  # sekundy pomiędzy sprawdzeniem połączenia
TIMEFRAME = '10m'
FILL_TIMEOUT = 10
LIVE_TRADING = True
USE_DIRECTIONAL_PARAMS = True  # if True, trade long/short with independently-tuned entry & exit
                                 # params (config.PARAMS' *_long/*_short keys, see tuner1.TUNE_DIRECTIONAL);
                                 # if False (default), today's behavior — one shared param set for both.
#FIXED_TRAIL_STOP_PCT = 0.5  # experiment: overrides the tuned/dynamic trail_stop_loss with a fixed value
CLOSE_OVERNIGHT = True  # if True, flatten every open position shortly before its session closes — no overnight holds
CLOSE_BEFORE_SECONDS = 1800  # how far ahead of a session's close to flatten it, when CLOSE_OVERNIGHT is on
NO_ENTRY_BARS_BEFORE_FLATTEN = 2  # stop opening new positions this many bars before the flatten, so a
                                   # fresh entry always has room to develop instead of being force-closed
                                   # minutes later. In bars, not seconds, so it scales with TIMEFRAME.
LIVE_WINDOW_BARS = 12  # reqRealTimeBars() only ever hands back 5s bars; 12 of them = 1 minute
                        # between client-side trailing-stop / take-profit checks (per symbol)


# --- trading sessions ---------------------------------------------------------------------------
# One entry per market. Each session owns its own clock, currency and ticker list, and manages its
# own open → trade → flatten → closed cycle independently of the others (see session_phase() and
# main()'s sweep). They are NOT mutually exclusive: EU and US overlap ~90min year-round, and in
# summer EU opens an hour before Hong Kong closes — so the loop services every session that is
# currently tradeable rather than switching between them.
US_TICKERS = ('ELVN', 'HPQ', 'PYPL', 'CSCO', 'PATH', 'DIS', 'LUV', 'UAA', 'XP',
              'JD', 'ABNB', 'FRSH', 'TMO', 'HALO', 'ISRG')
# NOTE: IBKR wants its own symbols here, not Yahoo-style ones — 'SIE', not 'SIE.DE'. Verify each
# against TWS before trading it; an unresolvable contract just returns no data (see fetch note below).
EU_TICKERS = ('AIXA', 'BESI', 'ASML', 'IFX', 'AMS')
# Hong Kong symbols are numeric in IBKR ('700' = Tencent, '5' = HSBC). Left empty on purpose so the
# session stays inert until you've confirmed data entitlement + contract resolution for real tickers.
ASIA_TICKERS = ()


@dataclass(frozen=True)
class Session:
    """One market's trading session. open_time/close_time are LOCAL times in `tz`, so DST is handled
    automatically by ZoneInfo — each market switches on its own dates (US and EU differ by ~2 weeks,
    Hong Kong has no DST at all)."""
    name: str
    tz: ZoneInfo
    open_time: datetime.time
    close_time: datetime.time
    currency: str
    tickers: tuple[str, ...]
    cash_per_trade: float       # notional per entry, in `currency` — converted to whole shares at
                                 # the latest price (see shares_for_cash); replaces a fixed share count,
                                 # which would mean wildly different risk per market.
    exchange: str = 'SMART'
    primary_exchange: str = ''  # disambiguates a symbol when SMART alone can't ('SEHK', 'IBIS', ...)
    tick_size: float = 0.0      # 0 = let IBKR round; set it if a venue needs explicit tick rounding


SESSIONS: tuple[Session, ...] = (
    Session('US', ZoneInfo('America/New_York'), datetime.time(9, 30), datetime.time(16, 0),
            'USD', US_TICKERS, cash_per_trade=1000.0),
    Session('EU', ZoneInfo('Europe/Berlin'), datetime.time(9, 0), datetime.time(17, 30),
            'EUR', EU_TICKERS, cash_per_trade=1000.0, primary_exchange='IBIS'),
    Session('ASIA', ZoneInfo('Asia/Hong_Kong'), datetime.time(9, 30), datetime.time(16, 0),
            'HKD', ASIA_TICKERS, cash_per_trade=8000.0, primary_exchange='SEHK'),
)
ACTIVE_SESSIONS = tuple(s for s in SESSIONS if s.tickers)  # empty ticker lists stay dormant
ALL_TICKERS = tuple(t for s in ACTIVE_SESSIONS for t in s.tickers)
SESSION_BY_TICKER = {t: s for s in ACTIVE_SESSIONS for t in s.tickers}
# -------------------------------------------------------------------------------------------------

LOG_SUFFIX = f"{datetime.datetime.now(EXCHANGE_TZ).strftime('%Y%m%d_%H%M')}_{TIMEFRAME}"
TRADE_LOG = Path(f'logs/trades_{LOG_SUFFIX}.csv')
SIGNAL_LOG = Path(f'logs/signals_{LOG_SUFFIX}.csv')

# Usage: result = execute_trade(gw, 'RKLB', 'BUY', contract, 10, 0.5, 10, positions)
# Places the entry (+ a broker-side TRAIL as a safety net) and returns (entry_trade, trail_trade)
# once filled, or None if the signal was skipped (no signal / already in position / didn't fill).
def execute_trade(gw: IBKRGateway, symbol: str, signal: str, contract, quantity: int, trail_stop_loss: float, fill_timeout: float, positions: list):
    if not signal:
        return None
    already_in_position = any(getattr(p.contract, 'symbol', None) == symbol for p in positions)

    if already_in_position:
        logger.info(f'{YELLOW}Signal {signal} skipped — already in position.{RESET}')
        return None

    entry, tp, trail = gw.place_bracket_trailing(
        contract,
        action=signal,
        quantity=quantity,
        trail_percent=trail_stop_loss,
        fill_timeout=fill_timeout,
    )
    if trail is None:
        return None  # entry didn't fill
    po.add_position(symbol, entry.orderStatus.filled, signal, entry.orderStatus.avgFillPrice, contract)
    return entry, trail

def round_to_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 10)

def get_tick_size(price: float, currency: str) -> float:
    if currency == 'USD':
        return 0.0001 if price < 1.0 else 0.01
    if price < 10:   return 0.01
    if price < 100:  return 0.05
    if price < 500:  return 0.10
    if price < 1000: return 0.50
    return 1.00

# Usage: local_date = session_date(session, time.time())
def session_date(session: Session, now: float) -> datetime.date:
    """The calendar date `now` falls on in this session's own timezone — the key used to remember
    'already flattened this session today', so each market rolls over on its own local midnight."""
    return datetime.datetime.fromtimestamp(now, tz=session.tz).date()

# Usage: opening_time = session_open_ts(session, time.time())
def session_open_ts(session: Session, now: float) -> float:
    """This session's open today, as a Unix timestamp comparable to time.time()."""
    local_date = session_date(session, now)
    return datetime.datetime.combine(local_date, session.open_time, tzinfo=session.tz).timestamp()

# Usage: closing_time = session_close_ts(session, time.time())
def session_close_ts(session: Session, now: float) -> float:
    """This session's close today, as a Unix timestamp comparable to time.time()."""
    local_date = session_date(session, now)
    return datetime.datetime.combine(local_date, session.close_time, tzinfo=session.tz).timestamp()

# Usage: phase = session_phase(session, time.time(), tf_seconds)
def session_phase(session: Session, now: float, bar_seconds: float) -> str:
    """Where `now` sits in this session's day. The single source of truth for what the main loop is
    allowed to do — deliberately pure, so it can be tested without touching IBKR:
      'closed'    — outside the session entirely; don't poll, don't hold a subscription.
      'warmup'    — open, but inside the first bar. Poll and log signals so the signal CSV stays
                    continuous, but don't enter: the first bar of the day is still forming.
      'trading'   — normal operation, entries allowed.
      'wind_down' — mirror image of 'warmup' at the other end of the day: close enough to the flatten
                    that a new position couldn't develop before being force-closed. Keep polling and
                    keep managing open positions (the session stays subscribed, so the client-side
                    take-profit / trailing-stop checks keep firing) — just don't open anything new.
      'flatten'   — the last CLOSE_BEFORE_SECONDS before the close. Flatten this session's positions
                    once and stop entirely, so nothing is carried overnight."""
    open_ts, close_ts = session_open_ts(session, now), session_close_ts(session, now)
    if not (open_ts <= now < close_ts):
        return 'closed'
    if now < open_ts + bar_seconds:
        return 'warmup'
    if CLOSE_OVERNIGHT:
        flatten_ts = close_ts - CLOSE_BEFORE_SECONDS
        if now >= flatten_ts:
            return 'flatten'
        if now >= flatten_ts - NO_ENTRY_BARS_BEFORE_FLATTEN * bar_seconds:
            return 'wind_down'
    return 'trading'

# Usage: qty = shares_for_cash(1000.0, 187.35)
def shares_for_cash(cash: float, price: float) -> int:
    """Whole shares that `cash` buys at `price` — how position size is set, instead of a fixed share
    count, so a trade risks a comparable amount whether it's a 15 EUR or a 500 USD instrument.
    Returns 0 when a single share costs more than the budget; the caller skips the trade."""
    if price <= 0:
        return 0
    return int(cash // price)

# Usage: close_positions(gw, symbols=session.tickers)
# Flattens open positions via gw.close_position — used ahead of a session's close so nothing is held
# overnight. `symbols` scopes it to one session's tickers; None flattens everything (shutdown, panic).
# close_position waits for the fill, so a non-'Filled' status here means the position is still open
# and its trail/TP was already cancelled — i.e. naked. Surfaced loudly on purpose.
def close_positions(gw: IBKRGateway, symbols=None) -> None:
    wanted = set(symbols) if symbols is not None else None
    for p in gw.get_positions():
        if p.position == 0:
            continue
        symbol = p.contract.symbol
        if wanted is not None and symbol not in wanted:
            continue
        try:
            trade = gw.close_position(p.contract)
            if trade.orderStatus.status == 'Filled':
                logger.info(f'{YELLOW}Closed {symbol} ahead of exchange close.{RESET}')
            else:
                logger.error(f'{RED}{symbol} did NOT close (status={trade.orderStatus.status}) — position is open and unprotected.{RESET}')
        except ValueError as e:
            logger.warning(f'{YELLOW}Could not close {symbol}: {e}{RESET}')
    po.sync_with_ibkr(gw.get_positions())

# Usage: df = fetch_data_from_IBKR(gw, contracts['HPQ'], '3000 S', '10m', use_rth=True)
# Takes the already-built contract rather than a symbol + currency, so the bars we make decisions on
# and the orders we send come from the exact same contract definition — which matters outside the US,
# where SMART routing alone often can't resolve a symbol without primaryExchange.
def fetch_data_from_IBKR(gw: IBKRGateway, contract, duration: str = '1 D', bar_size: str = '5m', use_rth: bool = False):
    symbol = contract.symbol
    bars = gw.fetch_historical(contract, duration=duration, bar_size=bar_size, use_rth=use_rth)
    if not bars:
        # Outside the US this is the usual symptom of a missing market-data entitlement or a contract
        # that didn't resolve — both look identical here (empty result, no error).
        logger.error(f'No data returned for {symbol} ({contract.currency}).')
        return

    df = pd.DataFrame([{
        'Date': b.date, 'Open': b.open, 'High': b.high,
        'Low': b.low, 'Close': b.close, 'Volume': b.volume,
    } for b in bars])
    df['Date'] = pd.to_datetime(df['Date'])
    df.set_index('Date', inplace=True)

    return df

def plot_candles_and_mean(df: pd.DataFrame, mean_price: float, mean_volume: float):
    ap_price  = mpf.make_addplot([mean_price]  * len(df), panel=0, color='blue',  linestyle='--', width=1)
    ap_volume = mpf.make_addplot([mean_volume] * len(df), panel=1, color='orange', linestyle='--', width=1)

    fig, axes = mpf.plot(df, type='candle', volume=True, title='Data', style='charles',
         figsize=(12, 8),
         addplot=[ap_price, ap_volume],
         returnfig=True)
    plt.show(block=False)
    return fig, axes


def main():
    gw = IBKRGateway(client_id=CLIENT_ID)
    logging.info("Connecting to IBKR...")

    if not gw.ensure_connected():
        logger.error(f'{RED}Could not connect to IBKR. Is the Gateway/TWS running?{RESET}')
        return
    po.start_dashboard()
    po.sync_with_ibkr(gw.get_positions())
    def _on_ibkr_error(reqId, code, msg):
        # codes >= 2000 are connection/system info; 202 = order cancelled confirmation
        if code >= 2000 or code == 202:
            logger.debug(f'IBKR info {code} (reqId={reqId}): {msg}')
        else:
            logger.error(f'{RED}IBKR error {code} (reqId={reqId}): {msg}{RESET}')
    gw.on_error(_on_ibkr_error)
    #logging data
    init_trade_log(TRADE_LOG)
    init_signal_log(SIGNAL_LOG)
    gw.on_fill(make_fill_handler(TRADE_LOG, ''))

    # Client-side trailing-stop state, one entry per symbol currently in a trade — absent/None
    # while flat. Tracks the running peak/trough (see signal_checks.scan_trailing_stop) across
    # repeated live-bar checks. The broker-side TRAIL order placed by execute_trade() stays in
    # place as a safety net; this is a second, tighter check evaluated every LIVE_WINDOW_BARS
    # live bars (~1 minute) per symbol.
    open_trades: dict[str, dict] = {}
    live_bars: dict[str, deque] = {sym: deque(maxlen=LIVE_WINDOW_BARS) for sym in ALL_TICKERS}
    symbol_by_req_id: dict[int, str] = {}
    realtime_req_id_by_symbol: dict[str, int] = {}
    subscribed_sessions: set[str] = set()  # session names currently holding live-bar subscriptions

    # Symbols _on_realtime_bar wants closed, drained by the main loop below (not by
    # _on_realtime_bar itself). _on_realtime_bar runs on ibapi's single message-dispatch thread —
    # the same thread that would have to process a close order's response — so a synchronous,
    # response-waiting call like gw.close_position() made from inside it can never receive that
    # response (the thread is busy waiting for it instead of reading it) and always times out
    # after 10s, misreported as "No open position". Flagging here and closing from the main
    # thread's own loop avoids that self-deadlock entirely.
    pending_closes: set[str] = set()
    pending_closes_lock = threading.Lock()


    #zapisuje kazdy fill do logu i usuwa z open_trades jesli to exit fill
    def _on_fill(trade, fill):
        logger.info(
            f'FILL: {fill.execution.side} {fill.execution.shares} {trade.contract.symbol} '
            f'@ {fill.execution.avgPrice:.4f} | orderId={fill.execution.orderId}'
        )
        symbol = trade.contract.symbol
        open_trade = open_trades.get(symbol)
        if open_trade is not None and fill.execution.orderId != open_trade['entry_order_id']:
            logger.info(f'{YELLOW}Exit fill detected for {symbol} (orderId={fill.execution.orderId}) — clearing trade state.{RESET}')
            del open_trades[symbol]

    gw.on_fill(_on_fill)

    # Fires na kazdym 5-sekundowym barze, aktualizuje trailing stop i sprawdza take-profit. Wykonuje zamkniecie pozycji jesli warunki sa spelnione.
    def _on_realtime_bar(reqId, bar):
        symbol = symbol_by_req_id.get(reqId)
        if symbol is None:
            return
        live_bars[symbol].append(bar)
        trade = open_trades.get(symbol)
        if trade is None:
            return
        trade['bars_since_check'] += 1
        if trade['bars_since_check'] < LIVE_WINDOW_BARS:
            return
        trade['bars_since_check'] = 0

        window_df = pd.DataFrame([{
            'Date': b.date, 'Open': b.open, 'High': b.high,
            'Low': b.low, 'Close': b.close, 'Volume': b.volume,
        } for b in live_bars[symbol]])
        window_df['Date'] = pd.to_datetime(window_df['Date'])
        window_df.set_index('Date', inplace=True)

        # Take-profit first — if it fires, skip the trailing-stop check this tick, there's
        # nothing left to trail.
        tp_time, tp_price = scan_take_profit(
            window_df, trade['entry_time'], trade['entry_price'], trade['direction'], trade['take_profit_pct'],
        )
        if tp_price is not None:
            logger.info(f'{GREEN}Client-side take-profit hit for {symbol} at {tp_price:.4f} — '
                        f'flagging for close on the main loop.{RESET}')
            with pending_closes_lock:
                pending_closes.add(symbol)
            return

        extreme, exit_time, exit_price = scan_trailing_stop(
            window_df, trade['entry_time'], trade['entry_price'], trade['direction'],
            trade['trail_stop_loss'], extreme=trade['extreme'],
        )
        trade['extreme'] = extreme
        if exit_price is not None:
            logger.info(f'{YELLOW}Client-side trailing stop hit for {symbol} at {exit_price:.4f} '
                        f'(extreme={extreme:.4f}) — flagging for close on the main loop.{RESET}')
            with pending_closes_lock:
                pending_closes.add(symbol)

    gw.on_realtime_bar(_on_realtime_bar) #appends the function to the list of callbacks

    try:
        #1. Pobiera parametry strategii z configs.py:
        config     = importlib.import_module(CONFIG_MODULE)
        pd.set_option('display.max_rows', None)

        #2. Calculating timings for fetching data:
        tf_seconds = timeframe_to_seconds(TIMEFRAME)
        fetch_interval = tf_seconds                        # fetch once per bar

        logger.debug(f'Monitoruję połączenie co {CHECK_INTERVAL} [s]. Wciśnij Ctrl+C aby zakończyć działanie programu.')
        # Per-session now, not global: each market opens, trades and flattens on its own clock, so
        # every one of these needs its own slot keyed by session name.
        last_fetch: dict[str, float] = {s.name: 0.0 for s in ACTIVE_SESSIONS}
        closed_overnight_on: dict[str, datetime.date] = {}
        last_processed_candle = {sym: None for sym in ALL_TICKERS}

        # One contract per ticker, built from its own session's currency/exchange. primaryExchange is
        # set post-construction rather than via make_stock_contract() so ibkr.py stays untouched —
        # rocket_janek.py is live against it and this file shouldn't be able to break that.
        contracts = {}
        for s in ACTIVE_SESSIONS:
            for sym in s.tickers:
                c = gw.make_stock_contract(sym, exchange=s.exchange, currency=s.currency)
                if s.primary_exchange:
                    c.primaryExchange = s.primary_exchange
                contracts[sym] = c

        #2b. Live 5s bars feed the client-side trailing-stop / take-profit check in
        # _on_realtime_bar(). Subscribed per session when it opens and dropped when it flattens,
        # rather than all-at-once at startup: IBKR caps concurrent market-data lines, and a
        # subscription outside its own RTH delivers nothing anyway.
        def subscribe_session(session: Session) -> None:
            if session.name in subscribed_sessions:
                return
            for sym in session.tickers:
                req_id = gw.start_realtime_bars(contracts[sym], what_to_show='TRADES', use_rth=True)
                symbol_by_req_id[req_id] = sym
                realtime_req_id_by_symbol[sym] = req_id
            subscribed_sessions.add(session.name)
            logger.info(f'{BLUE}[{session.name}] subscribed to live 5s bars for {len(session.tickers)} symbols.{RESET}')

        def unsubscribe_session(session: Session) -> None:
            if session.name not in subscribed_sessions:
                return
            for sym in session.tickers:
                req_id = realtime_req_id_by_symbol.pop(sym, None)
                if req_id is not None:
                    gw.stop_realtime_bars(req_id)
                    symbol_by_req_id.pop(req_id, None)
                live_bars[sym].clear()  # stale bars would poison the next session's first checks
            subscribed_sessions.discard(session.name)
            logger.info(f'{BLUE}[{session.name}] unsubscribed from live 5s bars.{RESET}')

        while True:
            time.sleep(CHECK_INTERVAL)
            if not gw.ensure_connected():
                logger.error('Lost connection and could not reconnect. Exiting.')
                break
            #logger.debug('...')

            # Close whatever _on_realtime_bar flagged since the last pass — safe here, this runs
            # on the main thread, not ibapi's message-dispatch thread (see pending_closes' comment
            # above for why that distinction matters). Runs every CHECK_INTERVAL, independent of
            # the slower per-symbol fetch cadence below, so a flagged close isn't held up by it.
            with pending_closes_lock:
                to_close = list(pending_closes)
                pending_closes.difference_update(to_close)
            for symbol in to_close:
                try:
                    gw.close_position(contracts[symbol])
                except ValueError as e:
                    logger.warning(f'{YELLOW}Could not close {symbol}: {e}{RESET}')

            now = time.time()

            # Service every session that has something to do this tick. They're independent on
            # purpose — EU and US overlap ~90min year-round, and in summer EU opens an hour before
            # Hong Kong closes, so there is no single "current session" to switch to.
            for session in ACTIVE_SESSIONS:
                phase = session_phase(session, now, tf_seconds)

                if phase == 'closed':
                    unsubscribe_session(session)
                    continue

                if phase == 'flatten':
                    today = session_date(session, now)
                    if CLOSE_OVERNIGHT and closed_overnight_on.get(session.name) != today:
                        logger.info(f'{YELLOW}[{session.name}] CLOSE_OVERNIGHT: flattening '
                                    f'({CLOSE_BEFORE_SECONDS // 60}min to close).{RESET}')
                        close_positions(gw, symbols=session.tickers)
                        closed_overnight_on[session.name] = today
                        # Drop any flags still queued for this session — the positions behind them
                        # are gone, so draining them would just log "No open position" warnings.
                        with pending_closes_lock:
                            pending_closes.difference_update(session.tickers)
                    unsubscribe_session(session)
                    continue

                # 'warmup', 'trading' or 'wind_down' — all three poll and log signals so the signal
                # CSV stays continuous, and all three stay subscribed so open positions keep being
                # managed. Only 'trading' is allowed to open new positions.
                subscribe_session(session)
                if now - last_fetch[session.name] < fetch_interval:
                    continue
                entries_allowed = phase == 'trading'
                positions = gw.get_positions()
                for symbol in session.tickers:
                  try:
                    #3. Parametry per-symbol — każdy symbol ma własną konfigurację:
                    params = params_lookup.get_params(config.PARAMS, 'MomentumV8Strategy', symbol, TIMEFRAME)
                    vol_len = params.get('vol_len', 10)  # shared for both directions regardless of USE_DIRECTIONAL_PARAMS
                    duration = f'{vol_len * tf_seconds} S'          # enough bars to fill vol_len

                    if USE_DIRECTIONAL_PARAMS:
                        # Each *_long/*_short key falls back to the plain (non-suffixed) key, so an
                        # older, non-directional params file still works — both directions just get
                        # the same value, identical to USE_DIRECTIONAL_PARAMS=False's behavior.
                        long_params = {
                            'vol_multiplier': params.get('vol_multiplier_long', params.get('vol_multiplier', 1.8)),
                            'price_move_pct': params.get('price_move_pct_long', params.get('price_move_pct', 1.5)),
                            'body_ratio_threshold': params.get('body_ratio_threshold_long', params.get('body_ratio_threshold', 0.5)),
                            'trail_stop_pct': params.get('trail_stop_pct_long', params.get('trail_stop_pct', 1.0)),
                            'take_profit_pct': params.get('take_profit_pct_long', params.get('take_profit_pct', 2.0)),
                        }
                        short_params = {
                            'vol_multiplier': params.get('vol_multiplier_short', params.get('vol_multiplier', 1.8)),
                            'price_move_pct': params.get('price_move_pct_short', params.get('price_move_pct', 1.5)),
                            'body_ratio_threshold': params.get('body_ratio_threshold_short', params.get('body_ratio_threshold', 0.5)),
                            'trail_stop_pct': params.get('trail_stop_pct_short', params.get('trail_stop_pct', 1.0)),
                            'take_profit_pct': params.get('take_profit_pct_short', params.get('take_profit_pct', 2.0)),
                        }
                        logger.debug(f"{YELLOW}{symbol}: vol_len={vol_len}, long={long_params}, short={short_params}{RESET}")
                    else:
                        vol_multiplier = params.get('vol_multiplier', 1.8)
                        price_move_pct = params.get('price_move_pct', 1.5)
                        trail_stop_pct = params.get('trail_stop_pct', 1.0)
                        body_ratio_threshold = params.get('body_ratio_threshold', 0.5)
                        take_profit_pct = params.get('take_profit_pct', 2.0)
                        logger.debug(f"{YELLOW}{symbol}: vol_len={vol_len}, vol_multiplier={vol_multiplier}, price_move_pct={price_move_pct}, trail_stop_pct={trail_stop_pct}, take_profit_pct={take_profit_pct},body_ratio_threshold={body_ratio_threshold}{RESET}")

                    #4. Ściągnij dane z IBKR
                    df = fetch_data_from_IBKR(gw, contracts[symbol], duration, TIMEFRAME, use_rth=True)
                    if df is None:
                        logger.warning(f'{YELLOW}No data for {symbol}, skipping.{RESET}')
                        continue
                    df = df.tail(vol_len).copy()
                    last_price = df['Close'].iloc[-1]
                    po.update_current_price(symbol, last_price)

                    #5. Sprawdź czy świeca już była przetworzona, jeśli tak to pomiń logikę wejścia
                    candle_time = df.iloc[-2].name
                    if candle_time == last_processed_candle[symbol]:
                        logger.debug(f'{YELLOW}{symbol}: candle {candle_time} already processed, skipping.{RESET}')
                        continue

                    #6. Entry logic
                    if USE_DIRECTIONAL_PARAMS:
                        signal, _, debug, flags = check_vol_price_body_dir(df, long_params, short_params)
                    else:
                        signal, _, trail_stop_loss, debug, flags = check_vol_price_body(df, vol_multiplier, price_move_pct, trail_stop_pct, body_ratio_threshold)
                        #trail_stop_loss = FIXED_TRAIL_STOP_PCT  # experiment: fixed tight stop instead of the tuned/dynamic one
                    log_signal_csv(SIGNAL_LOG, symbol, signal, debug, flags)
                    if not LIVE_TRADING:
                        logger.debug(f'{YELLOW}{symbol}: LIVE_TRADING is off, skipping entry.{RESET}')
                    elif not entries_allowed:
                        logger.debug(f'{YELLOW}{symbol}: [{session.name}] phase={phase} — polling and '
                                     f'managing open positions only, no new entries.{RESET}')
                    else:
                        # Resolve the actual exit params to trade with — the direction-specific dict
                        # once `signal` (hence direction) is known, or today's flat values otherwise.
                        if USE_DIRECTIONAL_PARAMS:
                            p = long_params if signal == 'BUY' else short_params
                            trail_stop_used, take_profit_used = p['trail_stop_pct'], p['take_profit_pct']
                        else:
                            trail_stop_used, take_profit_used = trail_stop_loss, take_profit_pct
                        # Size by money, not by share count: cash_per_trade is in the session's own
                        # currency, so 1000 USD of a US name and 1000 EUR of a European one carry
                        # comparable risk — a flat 10 shares would not.
                        quantity = shares_for_cash(session.cash_per_trade, last_price)
                        if signal and quantity < 1:
                            logger.warning(f'{YELLOW}{symbol}: one share costs {last_price:.2f} {session.currency}, '
                                           f'over the {session.cash_per_trade:.0f} {session.currency} budget — skipping.{RESET}')
                            last_processed_candle[symbol] = candle_time
                            continue
                        result = execute_trade(gw, symbol, signal, contracts[symbol], quantity, trail_stop_used, FILL_TIMEOUT, positions)
                        if result is not None:
                            entry, trail = result
                            open_trades[symbol] = {
                                'contract': contracts[symbol],
                                'direction': 'long' if signal == 'BUY' else 'short',
                                'entry_price': entry.orderStatus.avgFillPrice,
                                'entry_time': datetime.datetime.now(datetime.timezone.utc),
                                'entry_order_id': entry.order.orderId,
                                'trail_stop_loss': trail_stop_used,
                                'extreme': entry.orderStatus.avgFillPrice,
                                'take_profit_pct': take_profit_used,
                                'bars_since_check': 0,
                            }
                    last_processed_candle[symbol] = candle_time
                  except ConnectionError as e:
                    logger.error(f'{RED}{symbol}: connection error ({e}) — skipping this pass.{RESET}')
                    continue

                #7. Print positions
                current_positions = gw.get_positions()
                po.sync_with_ibkr(current_positions)
                if current_positions:
                    for p in current_positions:
                        logger.debug(f'{BLUE}Position: {p}{RESET}')
                else:
                    logger.debug(f'{YELLOW}No open positions.{RESET}')
                last_fetch[session.name] = now
                #logger.debug(f'Next fetch in 300s at {time.strftime("%H:%M:%S", time.localtime(last_fetch + 300))}')

    except KeyboardInterrupt:
        logger.info('Stopped by user.')
    finally:
        for req_id in realtime_req_id_by_symbol.values():
            gw.stop_realtime_bars(req_id)
        gw.disconnect()

if __name__ == '__main__':
    main()
