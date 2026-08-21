#!/usr/bin/env python3
"""Build a replaceable, research-only daily theme watch report.

The report deliberately separates public price evidence from industry evidence
that still needs human verification. It never loads broker data or produces
orders, target prices, or position sizes.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emailer import EmailNotifier
from tools.a_stock_market_data import snapshot


BEIJING_TZ = ZoneInfo("Asia/Shanghai")
USER_AGENT = "Niki-Investment-Decision-Workbench theme-watch/1.0"


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Config file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in {path}: {exc}") from exc


def safe_float(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def percent(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def number(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def clean_bars(payload: dict[str, Any], code: str) -> list[dict[str, Any]]:
    rows = ((payload.get("history") or {}).get(code) or {}).get("bars") or []
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        close = safe_float(row.get("close", row.get("收盘")))
        volume = safe_float(row.get("volume", row.get("vol", row.get("成交量"))))
        if close is None:
            continue
        cleaned.append({
            "date": str(row.get("date") or row.get("datetime") or row.get("日期") or ""),
            "close": close,
            "volume": volume,
        })
    return cleaned


def return_over_days(latest: float, closes: list[float], days: int) -> float | None:
    if len(closes) < days + 1:
        return None
    base = closes[-(days + 1)]
    return (latest / base - 1.0) * 100 if base else None


def normalize_quote_day(quote_time: object) -> str:
    raw = "".join(char for char in str(quote_time or "") if char.isdigit())
    return raw[:8] if len(raw) >= 8 else ""


def a_share_metric(
    code: str,
    item: dict[str, Any],
    payload: dict[str, Any],
    benchmark_returns: dict[int, float | None],
    rules: dict[str, Any],
) -> dict[str, Any]:
    quote = (payload.get("quotes") or {}).get(code) or {}
    bars = clean_bars(payload, code)
    closes = [row["close"] for row in bars]
    volumes = [row["volume"] for row in bars if row.get("volume") is not None]
    live_price = safe_float(quote.get("price"))
    latest = live_price if live_price is not None else (closes[-1] if closes else None)
    ma_window = int(rules.get("ma_window", 20))
    ma20 = average(closes[-ma_window:]) if len(closes) >= ma_window else None
    returns = {window: return_over_days(latest, closes, window) if latest is not None else None for window in rules.get("relative_windows", [5, 10, 20])}
    relative = {
        window: (returns[window] - benchmark_returns[window])
        if returns.get(window) is not None and benchmark_returns.get(window) is not None else None
        for window in returns
    }
    volume_window = int(rules.get("volume_window", 5))
    recent_volume = average(volumes[-volume_window:]) if len(volumes) >= volume_window else None
    baseline_volume = average(volumes[-ma_window:]) if len(volumes) >= ma_window else None
    volume_ratio = recent_volume / baseline_volume if recent_volume and baseline_volume else None
    daily_change = safe_float(quote.get("change_pct"))
    chase_threshold = safe_float(rules.get("chase_day_change_pct")) or 7.0
    is_chase = daily_change is not None and daily_change >= chase_threshold
    above_ma20 = latest is not None and ma20 is not None and latest >= ma20
    checks = [
        above_ma20,
        (relative.get(5) or -float("inf")) >= 0,
        (relative.get(20) or -float("inf")) >= 0,
        (volume_ratio or 0) >= 1,
    ]
    return {
        "code": code,
        "name": item.get("name") or quote.get("name") or code,
        "role": item.get("role") or "观察",
        "as_of": normalize_quote_day(quote.get("quote_time")) or (bars[-1]["date"] if bars else ""),
        "price": latest,
        "daily_change_pct": daily_change,
        "returns": returns,
        "relative_returns": relative,
        "ma20": ma20,
        "above_ma20": above_ma20,
        "volume_ratio_5_20": volume_ratio,
        "is_chase": is_chase,
        "signal_score": sum(bool(check) for check in checks),
        "data_ready": latest is not None and len(closes) >= ma_window,
        "source": (quote.get("source") or "Tencent Finance") + " / daily bars fallback route",
    }


def yahoo_history(symbol: str) -> dict[str, Any]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol) + "?range=4mo&interval=1d"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        return {"symbol": symbol, "error": str(exc), "source": "Yahoo Finance chart"}
    result = (((raw.get("chart") or {}).get("result") or [None])[0]) or {}
    quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
    timestamps = result.get("timestamp") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    rows = []
    for timestamp, close, volume in zip(timestamps, closes, volumes):
        close_number = safe_float(close)
        if close_number is None:
            continue
        rows.append({
            "date": datetime.fromtimestamp(timestamp, tz=timezone.utc).date().isoformat(),
            "close": close_number,
            "volume": safe_float(volume),
        })
    if not rows:
        return {"symbol": symbol, "error": "No usable daily bars", "source": "Yahoo Finance chart"}
    latest = rows[-1]["close"]
    closes_clean = [row["close"] for row in rows]
    volumes_clean = [row["volume"] for row in rows if row["volume"] is not None]
    ma20 = average(closes_clean[-20:]) if len(closes_clean) >= 20 else None
    volume_ratio = None
    if len(volumes_clean) >= 20:
        denominator = average(volumes_clean[-20:])
        numerator = average(volumes_clean[-5:])
        volume_ratio = numerator / denominator if numerator and denominator else None
    return {
        "symbol": symbol,
        "as_of": rows[-1]["date"],
        "price": latest,
        "daily_change_pct": return_over_days(latest, closes_clean, 1),
        "return_5d": return_over_days(latest, closes_clean, 5),
        "return_20d": return_over_days(latest, closes_clean, 20),
        "ma20": ma20,
        "above_ma20": latest >= ma20 if ma20 else None,
        "volume_ratio_5_20": volume_ratio,
        "source": "Yahoo Finance chart",
    }


def github_component(repo: str, purpose: str) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    base = "https://api.github.com/repos/" + repo
    result = {"repo": repo, "purpose": purpose, "source": "GitHub REST API"}
    try:
        request = urllib.request.Request(base + "/releases/latest", headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            release = json.loads(response.read().decode("utf-8"))
        result.update({"latest_tag": release.get("tag_name"), "release_at": release.get("published_at")})
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            result["release_error"] = f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        result["release_error"] = str(exc)
    try:
        request = urllib.request.Request(base, headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            metadata = json.loads(response.read().decode("utf-8"))
        result["pushed_at"] = metadata.get("pushed_at")
        result["updated_at"] = metadata.get("updated_at")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
        result["repo_error"] = str(exc)
    return result


def load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    history = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            history.append(parsed)
    return history


def write_history(path: Path, report: dict[str, Any], retention_days: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    today = report.get("as_of_date")
    compact = {
        "as_of_date": today,
        "generated_at": report.get("generated_at"),
        "a_share_trading_day": report.get("a_share_trading_day"),
        "modules": [{
            "id": module.get("id"),
            "status": module.get("status"),
            "breadth": module.get("breadth"),
            "leaders": [{
                "code": stock.get("code"),
                "return_20d": (stock.get("returns") or {}).get(20),
                "relative_20d": (stock.get("relative_returns") or {}).get(20),
                "above_ma20": stock.get("above_ma20"),
            } for stock in module.get("a_share") or []],
        } for module in report.get("modules") or []],
        "github_components": report.get("github_components") or [],
    }
    records = [record for record in load_history(path) if record.get("as_of_date") != today]
    records.append(compact)
    records = sorted(records, key=lambda row: str(row.get("as_of_date") or ""))[-retention_days:]
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in records) + "\n", encoding="utf-8")


def previous_component_state(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    if not history:
        return {}
    return {item.get("repo"): item for item in history[-1].get("github_components") or [] if item.get("repo")}


def build_report(config: dict[str, Any]) -> dict[str, Any]:
    benchmark = str(config.get("benchmark") or "510300")
    rules = config.get("signal_rules") or {}
    modules = config.get("modules") or []
    codes = [benchmark]
    for module in modules:
        codes.extend(str(item.get("code")) for item in module.get("a_share_symbols") or [] if item.get("code"))
    payload = snapshot(list(dict.fromkeys(codes)), bars=65)
    benchmark_bars = clean_bars(payload, benchmark)
    benchmark_quote = (payload.get("quotes") or {}).get(benchmark) or {}
    benchmark_price = safe_float(benchmark_quote.get("price")) or (benchmark_bars[-1]["close"] if benchmark_bars else None)
    benchmark_closes = [row["close"] for row in benchmark_bars]
    benchmark_returns = {
        window: return_over_days(benchmark_price, benchmark_closes, window) if benchmark_price is not None else None
        for window in rules.get("relative_windows", [5, 10, 20])
    }
    benchmark_ma20 = average(benchmark_closes[-int(rules.get("ma_window", 20)):]) if len(benchmark_closes) >= int(rules.get("ma_window", 20)) else None
    benchmark_trend_confirmed = bool(benchmark_price and benchmark_ma20 and benchmark_price >= benchmark_ma20)
    today = now_beijing().strftime("%Y%m%d")
    quote_days = [normalize_quote_day((payload.get("quotes") or {}).get(code, {}).get("quote_time")) for code in codes if code != benchmark]
    a_share_trading_day = any(day == today for day in quote_days)
    report_modules = []
    for module in modules:
        stocks = [a_share_metric(str(item["code"]), item, payload, benchmark_returns, rules) for item in module.get("a_share_symbols") or []]
        active = [stock for stock in stocks if stock.get("data_ready")]
        breadth_ratio = safe_float(rules.get("minimum_module_breadth_ratio")) or 0.5
        threshold = max(1, math.ceil(len(active) * breadth_ratio)) if active else 1
        trend_count = sum(bool(stock.get("above_ma20")) for stock in active)
        relative_count = sum(((stock.get("relative_returns") or {}).get(20) or -float("inf")) >= 0 for stock in active)
        review_candidates = [stock for stock in active if stock.get("signal_score", 0) >= 3 and not stock.get("is_chase")]
        breadth_ok = trend_count >= threshold and relative_count >= threshold
        if not a_share_trading_day:
            status = "等待：A股未开市"
        elif not benchmark_trend_confirmed:
            status = "等待：市场基准趋势未确认"
        elif len(active) < len(stocks):
            status = "等待：数据不完整"
        elif breadth_ok and review_candidates:
            status = "可人工复核"
        elif any(stock.get("is_chase") for stock in active):
            status = "等待：存在单日过热"
        else:
            status = "观察：趋势或扩散未确认"
        us_rows = [yahoo_history(str(symbol)) for symbol in module.get("us_symbols") or []]
        report_modules.append({
            "id": module.get("id"),
            "name": module.get("name"),
            "role": module.get("role"),
            "thesis": module.get("thesis"),
            "a_share": stocks,
            "us_validation": us_rows,
            "evidence_to_watch": module.get("evidence_to_watch") or [],
            "invalidation": module.get("invalidation") or [],
            "breadth": {
                "eligible": len(active), "threshold": threshold, "above_ma20": trend_count,
                "relative_20d_positive": relative_count, "breadth_ok": breadth_ok,
            },
            "status": status,
        })
    components = [github_component(str(item["repo"]), str(item.get("purpose") or "")) for item in config.get("github_components") or [] if item.get("repo")]
    return {
        "generated_at": now_beijing().isoformat(timespec="seconds"),
        "as_of_date": now_beijing().date().isoformat(),
        "a_share_trading_day": a_share_trading_day,
        "skip_reason": "A股非交易日或行情时间未更新，跳过邮件发送。" if config.get("skip_if_not_a_share_trading_day") and not a_share_trading_day else "",
        "benchmark": {
            "code": benchmark, "name": benchmark_quote.get("name") or benchmark,
            "price": benchmark_price, "return_20d": benchmark_returns.get(20), "ma20": benchmark_ma20,
            "above_ma20": benchmark_price >= benchmark_ma20 if benchmark_price and benchmark_ma20 else None,
            "trend_confirmed": benchmark_trend_confirmed,
        },
        "market_data_status": payload.get("status") or {},
        "modules": report_modules,
        "github_components": components,
        "disclaimer": "公开行情研究与人工复核工具，不连接券商、不自动下单、不提供目标价、固定仓位或收益承诺；相关性不等于因果。",
    }


def table_row(values: list[str]) -> str:
    return "<tr>" + "".join(f'<td style="padding:7px;border-bottom:1px solid #d8dee8">{value}</td>' for value in values) + "</tr>"


def render_html(report: dict[str, Any], component_changes: set[str]) -> str:
    sections = []
    for module in report.get("modules") or []:
        stock_rows = []
        for stock in module.get("a_share") or []:
            trend = "MA20上方" if stock.get("above_ma20") else "MA20下方" if stock.get("ma20") else "数据不足"
            chase = "单日过热，等待" if stock.get("is_chase") else "-"
            stock_rows.append(table_row([
                html.escape(f"{stock.get('code')} {stock.get('name')}"),
                html.escape(percent(stock.get("daily_change_pct"))),
                html.escape(percent((stock.get("returns") or {}).get(5))),
                html.escape(percent((stock.get("returns") or {}).get(20))),
                html.escape(percent((stock.get("relative_returns") or {}).get(20))),
                html.escape(trend),
                html.escape(number(stock.get("volume_ratio_5_20"))),
                html.escape(chase),
            ]))
        us_rows = "<br>".join(
            html.escape(f"{row.get('symbol')}: {percent(row.get('daily_change_pct'))} 当日，{percent(row.get('return_20d'))} 20日，{'MA20上方' if row.get('above_ma20') else 'MA20下方' if row.get('ma20') else '数据不足'}")
            if not row.get("error") else html.escape(f"{row.get('symbol')}: 数据不可用（{row.get('error')}）")
            for row in module.get("us_validation") or []
        ) or "-"
        evidence = "；".join(html.escape(str(item)) for item in module.get("evidence_to_watch") or [])
        invalidation = "；".join(html.escape(str(item)) for item in module.get("invalidation") or [])
        breadth = module.get("breadth") or {}
        sections.append(f"""
        <section style="background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px">
          <h2 style="margin:0;font-size:18px">{html.escape(str(module.get('name') or '-'))}</h2>
          <p style="color:#5d6b82"><strong>{html.escape(str(module.get('role') or '-'))}</strong> | 状态：<strong>{html.escape(str(module.get('status') or '-'))}</strong></p>
          <p>{html.escape(str(module.get('thesis') or '-'))}</p>
          <p style="background:#eef5ff;padding:10px;border-radius:6px">模块广度：{breadth.get('above_ma20', 0)}/{breadth.get('eligible', 0)} 站上MA20；{breadth.get('relative_20d_positive', 0)}/{breadth.get('eligible', 0)} 20日相对沪深300为正；阈值 {breadth.get('threshold', '-')}。</p>
          <table style="border-collapse:collapse;width:100%;font-size:13px"><thead><tr style="background:#f7f9fc"><th>标的</th><th>当日</th><th>5日</th><th>20日</th><th>20日相对300</th><th>趋势</th><th>5/20量比</th><th>过热</th></tr></thead><tbody>{''.join(stock_rows)}</tbody></table>
          <p><strong>美股产业验证（最近可用收盘）：</strong><br>{us_rows}</p>
          <p><strong>待核验产业证据：</strong>{evidence}</p>
          <p><strong>反证条件：</strong>{invalidation}</p>
        </section>""")
    component_rows = "".join(table_row([
        html.escape(str(item.get("repo") or "-")), html.escape(str(item.get("latest_tag") or "无Release")),
        html.escape(str(item.get("release_at") or "-")), html.escape(str(item.get("pushed_at") or "-")),
        "有更新" if item.get("repo") in component_changes else "未见新变动",
    ]) for item in report.get("github_components") or [])
    benchmark = report.get("benchmark") or {}
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f4f6f8;color:#172033;font:14px Arial,'Microsoft YaHei',sans-serif;line-height:1.55"><main style="max-width:860px;margin:0 auto;padding:20px">
<section style="background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:20px"><h1 style="margin:0;font-size:24px">主题观察日报</h1>
<p style="color:#5d6b82">{html.escape(report.get('generated_at') or '-')} | A股交易日：{'是' if report.get('a_share_trading_day') else '否'}</p>
<p style="background:#eef5ff;padding:10px;border-radius:6px">市场基准：{html.escape(str(benchmark.get('name') or benchmark.get('code') or '-'))}，20日 {html.escape(percent(benchmark.get('return_20d')))}，{'MA20上方' if benchmark.get('above_ma20') else 'MA20下方' if benchmark.get('ma20') else '数据不足'}。日报仅用于观察与人工复核。</p></section>
{''.join(sections)}
<section style="background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px"><h2 style="margin:0;font-size:18px">GitHub组件维护</h2><p style="color:#5d6b82">仅跟踪配置中的开源工具版本/最近推送，更新不代表必须升级。</p><table style="border-collapse:collapse;width:100%;font-size:13px"><thead><tr style="background:#f7f9fc"><th>仓库</th><th>最新Release</th><th>发布日期</th><th>最近推送</th><th>相对上次日报</th></tr></thead><tbody>{component_rows}</tbody></table></section>
<p style="color:#5d6b82;font-size:12px">{html.escape(str(report.get('disclaimer') or ''))}</p></main></body></html>"""


