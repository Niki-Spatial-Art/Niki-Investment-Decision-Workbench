#!/usr/bin/env python3
"""Price-only, non-overlapping event study for the replaceable theme modules.

This intentionally does not turn qualitative industry evidence into fabricated
history. It measures only whether a predefined trend/relative-strength/volume
condition had useful subsequent 20-session outcomes in the available public
daily-bar history.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emailer import EmailNotifier
from tools.a_stock_market_data import tencent_qfq_history


BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def percent(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def load_config(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Unable to read config {path}: {exc}") from exc


def history_rows(code: str, count: int = 900) -> list[dict[str, Any]]:
    """Use Tencent qfq bars directly so every stock uses the same adjusted route."""
    try:
        rows = tencent_qfq_history(code, count).get("bars") or []
    except Exception as exc:
        return [{"error": str(exc)}]
    clean = []
    for row in rows:
        close = safe_float(row.get("close"))
        open_price = safe_float(row.get("open"))
        low = safe_float(row.get("low"))
        volume = safe_float(row.get("volume"))
        date = str(row.get("date") or "")
        if close is None or open_price is None or not date:
            continue
        clean.append({"date": date, "open": open_price, "close": close, "low": low or close, "volume": volume})
    return clean


def aligned_rows(stock: list[dict[str, Any]], benchmark: list[dict[str, Any]]) -> list[dict[str, Any]]:
    reference = {row["date"]: row for row in benchmark if "date" in row}
    return [{"date": row["date"], "stock": row, "benchmark": reference[row["date"]]} for row in stock if row.get("date") in reference]


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def signal_study(
    code: str,
    name: str,
    rows: list[dict[str, Any]],
    rules: dict[str, Any],
    settings: dict[str, Any],
) -> dict[str, Any]:
    if rows and rows[0].get("error"):
        return {"code": code, "name": name, "error": rows[0]["error"]}
    ma_window = int(rules.get("ma_window", 20))
    holding_days = int(settings.get("holding_days", 20))
    volume_window = int(rules.get("volume_window", 5))
    chase_limit = safe_float(rules.get("chase_day_change_pct")) or 7.0
    round_trip_cost = (safe_float(settings.get("round_trip_cost_pct")) or 0.3) / 100
    minimum = max(int(settings.get("minimum_history_bars", 180)), ma_window + holding_days + 2)
    if len(rows) < minimum:
        return {"code": code, "name": name, "error": f"历史样本不足：{len(rows)}根，至少需要{minimum}根"}

    entries = []
    index = ma_window
    while index + holding_days + 1 < len(rows):
        stock_close = rows[index]["stock"]["close"]
        stock_ma20 = mean([item["stock"]["close"] for item in rows[index - ma_window + 1:index + 1]])
        benchmark_close = rows[index]["benchmark"]["close"]
        stock_return20 = stock_close / rows[index - 20]["stock"]["close"] - 1
        benchmark_return20 = benchmark_close / rows[index - 20]["benchmark"]["close"] - 1
        stock_return1 = stock_close / rows[index - 1]["stock"]["close"] - 1
        recent_volumes = [item["stock"]["volume"] for item in rows[index - volume_window + 1:index + 1] if item["stock"]["volume"]]
        baseline_volumes = [item["stock"]["volume"] for item in rows[index - ma_window + 1:index + 1] if item["stock"]["volume"]]
        volume_ratio = (mean(recent_volumes) / mean(baseline_volumes)) if recent_volumes and baseline_volumes and mean(baseline_volumes) else None
        qualifies = (
            stock_close >= stock_ma20
            and stock_return20 > benchmark_return20
            and volume_ratio is not None and volume_ratio >= 1
            and stock_return1 * 100 < chase_limit
        )
        if not qualifies:
            index += 1
            continue
        entry_index = index + 1  # next-session entry preserves a minimum T+1-compatible holding period
        exit_index = entry_index + holding_days
        entry_price = rows[entry_index]["stock"]["open"]
        exit_price = rows[exit_index]["stock"]["close"]
        gross_return = exit_price / entry_price - 1 if entry_price else 0
        net_return = gross_return - round_trip_cost
        lows = [item["stock"]["low"] for item in rows[entry_index:exit_index + 1]]
        max_adverse = min(lows) / entry_price - 1 if entry_price and lows else None
        entries.append({
            "signal_date": rows[index]["date"], "entry_date": rows[entry_index]["date"], "exit_date": rows[exit_index]["date"],
            "net_return_pct": net_return * 100, "gross_return_pct": gross_return * 100,
            "max_adverse_close_pct": max_adverse * 100 if max_adverse is not None else None,
        })
        index = exit_index + 1  # non-overlapping events avoid treating one trend as many independent trades

    returns = [item["net_return_pct"] for item in entries]
    adverse = [item["max_adverse_close_pct"] for item in entries if item["max_adverse_close_pct"] is not None]
    return {
        "code": code, "name": name, "sample_start": rows[0]["date"], "sample_end": rows[-1]["date"],
        "signal_count": len(entries), "win_rate_pct": (sum(value > 0 for value in returns) / len(returns) * 100) if returns else None,
        "average_net_return_pct": mean(returns), "median_net_return_pct": statistics.median(returns) if returns else None,
        "worst_net_return_pct": min(returns) if returns else None, "best_net_return_pct": max(returns) if returns else None,
        "average_max_adverse_pct": mean(adverse), "worst_max_adverse_pct": min(adverse) if adverse else None,
        "events": entries[-8:],
    }


def build_report(config: dict[str, Any]) -> dict[str, Any]:
    benchmark_code = str(config.get("benchmark") or "510300")
    rules = config.get("signal_rules") or {}
    settings = config.get("price_only_backtest") or {}
    benchmark = history_rows(benchmark_code)
    modules = []
    for module in config.get("modules") or []:
        rows = []
        for item in module.get("a_share_symbols") or []:
            code = str(item.get("code") or "")
            stock = history_rows(code)
            rows.append(signal_study(code, str(item.get("name") or code), aligned_rows(stock, benchmark), rules, settings))
        modules.append({"id": module.get("id"), "name": module.get("name"), "stocks": rows})
    return {
        "generated_at": datetime.now(BEIJING_TZ).isoformat(timespec="seconds"),
        "benchmark": benchmark_code,
        "method": {
            "entry": "当日收盘满足MA20、20日相对沪深300、近5日/20日量能和单日不过热后，于下一交易日开盘模拟进入",
            "exit": f"持有{settings.get('holding_days', 20)}个交易日后收盘模拟退出，信号不重叠",
            "cost": f"每次完整进出扣除{settings.get('round_trip_cost_pct', 0.3)}%费用与滑点估算",
            "limitation": settings.get("note"),
        },
        "modules": modules,
        "disclaimer": "这是价格条件事件研究，不是完整策略回测或投资建议。它无法历史化验证产业催化、财报披露时点、估值、流动性冲击和实际执行；结果不代表未来收益。",
    }


def render_html(report: dict[str, Any]) -> str:
    sections = []
    for module in report.get("modules") or []:
        rows = []
        for stock in module.get("stocks") or []:
            if stock.get("error"):
                rows.append(f"<tr><td>{html.escape(stock.get('code', '-'))}</td><td colspan=7>{html.escape(stock['error'])}</td></tr>")
                continue
            rows.append(
                "<tr>" + "".join(f"<td style='padding:7px;border-bottom:1px solid #d8dee8'>{html.escape(value)}</td>" for value in [
                    f"{stock.get('code')} {stock.get('name')}",
                    f"{stock.get('sample_start')} 至 {stock.get('sample_end')}", str(stock.get("signal_count", 0)),
                    percent(stock.get("win_rate_pct")), percent(stock.get("average_net_return_pct")), percent(stock.get("median_net_return_pct")),
                    percent(stock.get("worst_net_return_pct")), percent(stock.get("worst_max_adverse_pct")),
                ]) + "</tr>"
            )
        sections.append(f"""<section style='background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px'>
