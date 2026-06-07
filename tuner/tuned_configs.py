"""
tuned_configs.py — Best parameters found by the tuner.

Written automatically by tuner/main.py and tuner/main_v11.py.
Copy selected entries manually to configs.py.

Format mirrors configs.py:  PARAMS[strategy_name][symbol][timeframe] = {param: value, ...}
"""

PARAMS: dict = {
    # ── MomentumV8Strategy ────────────────────────────────────
    'MomentumV8Strategy': {
        'RKLB': {
            '10m': {'vol_len': 5, 'vol_multiplier': 1.5, 'price_move_pct': 0.6},
        },
    },

}
