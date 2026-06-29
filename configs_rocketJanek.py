

PARAMS: dict = {

    # ── MomentumV8 ────────────────────────────────────────────────────────────
    # High-volume candle momentum with trailing stop + hard stop.
    # Tune vol_multiplier up on noisy symbols; tighten stops on volatile ones.
    'MomentumV8Strategy': {
        'ASM': {
            '5m': {'vol_len': 10, 'vol_multiplier': 1.0, 'price_move_pct': 1.0,
                    'trail_stop_pct': 0.50, 'stop_loss_pct': 1.00, 'currency': 'EUR', 'tick_size': 1.00},
        },
        'BESI': {
            '5m': {'vol_len': 10, 'vol_multiplier': 1.0, 'price_move_pct': 1.0,
                    'trail_stop_pct': 0.50, 'stop_loss_pct': 1.00, 'currency': 'EUR', 'tick_size': 0.10},
            '10m': {'vol_len': 7, 'vol_multiplier': 1.0, 'price_move_pct': 1.0,
                    'trail_stop_pct': 0.50, 'stop_loss_pct': 1.00, 'currency': 'EUR', 'tick_size': 0.10},
        },
        'RKLB': {
            '10m': {'vol_len': 8, 'vol_multiplier': 1.8, 'price_move_pct': 1.8,
                    'trail_stop_pct': 0.80, 'stop_loss_pct': 1.00, 'currency': 'USD'},
            '30m': {'vol_len': 7, 'vol_multiplier': 1.8, 'price_move_pct': 1.7,
                    'trail_stop_pct': 0.80, 'stop_loss_pct': 1.00},
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
}


_TF_ORDER = ['1m', '5m', '10m', '15m', '30m', '45m', '1h', '2h', '4h', '1d']



#   symbols    = get_symbols('MomentumV8Strategy')           # ['SOL/USD', 'RKLB', ...]
def get_symbols(strategy_name: str) -> list[str]:
    """Return the sorted list of symbols configured for the given strategy."""
    return sorted(PARAMS.get(strategy_name, {}).keys())


#   timeframes = get_timeframes('MomentumV8Strategy', 'RKLB') # ['10m', '30m', ...]
def get_timeframes(strategy_name: str, symbol: str) -> list[str]:
    """Return timeframes for the given strategy/symbol, in natural order."""
    defined = set(PARAMS.get(strategy_name, {}).get(symbol, {}).keys())
    return [tf for tf in _TF_ORDER if tf in defined]


#   params     = get_params('MomentumV8Strategy', 'RKLB', '10m') # {'vol_len': 10, ...}
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