<h2 style='margin:0;font-size:18px'>{html.escape(str(module.get('name') or '-'))}</h2>
<table style='border-collapse:collapse;width:100%;font-size:13px;margin-top:10px'><thead><tr style='background:#f7f9fc'><th>标的</th><th>样本区间</th><th>非重叠信号</th><th>胜率</th><th>平均净收益</th><th>中位净收益</th><th>最差净收益</th><th>最差持有期回撤</th></tr></thead><tbody>{''.join(rows)}</tbody></table></section>""")
    method = report.get("method") or {}
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'></head><body style='margin:0;background:#f4f6f8;color:#172033;font:14px Arial,"Microsoft YaHei",sans-serif;line-height:1.55'><main style='max-width:900px;margin:0 auto;padding:20px'>
<section style='background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:20px'><h1 style='margin:0;font-size:24px'>主题价格条件研究周报</h1><p style='color:#5d6b82'>{html.escape(report.get('generated_at') or '-')} | 基准：{html.escape(report.get('benchmark') or '-')}</p>
<p><strong>进入：</strong>{html.escape(method.get('entry') or '-')}</p><p><strong>退出：</strong>{html.escape(method.get('exit') or '-')}</p><p><strong>成本：</strong>{html.escape(method.get('cost') or '-')}</p><p style='background:#fff5de;padding:10px;border-radius:6px'><strong>限制：</strong>{html.escape(method.get('limitation') or '-')}</p></section>{''.join(sections)}
<p style='color:#5d6b82;font-size:12px'>{html.escape(report.get('disclaimer') or '')}</p></main></body></html>"""


def send_email(report: dict[str, Any], content: str) -> None:
    sender = os.getenv("SENDER_EMAIL", "").strip()
    password = os.getenv("SENDER_PASSWORD", "").strip()
    recipient = os.getenv("THEME_WATCH_RECIPIENT_EMAIL", "").strip() or os.getenv("RECIPIENT_EMAIL", "").strip()
    if not sender or not password or not recipient:
        raise SystemExit("Missing SENDER_EMAIL, SENDER_PASSWORD, or THEME_WATCH_RECIPIENT_EMAIL/RECIPIENT_EMAIL")
    notifier = EmailNotifier(
        sender,
        password,
        (os.getenv("SMTP_SERVER") or "smtp.qq.com").strip(),
        int((os.getenv("SMTP_PORT") or "465").strip()),
    )
    subject = "Niki 决策工作台 | 主题价格条件研究周报 | " + datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
    if not notifier.send_html_alert(recipient, subject, content):
        raise SystemExit("SMTP did not accept the backtest research email")
    print("theme_backtest_email=sent")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the public price-only theme event study")
    parser.add_argument("--config", default="watchlists/theme_watchlist.json")
    parser.add_argument("--output", default="reports/theme_backtest_latest.json")
    parser.add_argument("--email", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    report = build_report(load_config(ROOT / args.config))
    content = render_html(report)
    if args.dry_run:
        print("dry_run=OK " + json.dumps({
            "modules": [{"id": module["id"], "stocks": len(module["stocks"])} for module in report["modules"]],
            "html_bytes": len(content.encode("utf-8")),
        }, ensure_ascii=False))
        return 0
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"theme_backtest_report={output}")
    if args.email:
        send_email(report, content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
