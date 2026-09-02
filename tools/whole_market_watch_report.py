#!/usr/bin/env python3
"""全市场漏斗观察报告。

市场层只做行业初筛，深研层保留少量可替换模块；公开报告不含账户、仓位、
目标价或买入金额，也不连接券商。覆盖不足时自动降级为观察。
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emailer import EmailNotifier
from monitor import load_digital_infra_watchlist, run_broad_market_scan
from tools.a_stock_market_data import snapshot
from tools.theme_watch_report import a_share_metric, clean_bars, return_over_days

BEIJING_TZ = ZoneInfo("Asia/Shanghai")
USER_AGENT = "Niki-Investment-Decision-Workbench whole-market-watch/1.0"


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Unable to read config {path}: {exc}") from exc


def finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:+.2f}%"


def num(value: float | None, digits: int = 2) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def keyed_metric(payload: dict[str, Any] | None, key: int | str) -> float | None:
    """Read metric dictionaries before or after JSON serialization.

    Python keeps return-window keys as integers while the report is being built,
    but saved JSON turns them into strings. Resending a saved report must render
    the same metrics instead of showing "-".
    """
    if not isinstance(payload, dict):
        return None
    if key in payload:
        return finite(payload.get(key))
    text_key = str(key)
    if text_key in payload:
        return finite(payload.get(text_key))
    return None


def quote_day(payload: dict[str, Any], code: str) -> str:
    raw = "".join(char for char in str((payload.get("quotes") or {}).get(code, {}).get("quote_time") or "") if char.isdigit())
    return raw[:8] if len(raw) >= 8 else ""


def trading_day(payload: dict[str, Any], benchmark: str) -> bool:
    today = now_beijing().strftime("%Y%m%d")
    day = quote_day(payload, benchmark)
    return bool(day and day == today)


def benchmark_state(payload: dict[str, Any], code: str, rules: dict[str, Any]) -> dict[str, Any]:
    quote = (payload.get("quotes") or {}).get(code) or {}
    bars = clean_bars(payload, code)
    closes = [row["close"] for row in bars]
    price = finite(quote.get("price")) or (closes[-1] if closes else None)
    window = int(rules.get("ma_window", 20))
    ma20 = sum(closes[-window:]) / window if len(closes) >= window else None
    return {
        "code": code,
        "name": quote.get("name") or code,
        "price": price,
        "ma20": ma20,
        "above_ma20": price >= ma20 if price is not None and ma20 is not None else None,
        "return_20d": return_over_days(price, closes, 20) if price is not None else None,
        "as_of": quote_day(payload, code) or (bars[-1]["date"] if bars else ""),
        "source": quote.get("source") or (payload.get("route") or ["unknown"])[0],
    }


def classify_sectors(rows: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    minimum = int(rules.get("minimum_sector_count", 3))
    research = []
    observe = []
    hot = []
    for row in rows:
        count = int(row.get("count") or 0)
        if count < minimum:
            continue
        breadth = (int(row.get("advancers") or 0) / count) if count else 0
        item = dict(row)
        item["breadth_ratio"] = round(breadth, 4)
        avg_pct = finite(row.get("avg_pct")) or 0
        if avg_pct >= float(rules.get("research_avg_pct", 1.5)) and breadth >= float(rules.get("research_breadth_ratio", 0.55)):
            item["status"] = "重点研究"
            research.append(item)
        elif avg_pct >= float(rules.get("observe_avg_pct", 0.5)) and breadth >= float(rules.get("observe_breadth_ratio", 0.45)):
            item["status"] = "可继续观察"
            observe.append(item)
        elif avg_pct >= float(rules.get("hot_avg_pct", 1.0)) and breadth >= float(rules.get("hot_breadth_ratio", 0.35)):
            item["status"] = "价格强、证据待补"
            hot.append(item)
        else:
            item["status"] = "等待"
    key = lambda row: (finite(row.get("avg_pct")) or -999, finite(row.get("amount")) or 0)
    return {
        "research": sorted(research, key=key, reverse=True)[: int(rules.get("max_research_sectors", 5))],
        "observe": sorted(observe, key=key, reverse=True)[: int(rules.get("max_observe_sectors", 10))],
        "hot": sorted(hot, key=key, reverse=True)[: int(rules.get("max_hot_sectors", 8))],
    }


def github_component(repo: str, purpose: str) -> dict[str, Any]:
    base = "https://api.github.com/repos/" + repo
    result = {"repo": repo, "purpose": purpose, "source": "GitHub REST API"}
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or "").strip()
    if token:
        headers["Authorization"] = "Bearer " + token
    for suffix, prefix in (("/releases/latest", "release"), ("", "repo")):
        try:
            request = urllib.request.Request(base + suffix, headers=headers)
            with urllib.request.urlopen(request, timeout=15) as response:
                data = json.loads(response.read().decode("utf-8"))
            if prefix == "release":
                result.update({"latest_tag": data.get("tag_name"), "release_at": data.get("published_at")})
            else:
                result.update({"pushed_at": data.get("pushed_at"), "updated_at": data.get("updated_at")})
        except urllib.error.HTTPError as exc:
            if not (prefix == "release" and exc.code == 404):
                result[f"{prefix}_error"] = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            result[f"{prefix}_error"] = str(exc)
    return result


def mason_public_tags(stock: dict[str, Any], bars: list[dict[str, Any]], rules: dict[str, Any]) -> dict[str, Any]:
    closes = [finite(row.get("close")) for row in bars]
    closes = [value for value in closes if value is not None]
    ma20 = finite(stock.get("ma20"))
    price = finite(stock.get("price"))
    relative_20 = finite((stock.get("relative_returns") or {}).get(20))
    return_5 = finite((stock.get("returns") or {}).get(5))
    deviation = ((price - ma20) / ma20 * 100) if price is not None and ma20 else None
    deviation_limit = finite(rules.get("overextension_pct")) or 8.0
    trend_ok = bool(stock.get("above_ma20")) and relative_20 is not None and relative_20 >= 0
    down_days = sum(1 for left, right in zip(closes[-5:-1], closes[-4:]) if right < left)
    double_drop = trend_ok and down_days >= 2
    pullback = trend_ok and return_5 is not None and return_5 < 0
    overextended = bool(stock.get("is_chase")) or (deviation is not None and deviation >= deviation_limit)
    if overextended:
        status = "不追：单日或均线乖离过大"
    elif pullback and double_drop:
        status = "可人工复核：顺大势逆小势（双跌观察）"
    elif pullback:
        status = "观察：等待回踩确认"
    elif trend_ok:
        status = "观察：趋势延续，等待合适回踩"
    else:
        status = "等待：趋势未确认"
    return {
        "trend_ok": trend_ok,
        "pullback_candidate": pullback,
        "double_drop_observed": double_drop,
        "down_days_last_5": down_days,
        "deviation_from_ma20_pct": deviation,
        "overextended": overextended,
        "status": status,
        "note": "梅森规则仅作公开研究标签；需再核对板块广度、产业/财务证据，不能单独触发交易。",
    }


def module_metrics(module: dict[str, Any], payload: dict[str, Any], benchmark_returns: dict[int, float | None], rules: dict[str, Any], mason_rules: dict[str, Any] | None = None) -> dict[str, Any]:
    stocks = [a_share_metric(str(item["code"]), item, payload, benchmark_returns, rules) for item in module.get("a_share_symbols") or []]
    mason_rules = mason_rules or {}
    for stock in stocks:
        stock["mason"] = mason_public_tags(stock, clean_bars(payload, str(stock.get("code"))), mason_rules)
    active = [stock for stock in stocks if stock.get("data_ready")]
    threshold = max(1, math.ceil(len(active) * float(rules.get("minimum_module_breadth_ratio", 0.5)))) if active else 1
    above = sum(bool(stock.get("above_ma20")) for stock in active)
    relative = sum(((stock.get("relative_returns") or {}).get(20) or -float("inf")) >= 0 for stock in active)
    candidates = [stock for stock in active if stock.get("signal_score", 0) >= 3 and not stock.get("is_chase")]
    if len(active) < len(stocks):
        status = "等待：数据不完整"
    elif above >= threshold and relative >= threshold and candidates:
        status = "可人工复核"
    elif any(stock.get("is_chase") for stock in active):
        status = "等待：存在单日过热"
    else:
        status = "观察：趋势或扩散未确认"
    return {
        "id": module.get("id"), "name": module.get("name"), "role": module.get("role"), "thesis": module.get("thesis"),
        "a_share": stocks, "breadth": {"eligible": len(active), "threshold": threshold, "above_ma20": above, "relative_20d_positive": relative, "breadth_ok": above >= threshold and relative >= threshold},
        "status": status, "evidence_to_watch": module.get("evidence_to_watch") or [], "invalidation": module.get("invalidation") or [],
    }


def yahoo_history(symbol: str) -> dict[str, Any]:
    url = "https://query1.finance.yahoo.com/v8/finance/chart/" + symbol + "?range=4mo&interval=1d"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=15) as response:
            raw = json.loads(response.read().decode("utf-8"))
        result = (((raw.get("chart") or {}).get("result") or [None])[0]) or {}
        quote = (((result.get("indicators") or {}).get("quote") or [None])[0]) or {}
        closes = [finite(value) for value in (quote.get("close") or [])]
        closes = [value for value in closes if value is not None]
        if not closes:
            return {"symbol": symbol, "error": "No usable daily bars", "source": "Yahoo Finance chart"}
        return {"symbol": symbol, "as_of": datetime.fromtimestamp((result.get("timestamp") or [0])[-1], tz=timezone.utc).date().isoformat() if result.get("timestamp") else "", "price": closes[-1], "return_20d": (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else None, "above_ma20": closes[-1] >= sum(closes[-20:]) / 20 if len(closes) >= 20 else None, "source": "Yahoo Finance chart"}
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
        return {"symbol": symbol, "error": str(exc), "source": "Yahoo Finance chart"}


def build_report(config: dict[str, Any]) -> dict[str, Any]:
    benchmark = str(config.get("benchmark") or "510300")
    rules = {**(config.get("signal_rules") or {}), **(config.get("sector_rules") or {})}
    market_scan = run_broad_market_scan(load_digital_infra_watchlist())
    codes = [benchmark] + [str(item["code"]) for module in config.get("deep_research_modules") or [] for item in module.get("a_share_symbols") or []]
    payload = snapshot(list(dict.fromkeys(codes)), bars=65)
    benchmark_info = benchmark_state(payload, benchmark, rules)
    benchmark_bars = clean_bars(payload, benchmark)
    benchmark_price = benchmark_info.get("price")
    benchmark_returns = {window: return_over_days(benchmark_price, [row["close"] for row in benchmark_bars], window) if benchmark_price is not None else None for window in rules.get("relative_windows", [5, 10, 20])}
    breadth = market_scan.get("breadth") or {}
    breadth_ok = int(breadth.get("advancers") or 0) > int(breadth.get("decliners") or 0)
    is_trading = trading_day(payload, benchmark)
    gate = "等待：A股未开市" if not is_trading else "等待：市场基准或广度未确认" if not benchmark_info.get("above_ma20") or not breadth_ok else "观察：市场层通过初筛，仍需板块和个股证据"
    sector_groups = classify_sectors(market_scan.get("industry_breadth") or [], config.get("sector_rules") or {})
    modules = []
    for module in config.get("deep_research_modules") or []:
        item = module_metrics(module, payload, benchmark_returns, config.get("signal_rules") or {}, config.get("mason_rules") or {})
        item["us_validation"] = [yahoo_history(str(symbol)) for symbol in module.get("us_symbols") or []]
        modules.append(item)
    components = [github_component(str(item["repo"]), str(item.get("purpose") or "")) for item in config.get("github_components") or []]
    return {
        "generated_at": now_beijing().isoformat(timespec="seconds"), "as_of_date": now_beijing().date().isoformat(), "a_share_trading_day": is_trading,
        "skip_reason": "A股非交易日或行情时间未更新，跳过邮件发送。" if config.get("skip_if_not_a_share_trading_day") and not is_trading else "",
        "market_gate": {"status": gate, "benchmark": benchmark_info, "breadth": breadth, "breadth_ok": breadth_ok},
        "sector_funnel": sector_groups,
        "market_scan": {key: market_scan.get(key) for key in ("scanned_count", "min_rows_target", "missing_estimate", "sources", "source_counts", "failures", "candidate_count")},
        "candidates": market_scan.get("results") or [], "modules": modules, "secondary_themes": config.get("secondary_themes") or [], "github_components": components,
        "disclaimer": "全市场行业初筛与公开行情研究，不连接券商、不自动下单、不提供目标价、固定仓位、买入金额或收益承诺；覆盖不足时只作观察，相关性不等于因果。",
    }


def cell(value: object) -> str:
    return f"<td style='padding:7px;border-bottom:1px solid #d8dee8'>{html.escape(str(value))}</td>"


def render_html(report: dict[str, Any]) -> str:
    gate = report.get("market_gate") or {}
    benchmark = gate.get("benchmark") or {}
    breadth = gate.get("breadth") or {}
    funnel = report.get("sector_funnel") or {}
    sector_rows = "".join("<tr>" + "".join(cell(value) for value in [row.get("name"), row.get("status"), num(finite(row.get("avg_pct"))), f"{row.get('advancers', 0)}/{row.get('count', 0)}", pct(finite(row.get("breadth_ratio")) * 100 if row.get("breadth_ratio") is not None else None), f"{(finite(row.get('amount')) or 0)/1e8:.1f}亿"]) + "</tr>" for group in ("research", "observe", "hot") for row in funnel.get(group, []))
    candidate_rows = "".join("<tr>" + "".join(cell(value) for value in [item.get("code"), item.get("name"), item.get("industry"), pct(finite(item.get("pct_change"))), f"{(finite(item.get('amount')) or 0)/1e8:.1f}亿", item.get("action")]) + "</tr>" for item in (report.get("candidates") or [])[:20]) or "<tr><td colspan='6'>本次没有可验证的量价候选</td></tr>"
    module_sections = []
    for module in report.get("modules") or []:
        rows = "".join("<tr>" + "".join(cell(value) for value in [f"{stock.get('code')} {stock.get('name')}", pct(keyed_metric(stock.get("returns"), 5)), pct(keyed_metric(stock.get("returns"), 20)), pct(keyed_metric(stock.get("relative_returns"), 20)), "MA20上方" if stock.get("above_ma20") else "MA20下方" if stock.get("ma20") else "数据不足", num(finite(stock.get("volume_ratio_5_20"))), (stock.get("mason") or {}).get("status", "-")]) + "</tr>" for stock in module.get("a_share") or [])
        us = "；".join(f"{row.get('symbol')} 20日{pct(row.get('return_20d'))}" if not row.get("error") else f"{row.get('symbol')} 数据不可用" for row in module.get("us_validation") or [])
        evidence = "；".join(str(item) for item in module.get("evidence_to_watch") or [])
        invalidation = "；".join(str(item) for item in module.get("invalidation") or [])
        module_sections.append(f"<section style='background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px'><h2>{html.escape(str(module.get('name')))}</h2><p><strong>{html.escape(str(module.get('status')))}</strong> | {html.escape(str(module.get('thesis')))}</p><p>模块广度：{module.get('breadth', {}).get('above_ma20', 0)}/{module.get('breadth', {}).get('eligible', 0)} 站上MA20；20日相对沪深300为正 {module.get('breadth', {}).get('relative_20d_positive', 0)}/{module.get('breadth', {}).get('eligible', 0)}。</p><table style='border-collapse:collapse;width:100%;font-size:13px'><tr><th>标的</th><th>5日</th><th>20日</th><th>20日相对300</th><th>趋势</th><th>5/20量比</th><th>梅森观察标签</th></tr>{rows}</table><p><strong>美股验证：</strong>{html.escape(us or '-')}</p><p><strong>待核验产业/财务证据：</strong>{html.escape(evidence)}</p><p><strong>反证条件：</strong>{html.escape(invalidation)}</p></section>")
    component_rows = "".join("<tr>" + "".join(cell(value) for value in [item.get("repo"), item.get("latest_tag") or "无Release", item.get("release_at") or "-", item.get("pushed_at") or "-", item.get("release_error") or item.get("repo_error") or "-"]) + "</tr>" for item in report.get("github_components") or [])
    return f"""<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'></head><body style='margin:0;background:#f4f6f8;color:#172033;font:14px Arial,'Microsoft YaHei',sans-serif;line-height:1.55'><main style='max-width:980px;margin:0 auto;padding:20px'><section style='background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:20px'><h1>全市场观察日报</h1><p>{html.escape(report.get('generated_at') or '-')} | A股交易日：{'是' if report.get('a_share_trading_day') else '否'}</p><p style='background:#eef5ff;padding:10px;border-radius:6px'><strong>市场总闸门：{html.escape(str(gate.get('status') or '-'))}</strong><br>基准 {html.escape(str(benchmark.get('name') or benchmark.get('code') or '-'))}：20日 {html.escape(pct(benchmark.get('return_20d')))}，{'MA20上方' if benchmark.get('above_ma20') else 'MA20下方' if benchmark.get('ma20') else '数据不足'}；上涨 {breadth.get('advancers', 0)}、下跌 {breadth.get('decliners', 0)}、平盘 {breadth.get('flat', 0)}。</p></section><section style='background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px'><h2>全市场行业漏斗</h2><p>读取 {report.get('market_scan', {}).get('scanned_count', 0)} 行，目标 {report.get('market_scan', {}).get('min_rows_target', 0)} 行；缺口估算 {report.get('market_scan', {}).get('missing_estimate', 0)}。重点研究只保留前5个行业，其他行业只进入观察或等待。</p><table style='border-collapse:collapse;width:100%;font-size:13px'><tr><th>行业</th><th>分类</th><th>平均涨跌</th><th>上涨/样本</th><th>上涨比例</th><th>成交额</th></tr>{sector_rows or '<tr><td colspan=6>行业广度不可用</td></tr>'}</table></section><section style='background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px'><h2>全市场量价候选</h2><p>候选只用于后续人工核验产业、财务、估值和反证条件，不是买入清单。</p><table style='border-collapse:collapse;width:100%;font-size:13px'><tr><th>代码</th><th>名称</th><th>行业</th><th>当日</th><th>成交额</th><th>状态</th></tr>{candidate_rows}</table></section>{''.join(module_sections)}<section style='background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px'><h2>GitHub组件更新</h2><table style='border-collapse:collapse;width:100%;font-size:13px'><tr><th>仓库</th><th>最新Release</th><th>发布日期</th><th>最近推送</th><th>错误/备注</th></tr>{component_rows}</table></section><p style='color:#5d6b82;font-size:12px'>{html.escape(str(report.get('disclaimer') or ''))}</p></main></body></html>"""


