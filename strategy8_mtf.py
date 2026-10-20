import csv
import json
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# ============================================================
# OKX FUTURES SCALPING BACKTEST
# Strategy: EMA 9/21 + VWAP + Pullback
# Timeframes: 1m entry, 5m trend
# Standard Python libraries only
# ============================================================

BASE_URL = "https://www.okx.com"
SYMBOLS = [
    "BTC-USDT-SWAP",
    "ETH-USDT-SWAP",
    "SOL-USDT-SWAP",
    "BNB-USDT-SWAP",
    "XRP-USDT-SWAP",
]

INITIAL_BALANCE = 1000.0
RISK_PERCENT = 0.005

FEE_RATE = 0.0005
SLIPPAGE_RATE = 0.0002

EMA_FAST = 9
EMA_SLOW = 21

STOP_LOOKBACK = 10
ATR_PERIOD = 14

DAYS = 30
MAX_CANDLES = DAYS * 24 * 60

RR_VALUES = [2.0, 3.0]

# ============================================================
# HTTP
# ============================================================

def http_get(path, params=None, retries=5):
    if params:
        path += "?" + urllib.parse.urlencode(params)

    url = BASE_URL + path

    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/json",
                },
            )

            with urllib.request.urlopen(req, timeout=25) as response:
                data = json.loads(response.read().decode("utf-8"))

            if data.get("code") != "0":
                raise RuntimeError(
                    "OKX API error: " + str(data.get("msg"))
                )

            return data.get("data", [])

        except Exception as exc:
            wait = min(2 ** attempt, 20)
            print("Request error:", exc, "| retry in", wait, "seconds")
            time.sleep(wait)

    raise RuntimeError("OKX API request failed after retries: " + url)


# ============================================================
# FETCH HISTORICAL CANDLES
# ============================================================

def fetch_candles(inst_id, bar, limit):
    result = {}
    before = None
    pages = 0

    while len(result) < limit:
        params = {
            "instId": inst_id,
            "bar": bar,
            "limit": "100",
        }

        if before is not None:
            params["after"] = str(before)

        rows = http_get(
            "/api/v5/market/history-candles",
            params
        )

        if not rows:
            break

        oldest = None

        for row in rows:
            try:
                ts = int(row[0])
                candle = {
                    "ts": ts,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "confirm": str(row[8]) if len(row) > 8 else "1",
                }

                if candle["confirm"] == "1":
                    result[ts] = candle

                if oldest is None or ts < oldest:
                    oldest = ts

            except (ValueError, IndexError):
                continue

        if oldest is None:
            break

        if before is not None and oldest >= before:
            break

        before = oldest
        pages += 1

        if pages % 10 == 0:
            print(
                inst_id,
                bar,
                "candles:",
                len(result),
                "/",
                limit
            )

        time.sleep(0.12)

        if len(rows) < 100:
            break

    candles = sorted(result.values(), key=lambda x: x["ts"])

    if limit:
        candles = candles[-limit:]

    return candles


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    output = [None] * len(values)

    if len(values) < period:
        return output

    alpha = 2.0 / (period + 1.0)

    current = sum(values[:period]) / period
    output[period - 1] = current

    for i in range(period, len(values)):
        current = alpha * values[i] + (1.0 - alpha) * current
        output[i] = current

    return output


def atr(candles, period=14):
    tr = [None] * len(candles)

    for i, c in enumerate(candles):
        if i == 0:
            tr[i] = c["high"] - c["low"]
        else:
            prev_close = candles[i - 1]["close"]

            tr[i] = max(
                c["high"] - c["low"],
                abs(c["high"] - prev_close),
                abs(c["low"] - prev_close),
            )

    output = [None] * len(candles)

    if len(candles) < period:
        return output

    current = sum(tr[:period]) / period
    output[period - 1] = current

    for i in range(period, len(candles)):
        current = (
            current * (period - 1) + tr[i]
        ) / period

        output[i] = current

    return output


def add_indicators(candles):
    closes = [c["close"] for c in candles]

    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    atr_values = atr(candles, ATR_PERIOD)

    for i, c in enumerate(candles):
        c["ema_fast"] = fast[i]
        c["ema_slow"] = slow[i]
        c["atr"] = atr_values[i]

    return candles


# ============================================================
# RESAMPLE 1M -> 5M
# ============================================================

def resample_5m(candles):
    groups = {}

    for c in candles:
        bucket = c["ts"] // 300000 * 300000

        if bucket not in groups:
            groups[bucket] = []

        groups[bucket].append(c)

    output = []

    for bucket in sorted(groups):
        items = sorted(groups[bucket], key=lambda x: x["ts"])

        if len(items) != 5:
            continue

        output.append({
            "ts": bucket,
            "open": items[0]["open"],
            "high": max(x["high"] for x in items),
            "low": min(x["low"] for x in items),
            "close": items[-1]["close"],
            "volume": sum(x["volume"] for x in items),
        })

    return add_indicators(output)


