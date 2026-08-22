#!/usr/bin/env python3
"""Fairer fixed-universe and rolling out-of-sample backtest.

This is a research audit, not an execution engine. It deliberately keeps a
fixed list containing theme names, broad-market controls and failed/delisted
samples. Public providers do not supply a complete point-in-time historical
universe, so coverage gaps are reported instead of silently dropping names.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from tools.a_stock_market_data import tencent_qfq_history

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def yahoo_history(code: str, range_name: str = "10y") -> list[dict[str, Any]]:
    suffix = "SS" if code.startswith(("600", "601", "603", "688", "689", "510", "511", "512", "513", "515", "518", "588")) else "SZ"
    symbol = f"{code}.{suffix}"
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?range={range_name}&interval=1d&events=history&includeAdjustedClose=true"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "Niki-Investment-Decision-Workbench/1.0"})
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = json.loads(response.read().decode("utf-8"))
    result = (((raw.get("chart") or {}).get("result") or [None])[0]) or {}
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    adjusted = (((result.get("indicators") or {}).get("adjclose") or [None])[0]) or {}
    raw_closes = quote.get("close") or []
    adjusted_closes = adjusted.get("adjclose") or raw_closes
    opens, highs, lows, volumes = quote.get("open") or [], quote.get("high") or [], quote.get("low") or [], quote.get("volume") or []
    rows: list[dict[str, Any]] = []
    for timestamp, raw_close, close, open_price, high, low, volume in zip(
        timestamps, raw_closes, adjusted_closes, opens, highs, lows, volumes
    ):
        raw_close_number, close_number = finite(raw_close), finite(close)
        if raw_close_number is None or close_number is None:
            continue
        factor = close_number / raw_close_number if raw_close_number else 1.0
        rows.append(
            {
                "date": datetime.fromtimestamp(int(timestamp), tz=ZoneInfo("UTC")).date().isoformat(),
                "open": (finite(open_price) or raw_close_number) * factor,
                "high": (finite(high) or raw_close_number) * factor,
                "low": (finite(low) or raw_close_number) * factor,
                "close": close_number,
                "volume": finite(volume) or 0.0,
            }
        )
    if not rows:
        raise RuntimeError(f"{code} Yahoo Finance returned no usable daily bars")
    return rows


def fetch_history(code: str, count: int = 3000) -> tuple[list[dict[str, Any]], str]:
    try:
        return yahoo_history(code), "Yahoo Finance adjusted daily"
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, RuntimeError):
        rows: list[dict[str, Any]] = []
        try:
            payload = tencent_qfq_history(code, min(count, 640))
            rows = payload.get("bars") or []
        except Exception:
            # The historical endpoint is more reliable for delisted symbols
            # when explicit date bounds and the legacy kline route are used.
            market = "sh" if code.startswith(("5", "6", "9")) else "sz"
            query = urllib.parse.urlencode(
                {"param": f"{market}{code},day,2010-01-01,2026-08-20,640,qfq"}
            )
            request = urllib.request.Request(
                "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?" + query,
                headers={"User-Agent": "Mozilla/5.0 Niki-Investment-Decision-Workbench"},
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = json.loads(response.read().decode("utf-8"))
            symbol = market + code
            data = (raw.get("data") or {}).get(symbol) or {}
            source_rows = data.get("qfqday") or data.get("day") or []
            for row in source_rows:
                if len(row) >= 6 and finite(row[2]) is not None:
                    rows.append(
                        {
                            "date": str(row[0]),
                            "open": finite(row[1]) or finite(row[2]),
                            "close": finite(row[2]),
                            "high": finite(row[3]) or finite(row[2]),
                            "low": finite(row[4]) or finite(row[2]),
                            "volume": finite(row[5]) or 0.0,
                        }
                    )
        if not rows:
            raise RuntimeError(f"{code} has no usable Yahoo/Tencent history")
        return rows, "Tencent Finance qfq daily"


def rolling(values: list[float], index: int, window: int) -> float | None:
    if index < window:
        return None
    base = values[index - window]
    return values[index] / base - 1 if base else None


def build_stock(rows: list[dict[str, Any]], benchmark_close: dict[str, float]) -> dict[str, Any]:
    rows = sorted(
        [{"date": str(r["date"]), "close": finite(r.get("close")), "volume": finite(r.get("volume")) or 0.0} for r in rows if finite(r.get("close")) is not None],
        key=lambda r: r["date"],
    )
    dates = [r["date"] for r in rows]
    closes = [float(r["close"]) for r in rows]
    volumes = [float(r["volume"]) for r in rows]
    index_by_date = {day: i for i, day in enumerate(dates)}
    features: dict[str, dict[str, float | None]] = {}
    for i, day in enumerate(dates):
        bench_now = benchmark_close.get(day)
        bench_5 = benchmark_close.get(dates[i - 5]) if i >= 5 else None
        bench_20 = benchmark_close.get(dates[i - 20]) if i >= 20 else None
        bench_60 = benchmark_close.get(dates[i - 60]) if i >= 60 else None
        features[day] = {
            "ret1": closes[i] / closes[i - 1] - 1 if i >= 1 and closes[i - 1] else None,
            "ret5": rolling(closes, i, 5),
            "ret20": rolling(closes, i, 20),
            "ret60": rolling(closes, i, 60),
            "ma20": sum(closes[i - 19 : i + 1]) / 20 if i >= 19 else None,
            "ma60": sum(closes[i - 59 : i + 1]) / 60 if i >= 59 else None,
            "vol_ratio": (sum(volumes[i - 4 : i + 1]) / 5) / (sum(volumes[i - 19 : i + 1]) / 20)
            if i >= 19 and sum(volumes[i - 19 : i + 1])
            else None,
            "bench_ret5": bench_now / bench_5 - 1 if bench_now and bench_5 else None,
            "bench_ret20": bench_now / bench_20 - 1 if bench_now and bench_20 else None,
            "bench_ret60": bench_now / bench_60 - 1 if bench_now and bench_60 else None,
        }
    return {
        "rows": rows,
        "dates": dates,
        "closes": closes,
        "close": {day: value for day, value in zip(dates, closes)},
        "index_by_date": index_by_date,
        "features": features,
    }


def signal_score(feature: dict[str, float | None]) -> tuple[int, bool]:
    checks = [
        feature["ma20"] is not None and feature["ma60"] is not None and float(feature["ma20"]) >= float(feature["ma60"]),
        feature["ret20"] is not None and feature["bench_ret20"] is not None and float(feature["ret20"]) > float(feature["bench_ret20"]),
        feature["ret60"] is not None and feature["bench_ret60"] is not None and float(feature["ret60"]) > float(feature["bench_ret60"]),
        feature["vol_ratio"] is not None and 0.6 <= float(feature["vol_ratio"]) <= 1.8,
        feature["ret1"] is not None and float(feature["ret1"]) < 0.07,
        feature["ret5"] is not None and float(feature["ret5"]) < 0.12,
    ]
    return sum(checks), sum(checks) >= 4


def benchmark_metrics(dates: list[str], close: dict[str, float], start: int, end: int) -> dict[str, Any]:
    returns = [close[dates[i + 1]] / close[dates[i]] - 1 for i in range(start, end) if dates[i] in close and dates[i + 1] in close]
    equity = [1.0]
    for value in returns:
        equity.append(equity[-1] * (1 + value))
    return summarize(equity, returns, dates[start : end + 1], 0, len(equity) - 1)


def summarize(equity: list[float], returns: list[float], dates: list[str], start: int = 0, end: int | None = None) -> dict[str, Any]:
    if end is None:
        end = len(equity) - 1
    curve = equity[start : end + 1]
    r = returns[start:end]
    if not curve:
        return {}
    peak, drawdown = curve[0], 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = min(drawdown, value / peak - 1)
    total = curve[-1] / curve[0] - 1
    years = max(len(r) / 252, 1 / 252)
    annualized = (1 + total) ** (1 / years) - 1 if total > -1 else -1
    vol = statistics.pstdev(r) * math.sqrt(252) if len(r) > 1 else None
    sharpe = statistics.mean(r) / statistics.pstdev(r) * math.sqrt(252) if len(r) > 1 and statistics.pstdev(r) else None
    return {
        "start_date": dates[start] if dates else None,
        "end_date": dates[min(end, len(dates) - 1)] if dates else None,
        "cumulative_return_pct": total * 100,
        "annualized_return_pct": annualized * 100,
        "max_drawdown_pct": drawdown * 100,
        "annualized_volatility_pct": vol * 100 if vol is not None else None,
        "sharpe": sharpe,
        "positive_days_pct": sum(x > 0 for x in r) / len(r) * 100 if r else None,
    }


def losing_years(dates: list[str], returns: list[float]) -> list[str]:
    by_year: dict[str, list[float]] = {}
    for day, value in zip(dates[1:], returns):
        by_year.setdefault(day[:4], []).append(value)
    return [year for year, values in sorted(by_year.items()) if math.prod(1 + x for x in values) < 1]


def simulate(
    dates: list[str],
    benchmark: dict[str, Any],
    stocks: dict[str, dict[str, Any]],
    themes: dict[str, str],
    cost: float,
    constrained: bool,
    max_per_theme: int,
    limits: dict[str, float],
) -> dict[str, Any]:
    equity = [1.0]
    returns: list[float] = []
    targets: dict[str, float] = {}
    entry_prices: dict[str, float] = {}
    theme_equity: dict[str, float] = {}
    theme_peak: dict[str, float] = {}
    theme_lock: set[str] = set()
    account_lock = False
    account_peak = 1.0
    turnover = 0.0
    trade_events = 0
    for i, day in enumerate(dates[:-1]):
        next_day = dates[i + 1]
        bench_close = benchmark["close"].get(day)
        bench_ma20 = benchmark["ma20"].get(day)
        ranked: list[tuple[float, str]] = []
        for code, item in stocks.items():
            feature = item["features"].get(day)
            if not feature:
                continue
            score, is_eligible = signal_score(feature)
            if not is_eligible:
                continue
            ranked.append((float(score), code))
        ranked.sort(reverse=True)
        selected: list[str] = []
        theme_counts: dict[str, int] = {}
        for _, code in ranked:
            theme = themes.get(code, "other")
            if theme_counts.get(theme, 0) >= max_per_theme or (constrained and theme in theme_lock):
                continue
            selected.append(code)
            theme_counts[theme] = theme_counts.get(theme, 0) + 1
        targets = {code: 1.0 / len(selected) for code in selected} if selected else {}
        if constrained:
            account_dd = equity[-1] / account_peak - 1
            if account_lock:
                targets = {}
            elif account_dd <= limits["account_drawdown"]:
                account_lock = True
                targets = {}
            for code in list(targets):
                item = stocks[code]
                price = item["close"].get(day)
                if price is None:
                    targets.pop(code, None)
                    continue
                entry = entry_prices.get(code, price)
                if price / entry - 1 <= limits["single_stock_drawdown"]:
                    targets.pop(code, None)
                    theme_lock.add(themes.get(code, "other"))
        # Compare today's target weights with yesterday's target weights.
        previous_targets = locals().get("previous_targets", {})
        deltas = sum(abs(targets.get(code, 0.0) - previous_targets.get(code, 0.0)) for code in set(targets) | set(previous_targets))
        if deltas > 1e-12:
            turnover += deltas
            trade_events += 1
            for code, weight in targets.items():
                item_price = stocks[code]["close"].get(day)
                if weight > previous_targets.get(code, 0.0) and item_price:
                    entry_prices[code] = item_price
        asset_return = 0.0
        theme_returns: dict[str, float] = {}
        for code, weight in targets.items():
            today = stocks[code]["close"].get(day)
            tomorrow = stocks[code]["close"].get(next_day)
            if today and tomorrow:
                value = tomorrow / today - 1
                asset_return += weight * value
                theme = themes.get(code, "other")
                theme_returns[theme] = theme_returns.get(theme, 0.0) + weight * value
        net = asset_return - deltas * cost
        returns.append(net)
        equity.append(equity[-1] * (1 + net))
        account_peak = max(account_peak, equity[-1])
        for theme, value in theme_returns.items():
            theme_equity[theme] = theme_equity.get(theme, 1.0) * (1 + value)
            theme_peak[theme] = max(theme_peak.get(theme, 1.0), theme_equity[theme])
            if constrained and theme_equity[theme] / theme_peak[theme] - 1 <= limits["theme_drawdown"]:
                theme_lock.add(theme)
        previous_targets = targets
    return {
        "equity": equity,
        "returns": returns,
        "turnover": turnover,
        "trade_events": trade_events,
        "losing_years": losing_years(dates, returns),
    }


def slice_oos(dates: list[str], run: dict[str, Any], benchmark_run: dict[str, Any], train_years: int, test_years: int) -> list[dict[str, Any]]:
    windows = []
    first_year = int(dates[0][:4]) + train_years
    last_year = int(dates[-1][:4])
    for year in range(first_year, last_year + 1, test_years):
        start = next((i for i, day in enumerate(dates) if day >= f"{year}-01-01"), None)
        end = next((i for i, day in enumerate(dates) if day >= f"{year + test_years}-01-01"), len(dates) - 1)
        if start is None or end - start < 60:
            continue
        windows.append(
            {
                "train_start": dates[0],
                "train_end": dates[max(0, start - 1)],
                "test_start": dates[start],
                "test_end": dates[end],
                "strategy": summarize(run["equity"], run["returns"], dates, start, end),
                "benchmark": summarize(benchmark_run["equity"], benchmark_run["returns"], dates, start, end),
            }
        )
    return windows


def build_report(config: dict[str, Any]) -> dict[str, Any]:
    codes = [str(config["benchmark"])] + [str(x) for x in config["symbols"]]
    histories: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, str] = {}
    coverage: dict[str, Any] = {}
    for code in codes:
        rows, source = fetch_history(code)
        histories[code], sources[code] = rows, source
        coverage[code] = {"rows": len(rows), "start": rows[0]["date"], "end": rows[-1]["date"], "source": source}
    benchmark_rows = sorted(histories[config["benchmark"]], key=lambda x: x["date"])
    start_date, end_date = str(config["start_date"]), str(config["end_date"])
    benchmark_rows = [r for r in benchmark_rows if start_date <= r["date"] <= end_date]
    dates = [r["date"] for r in benchmark_rows]
    benchmark_close = {r["date"]: float(r["close"]) for r in benchmark_rows}
    benchmark_ma20: dict[str, float] = {}
    for i, day in enumerate(dates):
        if i >= 19:
            benchmark_ma20[day] = sum(benchmark_close[dates[j]] for j in range(i - 19, i + 1)) / 20
    benchmark = {"close": benchmark_close, "ma20": benchmark_ma20}
    stocks: dict[str, dict[str, Any]] = {}
    for code in config["symbols"]:
        stock_rows = [r for r in histories[code] if start_date <= r["date"] <= end_date]
        stocks[code] = build_stock(stock_rows, benchmark_close)
        coverage[code]["usable_in_test"] = len(stock_rows)
        coverage[code]["in_test_start"] = stock_rows[0]["date"] if stock_rows else None
        coverage[code]["in_test_end"] = stock_rows[-1]["date"] if stock_rows else None
    unconstrained = simulate(dates, benchmark, stocks, config["themes"], float(config["transaction_cost"]), False, int(config["max_per_theme"]), config["risk_limits"])
    constrained = simulate(dates, benchmark, stocks, config["themes"], float(config["transaction_cost"]), True, int(config["max_per_theme"]), config["risk_limits"])
    benchmark_returns = [benchmark_close[dates[i + 1]] / benchmark_close[dates[i]] - 1 for i in range(len(dates) - 1)]
    benchmark_run = {"equity": [1.0], "returns": benchmark_returns}
    for value in benchmark_returns:
        benchmark_run["equity"].append(benchmark_run["equity"][-1] * (1 + value))
    capital = float(config["initial_capital"])
    def attach(result: dict[str, Any], label: str) -> dict[str, Any]:
        out = summarize(result["equity"], result["returns"], dates)
        out["hypothetical_pnl"] = capital * out["cumulative_return_pct"] / 100
        out["hypothetical_end_value"] = capital + out["hypothetical_pnl"]
        out["turnover"] = result.get("turnover")
        out["trade_events"] = result.get("trade_events")
        out["losing_years"] = result.get("losing_years")
        out["label"] = label
        period_starts = {
            "recent_three_year": max(0, len(dates) - 756),
            "recent_five_year": max(0, len(dates) - 1260),
        }
        out["periods"] = {}
        for period, period_start in period_starts.items():
            period_result = summarize(result["equity"], result["returns"], dates, period_start, len(dates) - 1)
            period_result["hypothetical_pnl"] = capital * period_result["cumulative_return_pct"] / 100
            period_result["hypothetical_end_value"] = capital + period_result["hypothetical_pnl"]
            out["periods"][period] = period_result
        return out
    report = {
        "generated_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "method": {
            "benchmark": "510300 is used as the CSI 300 ETF market-Beta reference for this audit.",
            "signal_timing": "Close-of-day signal, next benchmark session return; no future function.",
            "rolling_oos": f"{config['train_years']} years warm-up + {config['test_years']}-year non-overlapping test windows.",
            "cost": f"Weight turnover cost {float(config['transaction_cost']) * 100:.2f}% per rebalance.",
            "fairness": "Fixed audit pool includes theme names, broad-market controls and failed/delisted samples. It is not a complete point-in-time A-share universe.",
            "risk_constraints": "Constrained run applies account -12%, theme -8%, single-stock -6% hard locks for the remainder of the audit run; the hard lock is deliberately conservative.",
            "limitations": "Public history for several failed/delisted names is incomplete. Delisting-day execution, suspensions, dividends, taxes and real slippage are not fully observable; gaps are reported in coverage.",
        },
        "history": {"start": dates[0], "end": dates[-1], "sessions": len(dates)},
        "coverage": coverage,
        "failed_samples": config.get("failed_samples") or [],
        "results": {
            "benchmark_510300": attach(benchmark_run, "510300 buy-and-hold"),
            "unconstrained": attach(unconstrained, "fixed-universe trend strategy"),
            "risk_constrained": attach(constrained, "fixed-universe trend strategy with drawdown locks"),
        },
        "rolling_oos": {
            "unconstrained": slice_oos(dates, unconstrained, benchmark_run, int(config["train_years"]), int(config["test_years"])),
            "risk_constrained": slice_oos(dates, constrained, benchmark_run, int(config["train_years"]), int(config["test_years"])),
        },
    }
    for name, windows in list(report["rolling_oos"].items()):
        if not isinstance(windows, list):
            continue
        strategy_returns = [float(window["strategy"].get("cumulative_return_pct") or 0.0) for window in windows]
        benchmark_returns_oos = [float(window["benchmark"].get("cumulative_return_pct") or 0.0) for window in windows]
        report["rolling_oos"][name + "_summary"] = {
            "windows": len(windows),
            "positive_windows": sum(value > 0 for value in strategy_returns),
            "losing_windows": sum(value < 0 for value in strategy_returns),
            "beat_benchmark_windows": sum(a > b for a, b in zip(strategy_returns, benchmark_returns_oos)),
            "median_return_pct": statistics.median(strategy_returns) if strategy_returns else None,
            "mean_return_pct": statistics.mean(strategy_returns) if strategy_returns else None,
        }
    for name, result in report["results"].items():
        result["outperformance_vs_510300_pct"] = result["cumulative_return_pct"] - report["results"]["benchmark_510300"]["cumulative_return_pct"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run fixed-universe fair audit backtest")
    parser.add_argument("--config", default="watchlists/fair_universe_backtest.json")
    parser.add_argument("--output", default="reports/fair_universe_backtest_latest.json")
    args = parser.parse_args()
    report = build_report(json.loads((ROOT / args.config).read_text(encoding="utf-8")))
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"fair_universe_backtest={output}")
    for name, result in report["results"].items():
        print(name, result["cumulative_return_pct"], result["annualized_return_pct"], result["max_drawdown_pct"], result["sharpe"])
    print("oos_windows", len(report["rolling_oos"]["risk_constrained"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
