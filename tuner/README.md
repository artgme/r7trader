# BackTraderTuner

Optymalizacja parametrów, uzywamy: [BackTrader](https://www.backtrader.com/).  
Optymalizcja, strategie, plotting.
---

## Quick Start

### 1. Sklonuj cały projekt

```bash
cd BackTraderTuner
```

### 2. Stwórz wirtualne środowisko

```bash
# Create (only needed once)
python3 -m venv .venv

# Activate — macOS / Linux
source .venv/bin/activate

# Activate — Windows (PowerShell)
.venv\Scripts\Activate.ps1
```

### 3. Zainstaluj biblioteki

```bash
pip install -r requirements.txt
```

### 4. Dodaj dane

Export a daily (or intraday) chart from TradingView:  
**Chart → Export chart data → CSV**

Save the file to:

```
data/BTCUSDT_daily.csv
```

Mozesz zmienic nazwe na gorze pliku `main.py` (`DATA_FILE`).

### 5. Uruchom skrypt

```bash
python main.py
```

### 6. Deaktywuj srodowisko po skonczeniu

```bash
deactivate
```

---

## Struktura Projektu

```
BackTraderTuner/
├── data/                    # Place TradingView CSV files here
├── strategies/
│   ├── __init__.py          # Re-exports all strategy classes
│   └── rsi_strategy.py      # RSI mean-reversion strategy
├── data_loaders.py          # Functions for loading market data
├── plotting.py              # Trade table printer and 3-D surface plotter
├── main.py                  # Entry point — optimisation and reporting
└── requirements.txt         # Python dependencies
```

---

## File Descriptions

### `main.py`
The entry point. Runs the full pipeline:
1. Loads OHLCV data from a TradingView CSV.
2. Runs a grid-search optimisation (`cerebro.optstrategy`) over RSI parameters.
3. Prints the best parameter combination and its performance metrics (Sharpe ratio, max drawdown, win rate).
4. Re-runs the strategy once with the best parameters, printing every order and trade.
5. Plots a 3-D parameter surface (oversold × overbought → final portfolio value).
6. Plots a candlestick chart with the RSI indicator and buy/sell entry markers.

Key settings at the top of the file:

| Variable | Default | Description |
|---|---|---|
| `DATA_FILE` | `data/BTCUSDT_daily.csv` | Path to the TradingView CSV |
| `INITIAL_CASH` | `10 000 $` | Starting capital |
| `COMMISSION` | `$1.00` | Flat commission per order execution |
| `RSI_PERIODS` | `10, 15, 20` | Grid values for the RSI period |
| `OVERSOLD_VALS` | `20 … 40` (step 5) | Grid values for the oversold threshold |
| `OVERBOUGHT_VALS` | `55 … 75` (step 5) | Grid values for the overbought threshold |

### `strategies/rsi_strategy.py`
`RSIStrategy` — a long-only RSI mean-reversion strategy.

- **Entry**: buy when RSI falls below `oversold`.
- **Exit**: sell when RSI rises above `overbought`.
- Parameters: `rsi_period`, `oversold`, `overbought`, `printlog`.
- `notify_order`: logs each order execution (price, size, commission).
- `notify_trade`: records each completed round-trip into `self.trade_log`.
- `stop`: stores `self.final_value` so the optimisation loop can rank runs.

To add a new strategy, create a new file in `strategies/`, subclass `bt.Strategy`, and implement `__init__`, `next`, `notify_order`, and `notify_trade`.

### `data_loaders.py`
`load_tradingview_csv(filepath, timeframe, compression)` — reads a TradingView CSV export and returns a `bt.feeds.PandasData` feed ready for BackTrader.

- Normalises column names to lowercase.
- Strips UTC timezone offsets from the `time` column.
- Validates that `open`, `high`, `low`, `close` are present.
- Adds a zero `volume` column if the file does not include one.

### `plotting.py`
Two visualisation functions:

- **`print_trade_list(strategy)`** — prints a formatted table of every completed trade: entry and exit date, entry and exit price, position size, direction (Long/Short), gross P&L, and net P&L. Totals are shown at the bottom.

- **`plot_parameter_surface(results, param1_name, param2_name, initial_cash)`** — draws a 3-D surface where the X and Y axes are two strategy parameters and the Z axis is the final portfolio value. When a third parameter is present, the maximum value per cell is projected onto the surface. A grey plane marks the break-even level; a gold star marks the best combination. The plot is also saved as `parameter_surface.png`.

---

## Adding a New Strategy

### Step 1 — Write the strategy class

Create `strategies/my_strategy.py` and subclass `bt.Strategy`.  
Use `strategies/rsi_strategy.py` as a reference. Two things are required by the rest of the framework:

- **`self.trade_log`** — list of dicts populated in `notify_trade` when a trade closes. Each dict must have the keys:  
  `entry_date`, `exit_date`, `entry_price`, `exit_price`, `size`, `direction`, `gross_pnl`, `net_pnl`.
- **`self.final_value`** — set in `stop()` via `self.broker.getvalue()`. The optimisation loop uses this to rank runs.

```python
# strategies/my_strategy.py
import backtrader as bt

class MyStrategy(bt.Strategy):
    params = (
        ('param_a', 10),
        ('param_b', 20),
        ('printlog', False),
    )

    def __init__(self):
        self.trade_log = []
        # ... your indicators ...

    def notify_order(self, order):
        # record entry/exit date and price here
        ...

    def notify_trade(self, trade):
        if trade.isclosed:
            self.trade_log.append({ ... })  # same keys as above

    def next(self):
        # your signal logic
        ...

    def stop(self):
        self.final_value = self.broker.getvalue()
```

### Step 2 — Export the class

Add it to `strategies/__init__.py`:

```python
from .rsi_strategy import RSIStrategy
from .my_strategy  import MyStrategy   # ← add this

__all__ = ['RSIStrategy', 'MyStrategy']
```

### Step 3 — Update `main.py`

Four changes, all near the top of the file:

| What | Location | Change |
|---|---|---|
| Import | line 19 | add `MyStrategy` to the import |
| Parameter grid | lines 31–33 | replace `RSI_PERIODS` / `OVERSOLD_VALS` / `OVERBOUGHT_VALS` with your new param ranges |
| Optimisation | `make_opt_cerebro` | `cerebro.optstrategy(MyStrategy, param_a=..., param_b=...)` |
| Single run | `make_single_cerebro` | `cerebro.addstrategy(MyStrategy, param_a=..., param_b=...)` |

The keyword argument names passed to `optstrategy` / `addstrategy` must match the names defined in `params` inside your strategy class exactly — a typo will silently use the default value instead of raising an error.

Everything else (data loading, commission, analyzers, performance printing, trade table, candlestick chart, parameter surface) is strategy-agnostic and requires no changes.

---

## Dependencies

| Package | Purpose |
|---|---|
| `backtrader` | Backtesting engine |
| `pandas` | CSV loading and data manipulation |
| `matplotlib` | Charts and 3-D surface plots |
| `numpy` | Numerical grid operations for the surface plot |