# ============================================================
# VWAP
# ============================================================

def add_vwap(candles):
    current_day = None
    cumulative_pv = 0.0
    cumulative_volume = 0.0

    for c in candles:
        day = datetime.fromtimestamp(
            c["ts"] / 1000,
            tz=timezone.utc
        ).date()

        if day != current_day:
            current_day = day
            cumulative_pv = 0.0
            cumulative_volume = 0.0

        typical = (
            c["high"] + c["low"] + c["close"]
        ) / 3.0

        volume = c["volume"]

        cumulative_pv += typical * volume
        cumulative_volume += volume

        if cumulative_volume > 0:
            c["vwap"] = cumulative_pv / cumulative_volume
        else:
            c["vwap"] = c["close"]

    return candles


# ============================================================
# SIGNALS
# ============================================================

def generate_signals(candles_1m, candles_5m):
    if len(candles_1m) < 100 or len(candles_5m) < 30:
        return []

    candles_1m = add_indicators(candles_1m)
    candles_1m = add_vwap(candles_1m)

    signals = []

    j = 0
    last_signal_index = -100

    for i in range(1, len(candles_1m)):
        c = candles_1m[i]
        prev = candles_1m[i - 1]

        # Latest COMPLETED 5-minute candle only.
        while (
            j + 1 < len(candles_5m)
            and candles_5m[j + 1]["ts"] + 300000 <= c["ts"]
        ):
            j += 1

        if j >= len(candles_5m):
            continue

        trend = candles_5m[j]

        if (
            trend["ema_fast"] is None
            or trend["ema_slow"] is None
            or c["ema_fast"] is None
            or c["ema_slow"] is None
            or c["atr"] is None
            or c["vwap"] is None
        ):
            continue

        if i - last_signal_index < 5:
            continue

        # LONG: trend up, price above VWAP, EMA pullback and recovery.
        long_trend = (
            trend["ema_fast"] > trend["ema_slow"]
            and trend["close"] > trend["ema_slow"]
            and c["close"] > c["vwap"]
        )

        long_pullback = (
            prev["low"] <= prev["ema_fast"]
            and c["close"] > c["ema_fast"]
            and c["close"] > c["open"]
        )

        # SHORT: trend down, price below VWAP, EMA pullback and rejection.
        short_trend = (
            trend["ema_fast"] < trend["ema_slow"]
            and trend["close"] < trend["ema_slow"]
            and c["close"] < c["vwap"]
        )

        short_pullback = (
            prev["high"] >= prev["ema_fast"]
            and c["close"] < c["ema_fast"]
            and c["close"] < c["open"]
        )

        side = None

        if long_trend and long_pullback:
            side = "LONG"

        elif short_trend and short_pullback:
            side = "SHORT"

        if side:
            signals.append({
                "index": i,
                "ts": c["ts"],
                "side": side,
                "entry": c["close"],
                "atr": c["atr"],
            })

            last_signal_index = i

    return signals


# ============================================================
# BACKTEST
# ============================================================

