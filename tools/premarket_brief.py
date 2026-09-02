#!/usr/bin/env python3
"""每日盘前简报：美股→A股映射 + 追踪票盘前推送。

每个A股交易日早盘前(8:50)运行：
  1. 拉美股隔夜行情(三大指数 + 各映射模块 us_symbols + CBOT 农产品期货)；
  2. 按美股→A股映射表，生成一句"隔夜美股涨跌→今日A股板块情绪"结论；
  3. 拉用户追踪票盘前状态(持仓ETF + 农业种业 + 小金属 + AI算力主线)；
  4. 复用 EmailNotifier 发送 HTML 邮件。

研究观察工具，不连接券商、不自动下单、不提供买卖指令。
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
    BEIJING_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    BEIJING_TZ = timezone(timedelta(hours=8))

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from emailer import EmailNotifier
USER_AGENT = "Niki-Investment-Decision-Workbench premarket-brief/1.0"


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


# ---------------------------------------------------------------------------
# 美股行情（Yahoo Finance v8 chart API，与 theme_watch_report.yahoo_history 同源）
# ---------------------------------------------------------------------------

def _yahoo_fetch(symbol: str) -> dict[str, Any]:
    """轮询多个 Yahoo host 拉取 chart 数据，单个 host 403/超时则回退。"""
    hosts = ["query1.finance.yahoo.com", "query2.finance.yahoo.com"]
    last_error: Exception | None = None
    for host in hosts:
        url = "https://" + host + "/v8/finance/chart/" + urllib.parse.quote(symbol) + "?range=4mo&interval=1d"
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
    raise (last_error if last_error is not None else RuntimeError("Yahoo Finance chart fetch failed"))


def yahoo_quote(symbol: str) -> dict[str, Any]:
    """拉取单个美股/期货/指数的最新日线，返回涨跌幅、价格、MA20、量比等。"""
    try:
        raw = _yahoo_fetch(symbol)
    except Exception as exc:
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

    def return_over_days(days: int) -> float | None:
        if len(closes_clean) < days + 1:
            return None
        base = closes_clean[-(days + 1)]
        return (latest / base - 1.0) * 100 if base else None

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
        "daily_change_pct": return_over_days(1),
        "return_5d": return_over_days(5),
        "return_20d": return_over_days(20),
        "ma20": ma20,
        "above_ma20": latest >= ma20 if ma20 else None,
        "volume_ratio_5_20": volume_ratio,
        "source": "Yahoo Finance chart",
    }


def yahoo_batch(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """批量拉美股，逐个容错，返回 {symbol: quote}。"""
    return {symbol: yahoo_quote(symbol) for symbol in symbols}


# ---------------------------------------------------------------------------
# A股行情（腾讯，复用 a_stock_market_data 的行情解析逻辑，但独立实现以避免依赖）
# ---------------------------------------------------------------------------

def market_prefix(code: str) -> str:
    return "sh" if code.startswith(("5", "6", "9")) else "sz"


def tencent_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    """拉A股实时快照。指数用特殊前缀(sh000001/sz399001/sz399006)。"""
    symbols = []
    for code in codes:
        if code.startswith(("sh", "sz")):
            symbols.append(code)
        else:
            symbols.append(market_prefix(code) + code)
    request = urllib.request.Request(
        "https://qt.gtimg.cn/q=" + ",".join(symbols), headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        text = response.read().decode("gbk", "replace")

    rows: dict[str, dict[str, Any]] = {}
    for line in text.split(";"):
        if "~" not in line:
            continue
        symbol, _, quoted = line.partition('="')
        payload = quoted.rsplit('"', 1)[0]
        parts = payload.split("~")
        code = symbol.rsplit("_", 1)[-1][-6:]
        if len(code) != 6:
            continue

        def num(index: int) -> float | None:
            try:
                return float(parts[index])
            except (IndexError, TypeError, ValueError):
                return None

        price = num(3)
        previous_close = num(4)
        change_pct = num(32)
        if change_pct is None and price is not None and previous_close:
            change_pct = (price - previous_close) / previous_close * 100
        rows[code] = {
            "code": code,
            "name": parts[1] if len(parts) > 1 else code,
            "price": price,
            "previous_close": previous_close,
            "change_pct": change_pct,
            "amount": num(37),
            "quote_time": parts[30] if len(parts) > 30 else "",
            "source": "Tencent Finance quote",
        }
    return rows


# ---------------------------------------------------------------------------
# 报告构建
# ---------------------------------------------------------------------------

def build_report(config: dict[str, Any]) -> dict[str, Any]:
    today = now_beijing().strftime("%Y%m%d")

    # 1. 美股三大指数
    us_indices = []
    for idx in config.get("us_indices") or []:
        row = yahoo_quote(str(idx.get("symbol")))
        row["name"] = idx.get("name")
        row["a_share_proxy"] = idx.get("a_share_proxy")
        row["a_share_index"] = idx.get("a_share_index")
        us_indices.append(row)

    # 2. 美股→A股映射模块
    mapping_rows = []
    all_us_symbols: list[str] = []
    all_tracked_codes: list[str] = []
    for mod in config.get("us_to_a_mapping") or []:
        all_us_symbols.extend(str(s) for s in mod.get("us_symbols") or [])
        all_us_symbols.extend(str(s) for s in mod.get("commodity_symbols") or [])
        for item in mod.get("a_share_symbols") or []:
            if item.get("code"):
                all_tracked_codes.append(str(item["code"]))

    us_quotes = yahoo_batch(list(dict.fromkeys(all_us_symbols)))

    # 3. 追踪票清单（去重）
    tracked_groups = config.get("tracked_stocks") or {}
    for group in tracked_groups.values():
        for item in group or []:
            if item.get("code"):
                all_tracked_codes.append(str(item["code"]))

    a_share_codes = list(dict.fromkeys(all_tracked_codes))
    a_quotes = tencent_quotes(a_share_codes)
    quote_days = [str((a_quotes.get(code) or {}).get("quote_time") or "") for code in a_share_codes]
    a_share_trading_day = any(normalize_quote_day(day) == today for day in quote_days)

    # 组装映射模块结论
    for mod in config.get("us_to_a_mapping") or []:
        us_rows = []
        for symbol in list(mod.get("us_symbols") or []) + list(mod.get("commodity_symbols") or []):
            quote = us_quotes.get(str(symbol), {})
            us_rows.append({
                "symbol": str(symbol),
                "daily_change_pct": quote.get("daily_change_pct"),
                "return_5d": quote.get("return_5d"),
                "error": quote.get("error"),
            })
        # 计算模块平均隔夜涨跌
        valid_pcts = [r["daily_change_pct"] for r in us_rows if r.get("daily_change_pct") is not None]
        avg_us = average(valid_pcts) if valid_pcts else None
        a_share_rows = []
        for item in mod.get("a_share_symbols") or []:
            quote = a_quotes.get(str(item["code"]), {})
            a_share_rows.append({
                "code": str(item["code"]),
                "name": item.get("name") or quote.get("name") or item["code"],
                "role": item.get("role") or "",
                "prev_close": quote.get("previous_close"),
                "change_pct": quote.get("change_pct"),
            })
        mapping_rows.append({
            "id": mod.get("id"),
            "a_share_sector": mod.get("a_share_sector"),
            "mapping_note": mod.get("mapping_note"),
            "us_rows": us_rows,
            "avg_us_change": avg_us,
            "a_share_rows": a_share_rows,
        })

    # 追踪票分组快照
    tracked_snapshot = {}
    for group_name, group in tracked_groups.items():
        rows = []
        for item in group or []:
            quote = a_quotes.get(str(item["code"]), {})
            rows.append({
                "code": str(item["code"]),
                "name": item.get("name") or quote.get("name") or item["code"],
                "role": item.get("role") or "",
                "prev_close": quote.get("previous_close"),
                "price": quote.get("price"),
                "change_pct": quote.get("change_pct"),
            })
        tracked_snapshot[group_name] = rows

    return {
        "generated_at": now_beijing().isoformat(timespec="seconds"),
        "as_of_date": now_beijing().date().isoformat(),
        "a_share_trading_day": a_share_trading_day,
        "skip_reason": "A股非交易日，跳过邮件发送。" if config.get("skip_if_not_a_share_trading_day") and not a_share_trading_day else "",
        "us_indices": us_indices,
        "us_to_a_mapping": mapping_rows,
        "tracked_stocks": tracked_snapshot,
        "disclaimer": config.get("disclaimer") or "研究观察工具，不构成投资建议。",
    }


def normalize_quote_day(quote_time: object) -> str:
    raw = "".join(char for char in str(quote_time or "") if char.isdigit())
    return raw[:8] if len(raw) >= 8 else ""


# ---------------------------------------------------------------------------
# HTML 渲染
# ---------------------------------------------------------------------------

def table_row(values: list[str]) -> str:
    return "<tr>" + "".join(f'<td style="padding:7px;border-bottom:1px solid #d8dee8">{value}</td>' for value in values) + "</tr>"


def _arrow(pct: float | None) -> str:
    """A股情绪箭头：隔夜美股涨→偏暖(红箭头↑)，跌→偏冷(绿箭头↓)。"""
    if pct is None:
        return "—"
    if pct >= 0.5:
        return "↑偏暖"
    if pct <= -0.5:
        return "↓偏冷"
    return "→中性"


def render_html(report: dict[str, Any]) -> str:
    # 三大指数
    index_rows = []
    for idx in report.get("us_indices") or []:
        index_rows.append(table_row([
            html.escape(f"{idx.get('name') or idx.get('symbol')}"),
            html.escape(percent(idx.get("daily_change_pct"))),
            html.escape(percent(idx.get("return_5d"))),
            html.escape(percent(idx.get("return_20d"))),
            html.escape(_arrow(idx.get("daily_change_pct"))),
            html.escape(str(idx.get("a_share_proxy") or "-")),
        ]))

    # 映射模块
    mapping_sections = []
    for mod in report.get("us_to_a_mapping") or []:
        us_lines = "<br>".join(
            html.escape(
                f"{r.get('symbol')}: {percent(r.get('daily_change_pct'))} 隔夜"
                + (f"，{percent(r.get('return_5d'))} 5日" if r.get("return_5d") is not None else "")
            ) if not r.get("error") else html.escape(f"{r.get('symbol')}: 数据不可用({r.get('error')})")
            for r in mod.get("us_rows") or []
        ) or "-"
        a_share_rows = "".join(table_row([
            html.escape(f"{r.get('code')} {r.get('name')}"),
            html.escape(str(r.get("role") or "-")),
            html.escape(number(r.get("prev_close"))),
            html.escape(percent(r.get("change_pct"))),
        ]) for r in mod.get("a_share_rows") or [])
        sentiment = _arrow(mod.get("avg_us_change"))
        mapping_sections.append(f"""
        <section style="background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px">
          <h2 style="margin:0;font-size:17px">{html.escape(str(mod.get('a_share_sector') or '-'))}</h2>
          <p style="background:#eef5ff;padding:8px 10px;border-radius:6px">隔夜美股映射情绪：<strong>{sentiment}</strong>（对应美股平均 {html.escape(percent(mod.get('avg_us_change')))}）</p>
          <p style="color:#5d6b82;font-size:13px">{html.escape(str(mod.get('mapping_note') or ''))}</p>
          <p><strong>隔夜美股/商品：</strong><br>{us_lines}</p>
          <table style="border-collapse:collapse;width:100%;font-size:13px"><thead><tr style="background:#f7f9fc"><th>A股标的</th><th>定位</th><th>昨收</th><th>涨跌</th></tr></thead><tbody>{a_share_rows}</tbody></table>
        </section>""")

    # 追踪票
    tracked_sections = []
    for group_name, rows in report.get("tracked_stocks").items():
        tr = "".join(table_row([
            html.escape(f"{r.get('code')} {r.get('name')}"),
            html.escape(str(r.get("role") or "-")),
            html.escape(number(r.get("prev_close"))),
            html.escape(number(r.get("price"))),
            html.escape(percent(r.get("change_pct"))),
        ]) for r in rows)
        tracked_sections.append(f"""
        <section style="background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px">
          <h2 style="margin:0;font-size:17px">{html.escape(str(group_name))}</h2>
          <table style="border-collapse:collapse;width:100%;font-size:13px"><thead><tr style="background:#f7f9fc"><th>标的</th><th>定位</th><th>昨收</th><th>现价</th><th>涨跌</th></tr></thead><tbody>{tr}</tbody></table>
        </section>""")

    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f4f6f8;color:#172033;font:14px Arial,'Microsoft YaHei',sans-serif;line-height:1.55"><main style="max-width:860px;margin:0 auto;padding:20px">
<section style="background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:20px">
<h1 style="margin:0;font-size:24px">盘前简报：美股→A股映射 + 追踪票</h1>
<p style="color:#5d6b82">{html.escape(report.get('generated_at') or '-')} | A股交易日：{'是' if report.get('a_share_trading_day') else '否'}</p>
<p style="background:#fff8e6;padding:10px;border-radius:6px">用途：开盘前快速判断隔夜美股对今日A股板块情绪的影响，并查看追踪票状态。仅为研究观察，不构成买卖指令。</p>
</section>
<section style="background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:16px;margin-top:14px">
<h2 style="margin:0;font-size:18px">一、美股三大指数隔夜表现</h2>
<table style="border-collapse:collapse;width:100%;font-size:13px"><thead><tr style="background:#f7f9fc"><th>指数</th><th>隔夜</th><th>5日</th><th>20日</th><th>情绪</th><th>对应A股</th></tr></thead><tbody>{''.join(index_rows)}</tbody></table>
</section>
<h2 style="font-size:18px;margin:20px 0 0">二、美股→A股映射（板块情绪）</h2>
{''.join(mapping_sections)}
<h2 style="font-size:18px;margin:20px 0 0">三、追踪票盘前状态</h2>
{''.join(tracked_sections)}
<p style="color:#5d6b82;font-size:12px">{html.escape(str(report.get('disclaimer') or ''))}</p></main></body></html>"""


