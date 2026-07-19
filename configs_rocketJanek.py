

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
            '10m': {'vol_len': 8, 'vol_multiplier': 1.5, 'price_move_pct': 1.5,
                    'trail_stop_pct': 1.50, 'stop_loss_pct': 1.50, 'body_ratio_threshold': 0.5, 'currency': 'USD'},
            '30m': {'vol_len': 7, 'vol_multiplier': 1.5, 'price_move_pct': 1.5,
                    'trail_stop_pct': 1.50, 'stop_loss_pct': 1.50, 'body_ratio_threshold': 0.5},
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
