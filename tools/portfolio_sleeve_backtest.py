#!/usr/bin/env python3
"""Layered public portfolio backtest: deep / swing / shallow sleeves.

This is a research simulation only. Signals use information available at the
close of day t and apply the resulting target weights to the next session.
It does not place orders, produce current buy amounts, or forecast returns.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.a_stock_market_data import tencent_qfq_history

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def pct(value: float | None) -> float | None:
    return value * 100 if value is not None else None


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def fetch_history(code: str, count: int) -> list[dict[str, Any]]:
    try:
        payload = tencent_qfq_history(code, count)
        tencent_rows = payload.get("bars") or []
        if len(tencent_rows) >= min(count, 700):
            return sorted(
                [
                    {
                        "date": str(item.get("date") or ""),
                        "open": finite(item.get("open")) or finite(item.get("close")) or 0.0,
                        "high": finite(item.get("high")) or finite(item.get("close")) or 0.0,
                        "low": finite(item.get("low")) or finite(item.get("close")) or 0.0,
                        "close": finite(item.get("close")),
                        "volume": finite(item.get("volume")) or 0.0,
                    }
                    for item in tencent_rows
                    if finite(item.get("close")) is not None and str(item.get("date") or "")
                ],
                key=lambda row: row["date"],
            )
    except Exception:
        tencent_rows = []
    suffix = "SS" if code.startswith(("600", "601", "603", "688", "689", "510", "511", "512", "513", "515", "518", "588")) else "SZ"
    symbol = f"{code}.{suffix}"
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        f"{symbol}?range=5y&interval=1d&events=history&includeAdjustedClose=true"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "Niki-Investment-Decision-Workbench/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{code} history unavailable from Tencent and Yahoo: {exc}") from exc
    result = (((raw.get("chart") or {}).get("result") or [None])[0]) or {}
    timestamps = result.get("timestamp") or []
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    adjusted = (((result.get("indicators") or {}).get("adjclose") or [None])[0]) or {}
    raw_closes = quote.get("close") or []
    adjusted_closes = adjusted.get("adjclose") or raw_closes
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    volumes = quote.get("volume") or []
    rows = []
    for timestamp, raw_close, close, open_price, high, low, volume in zip(timestamps, raw_closes, adjusted_closes, opens, highs, lows, volumes):
        raw_close_number = finite(raw_close)
        close_number = finite(close)
        if raw_close_number is None or close_number is None:
            continue
        factor = close_number / raw_close_number if raw_close_number else 1.0
        rows.append({
            "date": datetime.fromtimestamp(int(timestamp), tz=ZoneInfo("UTC")).date().isoformat(),
            "open": (finite(open_price) or raw_close_number) * factor,
            "high": (finite(high) or raw_close_number) * factor,
            "low": (finite(low) or raw_close_number) * factor,
            "close": close_number,
            "volume": finite(volume) or 0.0,
        })
    if not rows:
        raise RuntimeError(f"{code} Yahoo Finance returned no usable daily bars")
    return sorted(rows, key=lambda row: row["date"])


def by_date(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {row["date"]: row for row in rows}


def rolling_return(values: list[float], index: int, window: int) -> float | None:
    if index < window:
        return None
    base = values[index - window]
    return values[index] / base - 1 if base else None


def metrics_for(code: str, rows: list[dict[str, Any]], benchmark: list[dict[str, Any]], windows: dict[str, int]) -> dict[str, Any]:
    closes = [row["close"] for row in rows]
    volumes = [row["volume"] for row in rows]
    bench_closes = [row["close"] for row in benchmark]
    out = {"code": code, "rows": rows, "close": closes, "volume": volumes, "benchmark_close": bench_closes}
    for label, window in windows.items():
        out[f"ret_{label}"] = [rolling_return(closes, i, window) for i in range(len(rows))]
        out[f"bench_ret_{label}"] = [rolling_return(bench_closes, i, window) for i in range(len(benchmark))]
        out[f"ma_{label}"] = [
            mean(closes[i - window + 1:i + 1]) if i >= window - 1 else None
            for i in range(len(rows))
        ]
    out["vol_ratio"] = [
        (mean(volumes[i - 4:i + 1]) / mean(volumes[i - 19:i + 1]))
        if i >= 19 and mean(volumes[i - 19:i + 1])
        else None
        for i in range(len(rows))
    ]
    return out


def signal_score(item: dict[str, Any], benchmark_item: dict[str, Any], index: int, sleeve: str) -> tuple[float, dict[str, Any]]:
    close = item["close"][index]
    ret5 = item["ret_5"][index]
    ret20 = item["ret_20"][index]
    ret60 = item["ret_60"][index]
    bench5 = benchmark_item["ret_5"][index]
    bench20 = benchmark_item["ret_20"][index]
    bench60 = benchmark_item["ret_60"][index]
    vol_ratio = item["vol_ratio"][index]
    ma20 = item["ma_20"][index]
    ma60 = item["ma_60"][index]
    ret1 = (close / item["close"][index - 1] - 1) if index else None
    ret3 = (close / item["close"][index - 3] - 1) if index >= 3 else None
    deviation = (close / ma20 - 1) if ma20 else None
    not_chase = (
        ret1 is not None and ret1 < 0.07
        and ret3 is not None and ret3 < 0.12
        and deviation is not None and deviation < 0.08
    )
    if sleeve == "deep":
        checks = [
            close >= (ma60 or float("inf")),
            ma20 is not None and ma60 is not None and ma20 >= ma60,
            ret60 is not None and bench60 is not None and ret60 > bench60,
            bool(not_chase),
        ]
        score = sum(checks)
        eligible = score >= 3
    elif sleeve == "swing":
        pullback = ret5 is not None and -0.08 <= ret5 <= 0.02 and deviation is not None and deviation <= 0.03
        checks = [
            close >= (ma20 or float("inf")),
            ret20 is not None and bench20 is not None and ret20 > bench20,
            vol_ratio is not None and 0.6 <= vol_ratio <= 1.8,
            pullback or (ret5 is not None and 0 <= ret5 <= 0.08),
            bool(not_chase),
        ]
        score = sum(checks)
        eligible = score >= 4
    else:
        checks = [
            close >= (ma20 or float("inf")),
            ret5 is not None and bench5 is not None and ret5 > bench5,
            vol_ratio is not None and vol_ratio >= 0.7,
            ret1 is not None and ret1 < 0.05,
            bool(not_chase),
        ]
        score = sum(checks)
        eligible = score >= 4
    return float(score), {
        "eligible": eligible,
        "score": score,
        "not_chase": not_chase,
        "deviation_ma20": deviation,
        "ret5": ret5,
        "ret20": ret20,
        "ret60": ret60,
        "vol_ratio": vol_ratio,
    }


def select_symbols(
    items: dict[str, dict[str, Any]],
    benchmark_item: dict[str, Any],
    index: int,
    sleeve: str,
    themes: dict[str, str],
    max_per_theme: int,
) -> tuple[list[str], dict[str, Any]]:
    ranked = []
    for code, item in items.items():
        score, detail = signal_score(item, benchmark_item, index, sleeve)
        if detail["eligible"]:
            ranked.append((score, code, detail))
    ranked.sort(reverse=True)
    selected: list[str] = []
    theme_counts: dict[str, int] = {}
    details: dict[str, Any] = {}
    for score, code, detail in ranked:
        theme = themes.get(code, "other")
        if theme_counts.get(theme, 0) >= max_per_theme:
            continue
        selected.append(code)
        theme_counts[theme] = theme_counts.get(theme, 0) + 1
        details[code] = {"score": score, "theme": theme, **detail}
    return selected, details


def simulate_sleeve(
    dates: list[str],
    items: dict[str, dict[str, Any]],
    benchmark_item: dict[str, Any],
    sleeve: str,
    themes: dict[str, str],
    config: dict[str, Any],
) -> dict[str, Any]:
    settings = config[sleeve]
    allocation = float(settings["allocation"])
    rebalance_every = int(settings["rebalance_every"])
    max_per_theme = int(settings.get("max_per_theme", 1))
    held: list[str] = []
    targets: dict[int, dict[str, float]] = {}
    signal_log: list[dict[str, Any]] = []
    last_rebalance = -10**9
    for index in range(len(dates) - 1):
        if index < 65:
            targets[index] = {}
            continue
        should_rebalance = index - last_rebalance >= rebalance_every
        if should_rebalance:
            selected, details = select_symbols(items, benchmark_item, index, sleeve, themes, max_per_theme)
            held = selected
            last_rebalance = index
            signal_log.append({
                "date": dates[index],
                "sleeve": sleeve,
                "selected": held,
                "details": details,
            })
        else:
            # Exit a held name when its defining trend fails, even before the
            # next scheduled rebalance.
            survivors = []
            for code in held:
                item = items[code]
                close = item["close"][index]
                ma = item["ma_60" if sleeve == "deep" else "ma_20"][index]
                relative = item["ret_60" if sleeve == "deep" else "ret_20"][index]
                bench_relative = benchmark_item["ret_60" if sleeve == "deep" else "ret_20"][index]
                if ma is not None and close >= ma and relative is not None and bench_relative is not None and relative >= bench_relative:
                    survivors.append(code)
            held = survivors
        equal_weight = allocation / len(held) if held else 0.0
        targets[index] = {code: equal_weight for code in held}
    return {"targets": targets, "signals": signal_log, "allocation": allocation}


def simulate_portfolio(
    dates: list[str],
    items: dict[str, dict[str, Any]],
    benchmark_item: dict[str, Any],
    sleeves: dict[str, dict[str, Any]],
    themes: dict[str, str],
    cost: float,
    market_filter: str = "none",
    gate_scale: float = 0.0,
) -> dict[str, Any]:
    sleeve_runs = {
        sleeve: simulate_sleeve(dates, items, benchmark_item, sleeve, themes, sleeves)
        for sleeve in ("deep", "swing", "shallow")
    }
    equity = [1.0]
    daily_returns: list[float] = []
    turnover = 0.0
    trades = 0
    previous: dict[str, float] = {}
    decisions: list[dict[str, Any]] = []
    for index in range(len(dates) - 1):
        targets: dict[str, float] = {}
        for run in sleeve_runs.values():
            for code, weight in (run["targets"].get(index) or {}).items():
                targets[code] = targets.get(code, 0.0) + weight
        if market_filter == "benchmark_ma20":
            benchmark_close = benchmark_item["close"][index]
            benchmark_ma20 = benchmark_item["ma_20"][index]
            if benchmark_ma20 is not None and benchmark_close < benchmark_ma20:
                targets = {code: weight * gate_scale for code, weight in targets.items()}
        deltas = sum(abs(targets.get(code, 0.0) - previous.get(code, 0.0)) for code in set(targets) | set(previous))
        if deltas > 1e-12:
            turnover += deltas
            trades += 1
            entries = sorted(code for code, weight in targets.items() if weight > previous.get(code, 0.0) + 1e-12)
            exits = sorted(code for code, weight in previous.items() if weight > targets.get(code, 0.0) + 1e-12)
            decisions.append({
                "date": dates[index],
                "entries": entries,
                "exits": exits,
                "positions": targets,
                "turnover": deltas,
                "action": "进入/加仓" if entries and not exits else "退出/减仓" if exits and not entries else "调仓",
            })
        asset_return = 0.0
        for code, weight in targets.items():
            row = items[code]
            close_today = row["close"][index]
            close_next = row["close"][index + 1]
            if close_today:
                asset_return += weight * (close_next / close_today - 1)
        cost_paid = deltas * cost
        net = asset_return - cost_paid
        daily_returns.append(net)
        equity.append(equity[-1] * (1 + net))
        previous = targets
    return {
        "equity": equity,
        "daily_returns": daily_returns,
        "turnover": turnover,
        "trade_events": trades,
        "decisions": decisions,
        "sleeves": sleeve_runs,
    }


def summarize(equity: list[float], daily_returns: list[float], dates: list[str], start: int, end: int) -> dict[str, Any]:
    subset = equity[start:end + 1]
    returns = daily_returns[max(0, start):end]
    if not subset:
        return {}
    peak = subset[0]
    max_dd = 0.0
    for value in subset:
        peak = max(peak, value)
        max_dd = min(max_dd, value / peak - 1)
    total = subset[-1] / subset[0] - 1
    years = max((end - start) / 252, 1 / 252)
    annualized = (1 + total) ** (1 / years) - 1 if 1 + total > 0 else -1
    volatility = statistics.pstdev(returns) * math.sqrt(252) if len(returns) > 1 else None
    sharpe = (statistics.mean(returns) / statistics.pstdev(returns) * math.sqrt(252)) if returns and statistics.pstdev(returns) > 0 else None
    return {
        "start_date": dates[start],
        "end_date": dates[end],
        "cumulative_return_pct": pct(total),
        "annualized_return_pct": pct(annualized),
        "max_drawdown_pct": pct(max_dd),
        "annualized_volatility_pct": pct(volatility),
        "sharpe": sharpe,
        "positive_days_pct": (sum(value > 0 for value in returns) / len(returns) * 100) if returns else None,
    }


def add_hypothetical_capital(result: dict[str, Any], capital: float) -> dict[str, Any]:
    """Attach a research-only 500k illustration without treating it as advice."""
    result["hypothetical_pnl"] = capital * ((result.get("cumulative_return_pct") or 0) / 100)
    result["hypothetical_end_value"] = capital + result["hypothetical_pnl"]
    return result


def scenario_result(
    run: dict[str, Any],
    common_dates: list[str],
    config: dict[str, Any],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    """Build the same period metrics for a base scenario or a blended portfolio."""
    ytd_start = next((i for i, day in enumerate(common_dates) if day >= config["ytd_start"]), 0)
    h1_end = max(
        ytd_start,
        max(
            (i for i, day in enumerate(common_dates) if day <= config.get("h1_end", "2026-06-30")),
            default=ytd_start,
        ),
    )
    three_year_start = max(0, len(common_dates) - int(config.get("three_year_days", 756)))
    five_year_start = max(0, len(common_dates) - int(config.get("five_year_days", 1260)))
    capital = float(config.get("initial_capital", 500000))
    result: dict[str, Any] = {
        **metadata,
        "full_period": summarize(run["equity"], run["daily_returns"], common_dates, 0, len(common_dates) - 1),
        "h1_2026": summarize(run["equity"], run["daily_returns"], common_dates, ytd_start, h1_end),
        "ytd": summarize(run["equity"], run["daily_returns"], common_dates, ytd_start, len(common_dates) - 1),
        "recent_three_year": summarize(
            run["equity"], run["daily_returns"], common_dates, three_year_start, len(common_dates) - 1
        ),
        "recent_five_year": summarize(
            run["equity"], run["daily_returns"], common_dates, five_year_start, len(common_dates) - 1
        ),
    }
    for period in ("full_period", "h1_2026", "ytd", "recent_three_year", "recent_five_year"):
        add_hypothetical_capital(result[period], capital)
    return result


def blend_run(
    scenario_runs: dict[str, dict[str, Any]],
    blend_weights: dict[str, float],
) -> dict[str, Any]:
    """Blend constituent *daily* net returns, leaving any residual in cash."""
    length = len(next(iter(scenario_runs.values()))["daily_returns"])
    daily_returns = [
        sum(float(weight) * scenario_runs[name]["daily_returns"][index] for name, weight in blend_weights.items())
        for index in range(length)
    ]
    equity = [1.0]
    for value in daily_returns:
        equity.append(equity[-1] * (1 + value))
    decision_dates = sorted(
        {
            decision["date"]
            for name in blend_weights
            for decision in scenario_runs[name].get("decisions", [])
        }
    )
    return {
        "equity": equity,
        "daily_returns": daily_returns,
        "turnover": sum(float(blend_weights[name]) * scenario_runs[name].get("turnover", 0.0) for name in blend_weights),
        "trade_events": None,
        "decisions": [{"date": day, "action": "底层场景组合发生调仓"} for day in decision_dates],
    }


def build_report(config: dict[str, Any]) -> dict[str, Any]:
    codes = [str(config["benchmark"])] + [str(code) for code in config["symbols"]]
    histories = {code: fetch_history(code, int(config.get("history_bars", 1000))) for code in codes}
    common_dates = sorted(set.intersection(*(set(row["date"] for row in histories[code]) for code in codes)))
    if len(common_dates) < 200:
        raise SystemExit(f"Insufficient common history: {len(common_dates)} dates")
    rows_by_code = {code: by_date(histories[code]) for code in codes}
    aligned = {code: [rows_by_code[code][day] for day in common_dates] for code in codes}
    benchmark_item = metrics_for(config["benchmark"], aligned[config["benchmark"]], aligned[config["benchmark"]], config["windows"])
    items = {
        code: metrics_for(code, aligned[code], aligned[config["benchmark"]], config["windows"])
        for code in config["symbols"]
    }
    scenarios: dict[str, dict[str, Any]] = {}
    scenario_runs: dict[str, dict[str, Any]] = {}
    for name, sleeves in config["scenarios"].items():
        scenario_settings = sleeves
        run = simulate_portfolio(
            common_dates,
            items,
            benchmark_item,
            sleeves,
            config["themes"],
            float(config["transaction_cost"]),
            str(scenario_settings.get("market_filter") or "none"),
            float(scenario_settings.get("gate_scale") or 0.0),
        )
        scenario_runs[name] = run
        scenarios[name] = scenario_result(
            run,
            common_dates,
            config,
            {
            "kind": "base_scenario",
            "initial_capital": config.get("initial_capital", 500000),
            "weights": {sleeve: value["allocation"] for sleeve, value in sleeves.items() if isinstance(value, dict) and "allocation" in value},
            "market_filter": scenario_settings.get("market_filter") or "none",
            "gate_scale": scenario_settings.get("gate_scale", 0.0),
            "trade_events": run["trade_events"],
            "turnover": run["turnover"],
            "last_decisions": run["decisions"][-10:],
            },
        )
    blends: dict[str, dict[str, Any]] = {}
    for name, raw_weights in (config.get("blend_scenarios") or {}).items():
        weights = {str(key): float(value) for key, value in raw_weights.items()}
        unknown = sorted(set(weights) - set(scenario_runs))
        if unknown:
            raise ValueError(f"Blend {name} references unknown scenarios: {unknown}")
        if any(value < 0 for value in weights.values()):
            raise ValueError(f"Blend {name} has negative weights")
        total_weight = sum(weights.values())
        if total_weight > 1.000001:
            raise ValueError(f"Blend {name} weights exceed 100%: {total_weight:.4f}")
        run = blend_run(scenario_runs, weights)
        scenario_runs[name] = run
        blends[name] = scenario_result(
            run,
            common_dates,
            config,
            {
                "kind": "blended_portfolio",
                "weights": weights,
                "cash_weight": max(0.0, 1.0 - total_weight),
                "trade_events": None,
                "turnover": run["turnover"],
                "last_decisions": run["decisions"][-10:],
                "method_note": "按底层场景逐日净收益加权复合；未把累计收益率做简单平均。",
            },
        )
    benchmark_returns = {
        "full_period": summarize(
            [1.0] + [aligned[config["benchmark"]][i]["close"] / aligned[config["benchmark"]][0]["close"] for i in range(len(common_dates))],
            [aligned[config["benchmark"]][i + 1]["close"] / aligned[config["benchmark"]][i]["close"] - 1 for i in range(len(common_dates) - 1)],
            common_dates, 0, len(common_dates) - 1,
        )
    }
    return {
        "generated_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "method": {
            "signal_timing": "收盘计算信号，下一交易日收益开始计入，避免未来函数",
            "deep": "约每60个交易日重评，MA60/相对强弱为主，偏长期埋伏",
            "swing": "约每20个交易日重评，MA20/回踩/相对强弱为主，偏中段波段",
            "shallow": "约每5个交易日重评，使用短周期条件，持有逻辑偏浅；A股个股仍按T+1建模，不等同T+0",
            "cost": f"每次权重变动按单边成本 {config['transaction_cost'] * 100:.2f}% 估算",
            "blend": "混合组合使用基础场景的逐日净收益按权重加权后再复合；未简单平均各场景累计收益。权重未占满部分视为现金，现金收益按0计。",
            "limitations": "价格/成交回测不能验证产业、估值、财报披露时点、停牌、涨跌停成交和真实滑点；不能保证未来收益。样本来自可获得的公开历史，个股篮子存在幸存者偏差。历史组合结果不能直接转化为当前买入金额。",
        },
        "history": {"start": common_dates[0], "end": common_dates[-1], "sessions": len(common_dates)},
        "benchmark": benchmark_returns,
        "scenarios": scenarios,
        "blended_portfolios": blends,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run layered public portfolio backtest")
    parser.add_argument("--config", default="watchlists/portfolio_sleeve_backtest.json")
    parser.add_argument("--output", default="reports/portfolio_sleeve_backtest_latest.json")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = build_report(load_json(ROOT / args.config))
    if args.dry_run:
        print(json.dumps({
            "history": report["history"],
            "scenarios": list(report["scenarios"]),
            "blended_portfolios": list(report["blended_portfolios"]),
        }, ensure_ascii=False))
        return 0
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"portfolio_sleeve_backtest={output}")
    for name, scenario in report["scenarios"].items():
        print(name, scenario["ytd"], scenario["recent_three_year"])
    for name, scenario in report["blended_portfolios"].items():
        print("blend", name, scenario["ytd"], scenario["recent_three_year"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
