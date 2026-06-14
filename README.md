# r7trader — Strategy Execution Flow

## Architecture Overview

Two threads run concurrently inside `Mozg.run()`:

| Thread | Role |
|---|---|
| **Main thread** | Runs `cerebro.run()` — consumes bars, executes strategy logic |
| **data-worker thread** | Fetches OHLCV bars from the exchange, pushes them onto `_feed_queue` |

`_QueueFeed` is the bridge between them. `_on_bt_fill` is the bridge between Backtrader's paper broker and the real exchange.

### How the timeframe drives strategy.next()

Cerebro has no internal timer. It calls `_QueueFeed._load()` in a loop and blocks until a bar appears on `_feed_queue`. `strategy.next()` fires exactly once per bar. The bar frequency equals the configured `timeframe`.

**IBKR** — event-driven: `reqHistoricalData(keepUpToDate=True)` opens a subscription. IBKR pushes a completed bar automatically at the end of each timeframe period. `on_bar_update(bars, has_new_bar)` fires only when `has_new_bar=True`.

**ccxt** — poll-driven: worker polls the exchange every `poll_interval_s` seconds (default 5s). A new bar is delivered only when the returned candle timestamp (`bar[0]`) is greater than the last seen timestamp, i.e. a new candle has closed.

---

## Case 1 — Bar arrives, no trade signal

```mermaid
sequenceDiagram
    participant IB as ibkr.py\nIBKRGateway
    participant IBKR as IBKR TWS / Gateway\n[external]
    participant DW as mozg.py :: Mozg\n_ibkr_data_worker() [data-worker thread]
    participant Q as mozg.py :: Mozg\n_feed_queue
    participant QF as mozg.py :: _QueueFeed\n._load()
    participant CB as backtrader\nbt.Cerebro [main thread]
    participant ST as strategies/\nMomentumV8Strategy.next()

    Note over DW,IB: one-time setup at worker startup
    DW->>IB: fetch_live_bars(contract, bar_size)
    IB->>IBKR: reqHistoricalData(keepUpToDate=True)
    IBKR-->>DW: live subscription open
    DW->>DW: live.updateEvent += on_bar_update

    Note over IBKR,DW: IBKR pushes a completed bar at the end of each timeframe period
    IBKR->>DW: on_bar_update(bars, has_new_bar=True)
    Note over DW: has_new_bar=True → take bars[-1]
    DW->>Q: queue.put(dt, o, h, l, c, v)

    Note over QF,CB: cerebro loops on _load() — no timer, purely data-driven
    QF->>Q: queue.get(timeout=5)
    Q->>QF: bar tuple
    QF->>CB: return True — fills BT OHLCV lines
    CB->>ST: next()
    ST->>ST: evaluate indicators (RSI, momentum…)
    ST->>CB: no signal — return
    Note over QF: _load() blocks again, waiting for next bar
```

---

## Case 2 — Bar arrives, order signal generated and routed to IBKR

```mermaid
sequenceDiagram
    participant IB as ibkr.py\nIBKRGateway
    participant IBKR as IBKR TWS / Gateway\n[external]
    participant DW as mozg.py :: Mozg\n_ibkr_data_worker() [data-worker thread]
    participant FQ as mozg.py :: Mozg\n_feed_queue
    participant QF as mozg.py :: _QueueFeed\n._load()
    participant CB as backtrader\nbt.Cerebro [main thread]
    participant WR as mozg.py :: _Wrapped\n.notify_order()
    participant FILL as mozg.py :: Mozg\n_on_bt_fill()
    participant OQ as mozg.py :: Mozg\n_ibkr_order_queue

    Note over DW,IB: one-time setup at worker startup
    DW->>IB: fetch_live_bars(contract, bar_size)
    IB->>IBKR: reqHistoricalData(keepUpToDate=True)
    IBKR-->>DW: live subscription open
    DW->>DW: live.updateEvent += on_bar_update

    Note over IBKR,DW: IBKR pushes bar at end of timeframe period
    IBKR->>DW: on_bar_update(bars, has_new_bar=True)
    DW->>FQ: queue.put(dt, o, h, l, c, v)

    Note over QF,CB: cerebro unblocks as soon as bar arrives
    QF->>FQ: queue.get(timeout=5)
    FQ->>QF: bar tuple
    QF->>CB: return True — fills BT OHLCV lines
    CB->>CB: strategy.next() — signal detected
    CB->>CB: self.buy() / self.sell() — BT paper broker fills immediately

    CB->>WR: notify_order(order)
    WR->>WR: super().notify_order() — P&L tracking, trailing stop, trade_log
    WR->>FILL: on_fill_cb(is_buy, size, price)
    FILL->>FILL: classify action (enter_long / exit_long_tp / exit_short_sl…)

    FILL->>OQ: _ibkr_order_queue.put(side, size, is_entry)

    Note over DW: data-worker drains order queue on every ib.sleep(1) cycle
    DW->>OQ: get_nowait()
    OQ->>DW: order request

    alt is_entry and bracket config set
        DW->>IB: place_bracket_trailing()
    else
        DW->>IB: place_market_order()
    end
    IB->>IBKR: real order sent to exchange
    IBKR-->>IB: order confirmed

    FILL->>FILL: update self.position (FLAT / LONG / SHORT)
    FILL->>FILL: _send_dash_marker()
    FILL->>FILL: _log_trade() → CSV
```

---

## Key Components

| Component | Class | File | Role |
|---|---|---|---|
| `Mozg` | `Mozg` | `mozg.py` | Top-level engine — wires everything together |
| `_QueueFeed` | `_QueueFeed` | `mozg.py` | BT data feed backed by a thread-safe queue |
| `_make_live_strategy` | `_Wrapped` (generated) | `mozg.py` | Wraps strategy to intercept BT fills |
| `_on_bt_fill` | `Mozg` | `mozg.py` | Classifies signal, routes real order, logs trade |
| `_ibkr_data_worker` | `Mozg` | `mozg.py` | Subscribes to IBKR live bars + drains IBKR order queue |
| `_ccxt_data_worker` | `Mozg` | `mozg.py` | Polls ccxt for new closed candles |
| `fetch_live_bars` | `IBKRGateway` | `ibkr.py` | Opens `reqHistoricalData(keepUpToDate=True)` subscription |
| `place_market_order` | `IBKRGateway` | `ibkr.py` | Sends a market order to IBKR |
| `place_bracket_trailing` | `IBKRGateway` | `ibkr.py` | Sends a bracket order with trailing stop to IBKR |
| Strategy `next()` | e.g. `MomentumV8Strategy` | `strategies/` | Evaluates indicators, calls `self.buy()` / `self.sell()` |
| `notify_order` | e.g. `MomentumV8Strategy` | `strategies/` | Updates per-trade state (entry price, trailing stop, trade log) |
