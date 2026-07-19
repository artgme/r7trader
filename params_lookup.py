_TF_ORDER = ['1m', '5m', '10m', '15m', '30m', '45m', '1h', '2h', '4h', '1d']


# Usage: symbols = get_symbols(cfg.PARAMS, 'MomentumV8Strategy')  # ['SOL/USD', 'RKLB', ...]
def get_symbols(params: dict, strategy_name: str) -> list[str]:
    """Return the sorted list of symbols configured for the given strategy."""
    return sorted(params.get(strategy_name, {}).keys())


# Usage: timeframes = get_timeframes(cfg.PARAMS, 'MomentumV8Strategy', 'RKLB')  # ['10m', '30m', ...]
def get_timeframes(params: dict, strategy_name: str, symbol: str) -> list[str]:
    """Return timeframes for the given strategy/symbol, in natural order."""
    defined = set(params.get(strategy_name, {}).get(symbol, {}).keys())
    return [tf for tf in _TF_ORDER if tf in defined]


# Usage: p = get_params(cfg.PARAMS, 'MomentumV8Strategy', 'RKLB', '10m')  # {'vol_len': 10, ...}
def get_params(params: dict, strategy_name: str, symbol: str, timeframe: str) -> dict:
    """
    Return the parameter dict for the given strategy / symbol / timeframe combo.
    Returns an empty dict if the combo is not defined — the strategy will then
    use its own built-in default values.
    """
    return (params
            .get(strategy_name, {})
            .get(symbol, {})
            .get(timeframe, {}))
