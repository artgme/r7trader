"""Helpers for persisting tuner results to tuned_configs.py."""

from __future__ import annotations

import importlib.util
import os

_TUNED_PATH = os.path.join(os.path.dirname(__file__), 'tuned_configs.py')

_FILE_HEADER = '''\
"""
tuned_configs.py — Best parameters found by the tuner.

Written automatically by tuner/main.py and tuner/main_v11.py.
Copy selected entries manually to configs.py.

Format mirrors configs.py:  PARAMS[strategy_name][symbol][timeframe] = {param: value, ...}
"""

PARAMS: dict = {
'''

_FILE_FOOTER = '}\n'


def _load_existing() -> dict:
    """Return the current PARAMS dict from tuned_configs.py, or {} if the file is missing."""
    if not os.path.exists(_TUNED_PATH):
        return {}
    spec = importlib.util.spec_from_file_location('tuned_configs', _TUNED_PATH)
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return dict(getattr(mod, 'PARAMS', {}))


def _fmt_params(p: dict) -> str:
    return '{' + ', '.join(f'{k!r}: {v!r}' for k, v in p.items()) + '}'


def _write_tuned_configs(params: dict) -> None:
    lines = [_FILE_HEADER]
    for strategy_name, symbols in params.items():
        sep = '─' * max(1, 54 - len(strategy_name))
        lines.append(f'    # ── {strategy_name} {sep}\n')
        lines.append(f'    {strategy_name!r}: {{\n')
        for symbol, timeframes in symbols.items():
            lines.append(f'        {symbol!r}: {{\n')
            for tf, p in timeframes.items():
                lines.append(f'            {tf!r}: {_fmt_params(p)},\n')
            lines.append(f'        }},\n')
        lines.append(f'    }},\n\n')
    lines.append(_FILE_FOOTER)
    with open(_TUNED_PATH, 'w') as fh:
        fh.write(''.join(lines))


def save_tuned_params(strategy_name: str, symbol: str, timeframe: str, params: dict) -> None:
    """Upsert one entry in tuned_configs.py and print a confirmation line."""
    existing = _load_existing()
    existing.setdefault(strategy_name, {}).setdefault(symbol, {})[timeframe] = params
    _write_tuned_configs(existing)
    print(f'  → tuned_configs.py updated  [{strategy_name!r}][{symbol!r}][{timeframe!r}]')