# ---------------------------------------------------------------------------
# 邮件发送
# ---------------------------------------------------------------------------

def send_email(report: dict[str, Any], html_content: str) -> bool:
    if report.get("skip_reason"):
        print("email_skipped=" + str(report["skip_reason"]))
        return True
    sender = os.getenv("SENDER_EMAIL", "").strip()
    password = os.getenv("SENDER_PASSWORD", "").strip()
    recipient = os.getenv("PREMARKET_RECIPIENT_EMAIL", "").strip() or os.getenv("RECIPIENT_EMAIL", "").strip()
    missing = [label for label, value in {"SENDER_EMAIL": sender, "SENDER_PASSWORD": password, "PREMARKET_RECIPIENT_EMAIL or RECIPIENT_EMAIL": recipient}.items() if not value]
    if missing:
        raise SystemExit("Missing email secrets: " + ", ".join(missing))
    notifier = EmailNotifier(
        sender,
        password,
        (os.getenv("SMTP_SERVER") or "smtp.qq.com").strip(),
        int((os.getenv("SMTP_PORT") or "465").strip()),
    )
    subject = "Niki 盘前简报 | 美股映射 + 追踪票 | " + now_beijing().strftime("%Y-%m-%d")
    if not notifier.send_html_alert(recipient, subject, html_content):
        raise SystemExit("SMTP did not accept the premarket brief email")
    print("premarket_brief_email=sent")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Build daily premarket brief (US→A-share mapping + tracked stocks)")
    parser.add_argument("--config", default="watchlists/premarket_brief.json")
    parser.add_argument("--output", default="reports/premarket_brief_latest.json")
    parser.add_argument("--email", action="store_true", help="Send the brief with configured SMTP secrets")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and validate without writing or sending")
    args = parser.parse_args()
    config = read_json(ROOT / args.config)
    report = build_report(config)
    html_content = render_html(report)
    if args.dry_run:
        print("dry_run=OK " + json.dumps({
            "a_share_trading_day": report["a_share_trading_day"],
            "us_indices": [{"symbol": i.get("symbol"), "change": i.get("daily_change_pct")} for i in report["us_indices"]],
            "mapping_modules": len(report["us_to_a_mapping"]),
            "tracked_groups": {k: len(v) for k, v in report["tracked_stocks"].items()},
            "html_bytes": len(html_content.encode("utf-8")),
        }, ensure_ascii=False))
        return 0
    output_path = ROOT / args.output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"premarket_brief={output_path}")
    if args.email:
        send_email(report, html_content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