def simulate(candles, signals, rr, initial_balance):
    balance = initial_balance
    peak = balance
    max_drawdown = 0.0

    trades = []
    equity_rows = []

    signal_map = {
        s["index"]: s for s in signals
    }

    i = 0

    while i < len(candles) - 1:
        signal = signal_map.get(i)

        if signal is None:
            equity_rows.append({
                "ts": candles[i]["ts"],
                "balance": balance,
            })
            i += 1
            continue

        side = signal["side"]
        entry_i = i + 1
        entry_candle = candles[entry_i]

        # Enter at next candle open, with adverse slippage.
        if side == "LONG":
            entry = entry_candle["open"] * (1 + SLIPPAGE_RATE)
        else:
            entry = entry_candle["open"] * (1 - SLIPPAGE_RATE)

        lookback_start = max(0, i - STOP_LOOKBACK + 1)
        recent = candles[lookback_start:i + 1]

        if side == "LONG":
            stop = min(c["low"] for c in recent)
            risk_distance = entry - stop
        else:
            stop = max(c["high"] for c in recent)
            risk_distance = stop - entry

        if risk_distance <= 0:
            i += 1
            continue

        # Avoid implausibly wide stops.
        if risk_distance / entry > 0.02:
            i += 1
            continue

        target = (
            entry + rr * risk_distance
            if side == "LONG"
            else entry - rr * risk_distance
        )

        risk_cash = balance * RISK_PERCENT

        quantity = risk_cash / risk_distance

        entry_fee = quantity * entry * FEE_RATE

        exit_price = None
        reason = None
        exit_i = None

        for k in range(entry_i, len(candles)):
            c = candles[k]

            if side == "LONG":
                hit_stop = c["low"] <= stop
                hit_target = c["high"] >= target
            else:
                hit_stop = c["high"] >= stop
                hit_target = c["low"] <= target

            # Conservative assumption if both levels hit in one candle.
            if hit_stop:
                exit_price = stop
                reason = "SL"
                exit_i = k
                break

            if hit_target:
                exit_price = target
                reason = "TP"
                exit_i = k
                break

        if exit_price is None:
            exit_i = len(candles) - 1
            exit_price = candles[exit_i]["close"]
            reason = "END_OF_DATA"

        # Adverse slippage on exit.
        if side == "LONG":
            exit_price *= (1 - SLIPPAGE_RATE)
            gross_pnl = quantity * (exit_price - entry)
        else:
            exit_price *= (1 + SLIPPAGE_RATE)
            gross_pnl = quantity * (entry - exit_price)

        exit_fee = quantity * exit_price * FEE_RATE

        net_pnl = gross_pnl - entry_fee - exit_fee

        balance += net_pnl

        peak = max(peak, balance)

        if peak > 0:
            dd = (peak - balance) / peak
            max_drawdown = max(max_drawdown, dd)

        trades.append({
            "entry_time": datetime.fromtimestamp(
                entry_candle["ts"] / 1000,
                tz=timezone.utc
            ).isoformat(),
            "exit_time": datetime.fromtimestamp(
                candles[exit_i]["ts"] / 1000,
                tz=timezone.utc
            ).isoformat(),
            "side": side,
            "entry": round(entry, 8),
            "stop": round(stop, 8),
            "target": round(target, 8),
            "exit": round(exit_price, 8),
            "quantity": round(quantity, 8),
            "gross_pnl": round(gross_pnl, 4),
            "fees": round(entry_fee + exit_fee, 4),
            "net_pnl": round(net_pnl, 4),
            "balance": round(balance, 4),
            "reason": reason,
            "rr": rr,
        })

        equity_rows.append({
            "ts": candles[exit_i]["ts"],
            "balance": balance,
        })

        # One position at a time per symbol.
        i = exit_i + 1

    wins = [t for t in trades if t["net_pnl"] > 0]
    losses = [t for t in trades if t["net_pnl"] <= 0]

    gross_profit = sum(t["net_pnl"] for t in wins)
    gross_loss = abs(sum(t["net_pnl"] for t in losses))

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else 0.0
    )

    win_rate = (
        100.0 * len(wins) / len(trades)
        if trades
        else 0.0
    )

    summary = {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4),
        "initial_balance": round(initial_balance, 2),
        "final_balance": round(balance, 2),
        "net_pnl": round(balance - initial_balance, 2),
        "return_pct": round(
            100.0 * (balance / initial_balance - 1),
            2
        ),
        "max_drawdown_pct": round(100.0 * max_drawdown, 2),
        "rr": rr,
    }

    return summary, trades, equity_rows


# ============================================================
# CSV
# ============================================================

def write_csv(filename, rows):
    if not rows:
        print("No rows for", filename)
        return

    with open(filename, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(rows[0].keys())
        )
        writer.writeheader()
        writer.writerows(rows)


# ============================================================
# MAIN
# ============================================================

def main():
    global DAYS, MAX_CANDLES

    if len(__import__("sys").argv) > 1:
        DAYS = max(7, min(365, int(__import__("sys").argv[1])))

    MAX_CANDLES = DAYS * 24 * 60

    all_summaries = []
    all_trades = []
    all_equity = []

    for symbol in SYMBOLS:
        print("\n" + "=" * 60)
        print("Downloading", symbol)

        try:
            candles = fetch_candles(
                symbol,
                "1m",
                MAX_CANDLES
            )

            print("Downloaded 1m candles:", len(candles))

            if len(candles) < 2000:
                print("Not enough candles; skipping", symbol)
                continue

            candles = add_indicators(candles)
            candles = add_vwap(candles)

            candles_5m = resample_5m(candles)

            print("5m candles:", len(candles_5m))

            signals = generate_signals(
                candles,
                candles_5m
            )

            print("Signals:", len(signals))

            for rr in RR_VALUES:
                summary, trades, equity = simulate(
                    candles,
                    signals,
                    rr,
                    INITIAL_BALANCE
                )

                summary["symbol"] = symbol
                all_summaries.append(summary)

                for t in trades:
                    t["symbol"] = symbol

                for e in equity:
                    e["symbol"] = symbol
                    e["rr"] = rr

                all_trades.extend(trades)
                all_equity.extend(equity)

                print(
                    symbol,
                    "RR", rr,
                    "| trades", summary["trades"],
                    "| win rate", summary["win_rate_pct"],
                    "| PF", summary["profit_factor"],
                    "| return", summary["return_pct"], "%",
                    "| drawdown", summary["max_drawdown_pct"], "%"
                )

        except Exception as exc:
            print("Symbol failed:", symbol, str(exc))

    write_csv("summary.csv", all_summaries)
    write_csv("trades.csv", all_trades)
    write_csv("equity.csv", all_equity)

    print("\nBacktest complete.")
    print("Files: summary.csv, trades.csv, equity.csv")


if __name__ == "__main__":
    main()
