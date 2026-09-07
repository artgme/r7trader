PARAMS: dict = {   'MomentumV8Strategy': {   'ALAB': {   '10m': {   'body_ratio_threshold': 0.7,
                                                     'price_move_pct': 3.0,
                                                     'take_profit_pct': 2.0,
                                                     'trail_stop_pct': 2.0,
                                                     'vol_len': 5,
                                                     'vol_multiplier': 3.0}},
                              'ASTS': {   '10m': {   'body_ratio_threshold': 0.3,
                                                     'price_move_pct': 3.5,
                                                     'take_profit_pct': 2.0,
                                                     'trail_stop_pct': 2.5,
                                                     'vol_len': 5,
                                                     'vol_multiplier': 3.5}},
                              'ISRG': {   '10m': {   'body_ratio_threshold': 0.7,
                                                     'price_move_pct': 0.8,
                                                     'take_profit_pct': 1.5,
                                                     'trail_stop_pct': 3.0,
                                                     'vol_len': 5,
                                                     'vol_multiplier': 2.0}},
                              'NXT': {   '10m': {   'body_ratio_threshold': 0.7,
                                                    'price_move_pct': 3.5,
                                                    'take_profit_pct': 2.0,
                                                    'trail_stop_pct': 3.0,
                                                    'vol_len': 7,
                                                    'vol_multiplier': 1.5}},
                              'VOYG': {   '10m': {   'body_ratio_threshold': 0.7,
                                                     'price_move_pct': 1.2,
                                                     'take_profit_pct': 2.0,
                                                     'trail_stop_pct': 1.0,
                                                     'vol_len': 5,
                                                     'vol_multiplier': 3.5}}}}


# === Ticker ranking by sortino (best combo per ticker) ===
#     #  ticker      sortino  vol_len   vol_mult   price_pct   trail_pct   tp_pct   body_ratio
#     1  ASTS          +3.64        5       3.50        3.50        2.50     2.00         0.30
#     2  ALAB          +2.79        5       3.00        3.00        2.00     2.00         0.70
#     3  VOYG          +1.71        7       3.50        3.00        1.00     2.00         0.30
#     4  NXT           +1.38        5       3.50        3.50        1.00     2.00         0.30
#     5  ISRG          +0.49        5       2.00        0.80        3.00     1.50         0.30

# === Ticker ranking by sortino (best combo per ticker) ===
#     #  ticker      sortino  vol_len   vol_mult   price_pct   trail_pct   tp_pct   body_ratio
#     1  PATH          +4.69       10       3.50        3.50        2.50     1.00         0.70
#     2  HPQ           +3.50       10       3.00        3.00        1.50     1.00         0.70
#     3  ABNB          +2.33        7       3.50        3.00        3.50     2.00         0.70
#     4  LUV           +1.73        7       3.00        3.50        3.00     2.00         0.70
#     5  FRSH          +1.73        7       2.50        2.50        1.00     1.50         0.30
#     6  JD            +1.21        5       1.20        1.00        2.50     2.00         0.70
#     7  DIS           +1.06       10       1.50        2.50        3.00     2.00         0.70
#     8  TMO           +0.85       10       1.50        2.50        1.00     2.00         0.70
#     9  CSCO          +0.75        5       2.00        1.80        1.00     2.00         0.30
#    10  PYPL          +0.72        5       1.00        3.50        3.50     1.50         0.70
#    11  UAA           +0.72        7       3.50        1.20        1.00     2.00         0.30
#    12  ELVN          +0.68        5       3.50        3.50        2.50     1.00         0.70
#    13  HALO          +0.60       10       0.50        2.50        3.00     2.00         0.50
#    14  XP            +0.37        7       0.70        3.00        3.00     2.00         0.30
