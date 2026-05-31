"""
configs.py — Strategy parameter store.

Structure:  PARAMS[strategy_name][symbol][timeframe] = {param: value, ...}

Symbol format
─────────────
  ccxt  (crypto)  : 'SOL/USD', 'BTC/USD', 'ETH/USD'
  IBKR  (stocks)  : 'AAPL', 'TSLA', 'NVDA'
  IBKR  (forex)   : 'EURUSD', 'GBPUSD', 'USDJPY'

Only parameters that differ from the strategy's built-in defaults need to be
listed.  get_params() returns {} for any unknown combo, which makes the
strategy fall back to its own defaults automatically.

Adding a new symbol or timeframe: add one nested dict entry — no code changes.
"""

PARAMS: dict = {

    # ── MomentumV8 ────────────────────────────────────────────────────────────
    # High-volume candle momentum with trailing stop + hard stop.
    # Tune vol_multiplier up on noisy symbols; tighten stops on volatile ones.
    'MomentumV8Strategy': {

        # ── Crypto (ccxt) ──────────────────────────────────────────────────
        'SOL/USD': {
            '1m':  {'vol_len': 10, 'vol_multiplier': 1.1, 'price_move_pct': 0.10,
                    'trail_stop_pct': 0.10, 'stop_loss_pct': 0.20,
                    'entry_cutoff_hour': 23, 'entry_cutoff_min': 59},   # 24/7 crypto
            '5m':  {'vol_len': 10, 'vol_multiplier': 1.3, 'price_move_pct': 0.20,
                    'trail_stop_pct': 0.20, 'stop_loss_pct': 0.40,
                    'entry_cutoff_hour': 23, 'entry_cutoff_min': 59},
        },
        'BTC/USD': {
            '1m':  {'vol_len': 10, 'vol_multiplier': 1.2, 'price_move_pct': 0.05,
                    'trail_stop_pct': 0.05, 'stop_loss_pct': 0.10,
                    'entry_cutoff_hour': 23, 'entry_cutoff_min': 59},
            '5m':  {'vol_len': 10, 'vol_multiplier': 1.4, 'price_move_pct': 0.10,
                    'trail_stop_pct': 0.10, 'stop_loss_pct': 0.20,
                    'entry_cutoff_hour': 23, 'entry_cutoff_min': 59},
        },
        'ETH/USD': {
            '1m':  {'vol_len': 10, 'vol_multiplier': 1.2, 'price_move_pct': 0.08,
                    'trail_stop_pct': 0.08, 'stop_loss_pct': 0.16,
                    'entry_cutoff_hour': 23, 'entry_cutoff_min': 59},
        },

        # ── Stocks (IBKR) ──────────────────────────────────────────────────
        'AAPL': {
            '1m':  {'vol_len': 10, 'vol_multiplier': 1.5, 'price_move_pct': 0.10,
                    'trail_stop_pct': 0.10, 'stop_loss_pct': 0.20,
                    'allow_short': False},                              # long-only for stocks
            '5m':  {'vol_len': 20, 'vol_multiplier': 1.8, 'price_move_pct': 0.20,
                    'trail_stop_pct': 0.20, 'stop_loss_pct': 0.40,
                    'allow_short': False},
        },
        'TSLA': {
            '1m':  {'vol_len': 10, 'vol_multiplier': 2.0, 'price_move_pct': 0.20,
                    'trail_stop_pct': 0.20, 'stop_loss_pct': 0.50,
                    'allow_short': False},                              # TSLA is very volatile
        },
        'NVDA': {
            '1m':  {'vol_len': 10, 'vol_multiplier': 1.8, 'price_move_pct': 0.15,
                    'trail_stop_pct': 0.15, 'stop_loss_pct': 0.30,
                    'allow_short': False},
        },

        # ── Forex (IBKR) ───────────────────────────────────────────────────
        'EURUSD': {
            '1m':  {'vol_len': 15, 'vol_multiplier': 1.3, 'price_move_pct': 0.02,
                    'trail_stop_pct': 0.02, 'stop_loss_pct': 0.05},
            '5m':  {'vol_len': 15, 'vol_multiplier': 1.5, 'price_move_pct': 0.05,
                    'trail_stop_pct': 0.05, 'stop_loss_pct': 0.10},
        },
        'GBPUSD': {
            '1m':  {'vol_len': 15, 'vol_multiplier': 1.4, 'price_move_pct': 0.03,
                    'trail_stop_pct': 0.03, 'stop_loss_pct': 0.06},
        },
    },

    # ── MomentumV11 ───────────────────────────────────────────────────────────
    # V8 + ADX regime filter + body-quality filter + trail activation gate.
    # Raise adx_threshold on trending symbols; lower it on ranging ones.
    'MomentumV11Strategy': {

        # ── Crypto (ccxt) ──────────────────────────────────────────────────
        'SOL/USD': {
            '1m':  {'vol_len': 20, 'vol_multiplier': 1.7, 'price_move_pct': 1.1,
                    'adx_threshold': 16.0, 'trail_activate_pct': 0.3,
                    'trail_distance_pct': 0.15, 'stop_loss_pct': 0.20,
                    'entry_cutoff_hour': 23, 'entry_cutoff_min': 59},
            '5m':  {'vol_len': 20, 'vol_multiplier': 2.0, 'price_move_pct': 1.5,
                    'adx_threshold': 20.0, 'trail_activate_pct': 0.5,
                    'trail_distance_pct': 0.25, 'stop_loss_pct': 0.40,
                    'entry_cutoff_hour': 23, 'entry_cutoff_min': 59},
        },
        'BTC/USD': {
            '1m':  {'vol_len': 20, 'vol_multiplier': 1.5, 'price_move_pct': 0.5,
                    'adx_threshold': 18.0, 'trail_activate_pct': 0.2,
                    'trail_distance_pct': 0.10, 'stop_loss_pct': 0.15,
                    'entry_cutoff_hour': 23, 'entry_cutoff_min': 59},
        },

        # ── Stocks (IBKR) ──────────────────────────────────────────────────
        'AAPL': {
            '5m':  {'vol_len': 20, 'vol_multiplier': 2.0, 'price_move_pct': 0.30,
                    'adx_threshold': 20.0, 'trail_activate_pct': 0.3,
                    'trail_distance_pct': 0.15, 'stop_loss_pct': 0.30,
                    'allow_short': False},
        },
        'NVDA': {
            '5m':  {'vol_len': 20, 'vol_multiplier': 2.2, 'price_move_pct': 0.40,
                    'adx_threshold': 22.0, 'trail_activate_pct': 0.4,
                    'trail_distance_pct': 0.20, 'stop_loss_pct': 0.40,
                    'allow_short': False},
        },

        # ── Forex (IBKR) ───────────────────────────────────────────────────
        'EURUSD': {
            '5m':  {'vol_len': 20, 'vol_multiplier': 1.6, 'price_move_pct': 0.05,
                    'adx_threshold': 18.0, 'trail_activate_pct': 0.1,
                    'trail_distance_pct': 0.05, 'stop_loss_pct': 0.10},
        },
    },

    # ── RSIStrategy ───────────────────────────────────────────────────────────
    # Mean-reversion, long-only.  Works best on ranging / less-trending markets.
    # Lower oversold / raise overbought for less frequent but higher-quality trades.
    'RSIStrategy': {

        # ── Crypto (ccxt) ──────────────────────────────────────────────────
        'SOL/USD': {
            '1m':  {'rsi_period': 14, 'oversold': 30, 'overbought': 70},
            '5m':  {'rsi_period': 14, 'oversold': 28, 'overbought': 72},
            '15m': {'rsi_period': 14, 'oversold': 25, 'overbought': 75},
        },
        'BTC/USD': {
            '1m':  {'rsi_period': 14, 'oversold': 32, 'overbought': 68},
            '5m':  {'rsi_period': 14, 'oversold': 30, 'overbought': 70},
        },
        'ETH/USD': {
            '5m':  {'rsi_period': 14, 'oversold': 30, 'overbought': 70},
        },

        # ── Stocks (IBKR) ──────────────────────────────────────────────────
        'AAPL': {
            '5m':  {'rsi_period': 14, 'oversold': 35, 'overbought': 65},
            '15m': {'rsi_period': 14, 'oversold': 30, 'overbought': 70},
        },
        'TSLA': {
            '5m':  {'rsi_period': 14, 'oversold': 30, 'overbought': 70},
        },

        # ── Forex (IBKR) ───────────────────────────────────────────────────
        'EURUSD': {
            '5m':  {'rsi_period': 14, 'oversold': 35, 'overbought': 65},
            '15m': {'rsi_period': 21, 'oversold': 30, 'overbought': 70},
        },
        'GBPUSD': {
            '5m':  {'rsi_period': 14, 'oversold': 33, 'overbought': 67},
        },
    },
}


_TF_ORDER = ['1m', '5m', '15m', '30m', '1h', '4h', '1d']


def get_symbols(strategy_name: str) -> list[str]:
    """Return the sorted list of symbols configured for the given strategy."""
    return sorted(PARAMS.get(strategy_name, {}).keys())


def get_timeframes(strategy_name: str, symbol: str) -> list[str]:
    """Return timeframes for the given strategy/symbol, in natural order."""
    defined = set(PARAMS.get(strategy_name, {}).get(symbol, {}).keys())
    return [tf for tf in _TF_ORDER if tf in defined]


def get_params(strategy_name: str, symbol: str, timeframe: str) -> dict:
    """
    Return the parameter dict for the given strategy / symbol / timeframe combo.
    Returns an empty dict if the combo is not defined — the strategy will then
    use its own built-in default values.
    """
    return (PARAMS
            .get(strategy_name, {})
            .get(symbol, {})
            .get(timeframe, {}))
