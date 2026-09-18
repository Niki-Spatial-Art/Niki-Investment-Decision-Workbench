"""Small-sample historical diagnostics, never out-of-sample profitability evidence."""
from __future__ import annotations

from tools.decision_state import number

METHOD_VERSION = "next-open-nonoverlap-2026-09-18"


def signal_backtest(bars: list[dict], rules: dict) -> dict:
    result = {"eligible": False, "method_version": METHOD_VERSION, "validation": "in_sample_diagnostic",
              "reason": "历史样本不足", "horizons": {},
              "limitations": "样本内诊断，非下一笔胜率；各期限独立且不重叠；次日开盘模拟入场，至少隔一交易日退出；未模拟实际挂单、涨跌停排队或组合资金竞争。回撤按每日收盘估值。"}
    if len(bars) < 30:
        return result
    dates = [str(b.get("date") or "")[:10] for b in bars]
    if any(not date for date in dates) or dates != sorted(set(dates)):
        return {**result, "reason": "日线日期缺失、重复或未排序"}
    closes = [number(b.get("close")) for b in bars]
    opens = [number(b.get("open")) for b in bars]
    volumes = [number(b.get("volume")) for b in bars]
    if any(v is None or v <= 0 for v in closes + opens) or any(v is None or v < 0 for v in volumes):
        return {**result, "reason": "日线价格或成交量无效"}
    friction = (float(rules.get("cost_bps", 10)) + float(rules.get("slippage_bps", 5))) / 10000
    if not 0 <= friction < 1:
        return {**result, "reason": "费用参数无效"}
    stats = {}
    for horizon in (1, 3, 5):
        values, trades = [], []
        equity = peak = 1.0
        drawdown = 0.0
        next_signal = 20
        for i in range(20, len(bars) - horizon - 1):
            if i < next_signal:
                continue
            c = closes[i]
            v20, v5 = sum(volumes[i-19:i+1]) / 20, sum(volumes[i-4:i+1]) / 5
            if c <= sum(closes[i-19:i+1]) / 20 or (c / closes[i-1] - 1) * 100 >= float(rules.get("chase_day_change_pct", 7)) or v20 <= 0 or v5 > v20 * 1.8:
                continue
            entry, end = i + 1, i + 1 + horizon
            # Zero volume and one-price bars may be suspended/locked; do not assume fills.
            if any(volumes[j] <= 0 or (number(bars[j].get("high")) is not None and number(bars[j].get("high")) == number(bars[j].get("low"))) for j in (entry, end)):
                continue
            start_equity = equity
            for j in range(entry, end + 1):
                equity = start_equity * (closes[j] / opens[entry] - (friction if j == end else 0))
                peak = max(peak, equity)
                drawdown = min(drawdown, equity / peak - 1)
            net = closes[end] / opens[entry] - 1 - friction
            values.append(net)
            trades.append({"signal_date": dates[i], "entry_date": dates[entry], "exit_date": dates[end]})
            next_signal = end
        stats[str(horizon)] = {"observations": len(values),
            "win_rate": round(sum(v > 0 for v in values) / len(values), 4) if values else None,
            "net_return": round(sum(values) / len(values), 4) if values else None,
            "max_drawdown": round(drawdown, 4) if values else None,
            "cumulative_return": round(equity - 1, 4) if values else None, "trades": trades}
    thresholds = rules.get("backtest_thresholds") or {}
    eligible = all(
        row["observations"] >= int(thresholds.get("minimum_observations", 3))
        and row["win_rate"] is not None and row["win_rate"] >= float(thresholds.get("minimum_win_rate", .5))
        and row["net_return"] is not None and row["net_return"] > float(thresholds.get("minimum_net_return", 0))
        and row["max_drawdown"] is not None and row["max_drawdown"] >= -float(thresholds.get("maximum_drawdown", .2))
        for row in stats.values())
    return {**result, "eligible": eligible, "reason": "诊断阈值通过，仍待样本外验证" if eligible else "诊断阈值未通过",
            "cost_bps": friction * 10000, "horizons": stats}