def send_email(report: dict[str, Any], html_content: str) -> bool:
    if report.get("skip_reason"):
        print("email_skipped=" + str(report["skip_reason"]))
        return True
    sender = os.getenv("SENDER_EMAIL", "").strip()
    password = os.getenv("SENDER_PASSWORD", "").strip()
    recipient = os.getenv("THEME_WATCH_RECIPIENT_EMAIL", "").strip() or os.getenv("RECIPIENT_EMAIL", "").strip()
    missing = [label for label, value in {"SENDER_EMAIL": sender, "SENDER_PASSWORD": password, "THEME_WATCH_RECIPIENT_EMAIL or RECIPIENT_EMAIL": recipient}.items() if not value]
    if missing:
        raise SystemExit("Missing email secrets: " + ", ".join(missing))
    notifier = EmailNotifier(sender, password, os.getenv("SMTP_SERVER", "smtp.qq.com"), int(os.getenv("SMTP_PORT", "465")))
    subject = "Niki 决策工作台 | 主题观察日报 | " + now_beijing().strftime("%Y-%m-%d")
    if not notifier.send_html_alert(recipient, subject, html_content):
        raise SystemExit("SMTP did not accept the theme watch email")
    print("theme_watch_email=sent")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a replaceable research-only theme watch report")
    parser.add_argument("--config", default="watchlists/theme_watchlist.json")
    parser.add_argument("--output", default="reports/theme_watch_latest.json")
    parser.add_argument("--history", default="reports/theme_watch_history.jsonl")
    parser.add_argument("--email", action="store_true", help="Send the report with configured SMTP secrets")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and validate without writing or sending")
    args = parser.parse_args()
    config = read_json(ROOT / args.config)
    report = build_report(config)
    history_path = ROOT / args.history
    history = load_history(history_path)
    previous = previous_component_state(history)
    component_changes = {
        item.get("repo") for item in report.get("github_components") or []
        if previous.get(item.get("repo"), {}).get("latest_tag") not in (None, item.get("latest_tag"))
        or previous.get(item.get("repo"), {}).get("pushed_at") not in (None, item.get("pushed_at"))
    }
    report["github_component_changes"] = sorted(item for item in component_changes if item)
    html_content = render_html(report, component_changes)
    if args.dry_run:
        print("dry_run=OK " + json.dumps({
            "a_share_trading_day": report["a_share_trading_day"],
            "modules": [{"id": item["id"], "status": item["status"]} for item in report["modules"]],
            "github_components": len(report["github_components"]),
            "html_bytes": len(html_content.encode("utf-8")),
        }, ensure_ascii=False))
        return 0
    output_path = ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_history(history_path, report, int(config.get("history_retention_days") or 365))
    print(f"theme_watch_report={output_path}")
    if args.email:
        send_email(report, html_content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