def send_email(report: dict[str, Any], content: str) -> None:
    sender = os.getenv("SENDER_EMAIL", "").strip()
    password = os.getenv("SENDER_PASSWORD", "").strip()
    recipient = (os.getenv("WHOLE_MARKET_RECIPIENT_EMAIL", "").strip() or os.getenv("THEME_WATCH_RECIPIENT_EMAIL", "").strip() or os.getenv("RECIPIENT_EMAIL", "").strip())
    if not sender or not password or not recipient:
        raise SystemExit("Missing SENDER_EMAIL, SENDER_PASSWORD, or recipient secret")
    notifier = EmailNotifier(sender, password, os.getenv("SMTP_SERVER") or "smtp.qq.com", int(os.getenv("SMTP_PORT") or "465"))
    report_date = str(report.get("as_of_date") or now_beijing().date().isoformat())
    subject = "Niki 决策工作台 | 全市场观察日报 | " + report_date
    if not notifier.send_html_alert(recipient, subject, content):
        raise SystemExit("SMTP did not accept the whole-market watch email")
    print("whole_market_email=sent")


def existing_trading_report_for_today(path: Path) -> bool:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return False
    return bool(
        report.get("a_share_trading_day")
        and report.get("as_of_date") == now_beijing().date().isoformat()
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a public whole-market observation report")
    parser.add_argument("--config", default="watchlists/whole_market_watchlist.json")
    parser.add_argument("--output", default="reports/whole_market_watch_latest.json")
    parser.add_argument("--history", default="reports/whole_market_watch_history.jsonl")
    parser.add_argument("--email", action="store_true")
    parser.add_argument("--resend-existing", action="store_true", help="Resend the saved trading-day report without refreshing market data")
    parser.add_argument(
        "--skip-existing-today",
        action="store_true",
        help="Skip scheduled duplicate runs when today's trading-day report has already been persisted",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    output = ROOT / args.output
    if args.resend_existing:
        report = read_json(output)
        if not report.get("a_share_trading_day"):
            raise SystemExit("Saved report is not an A-share trading-day report; refusing to resend")
        send_email(report, render_html(report))
        return 0
    if args.skip_existing_today and existing_trading_report_for_today(output):
        print(f"whole_market_report=skipped_existing_today {output}")
        return 0
    report = build_report(read_json(ROOT / args.config))
    content = render_html(report)
    if args.dry_run:
        print("dry_run=OK " + json.dumps({"a_share_trading_day": report["a_share_trading_day"], "market_gate": report["market_gate"]["status"], "sectors": {key: len(value) for key, value in report["sector_funnel"].items()}, "modules": len(report["modules"]), "html_bytes": len(content.encode("utf-8"))}, ensure_ascii=False))
        return 0
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    history = ROOT / args.history
    history.parent.mkdir(parents=True, exist_ok=True)
    old = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines() if line.strip()] if history.exists() else []
    old = [item for item in old if item.get("as_of_date") != report.get("as_of_date")][-364:]
    old.append({"as_of_date": report.get("as_of_date"), "generated_at": report.get("generated_at"), "market_gate": report.get("market_gate"), "sector_funnel": report.get("sector_funnel"), "modules": [{"id": item.get("id"), "status": item.get("status")} for item in report.get("modules") or []]})
    history.write_text("\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in old) + "\n", encoding="utf-8")
    print(f"whole_market_report={output}")
    if args.email and not report.get("skip_reason"):
        send_email(report, content)
    elif args.email:
        print("email_skipped=" + str(report.get("skip_reason")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
