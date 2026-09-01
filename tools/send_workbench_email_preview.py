#!/usr/bin/env python3
"""Send a manual post-close market scan email for the investment workbench."""

from __future__ import annotations

import argparse
import html
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from a_stock_market_data import snapshot
from emailer import EmailNotifier
from monitor import broad_market_tiers, load_digital_infra_watchlist, run_broad_market_scan


INDEX_CODES = ["510300", "512100", "512880", "588000", "518880"]
CORE_INDEX_CODES = ["510300", "512100", "588000"]
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
INTRADAY_WINDOWS = [
    (9, 30),
    (10, 0),
    (10, 30),
    (11, 0),
    (11, 30),
    (13, 0),
    (13, 30),
    (14, 0),
    (14, 30),
    (14, 55),
    (15, 0),
]
DEFAULT_INTRADAY_TOLERANCE_MINUTES = 12


def as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def fmt_price(value: object) -> str:
    try:
        return f"{float(value):.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "-"


def fmt_pct(value: object) -> str:
    try:
        number = float(value)
        return "0.00%" if abs(number) < 0.005 else f"{number:+.2f}%"
    except (TypeError, ValueError):
        return "-"


def fmt_amount(value: object) -> str:
    amount = as_float(value)
    if amount >= 100_000_000:
        return f"{amount / 100_000_000:.1f} 亿"
    if amount >= 10_000:
        return f"{amount / 10_000:.0f} 万"
    return f"{amount:.0f}"


def fmt_beijing_time(value: object) -> str:
    """Render cloud-generated timestamps consistently for the local user."""
    raw = str(value or "").strip()
    if not raw:
        return "-"
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return f"{parsed.astimezone(BEIJING_TZ):%Y-%m-%d %H:%M:%S} 北京时间"


def intraday_window_state(now: datetime | None = None) -> tuple[bool, str]:
    """Return whether an intraday email is allowed before reading market data."""
    current = now.astimezone(BEIJING_TZ) if now else datetime.now(BEIJING_TZ)
    tolerance = as_float(os.getenv("INTRADAY_WINDOW_TOLERANCE_MINUTES"), DEFAULT_INTRADAY_TOLERANCE_MINUTES)
    if current.weekday() >= 5:
        return False, f"skip_intraday=weekend now={current:%Y-%m-%d %H:%M:%S %Z}"
    for hour, minute in INTRADAY_WINDOWS:
        scheduled = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delay = current - scheduled
        if timedelta(0) <= delay <= timedelta(minutes=tolerance):
            return True, f"window={hour:02d}:{minute:02d} delay_minutes={delay.total_seconds() / 60:.1f}"
    windows = ",".join(f"{hour:02d}:{minute:02d}" for hour, minute in INTRADAY_WINDOWS)
    return False, f"skip_intraday=outside_window now={current:%Y-%m-%d %H:%M:%S %Z} windows={windows} tolerance_minutes={tolerance:g}"


def quote_day(payload: dict, code: str) -> str:
    raw = "".join(char for char in str((payload.get("quotes") or {}).get(code, {}).get("quote_time") or "") if char.isdigit())
    return raw[:8] if len(raw) >= 8 else ""


def is_current_a_share_trading_day(payload: dict) -> bool:
    today = datetime.now(BEIJING_TZ).strftime("%Y%m%d")
    return any(quote_day(payload, code) == today for code in CORE_INDEX_CODES)


def index_stop_state(payload: dict) -> tuple[bool, int]:
    quotes = payload.get("quotes") or {}
    recovered = 0
    for code in CORE_INDEX_CODES:
        quote = quotes.get(code) or {}
        price = as_float(quote.get("price"))
        ma20 = as_float(quote.get("ma20"))
        change = as_float(quote.get("change_pct"))
        if price and ma20 and price >= ma20 and change >= 0:
            recovered += 1
    return recovered >= 2, recovered


def build_scan_summary(payload: dict, scan: dict) -> dict:
    breadth = scan.get("breadth") or {}
    advancers = int(breadth.get("advancers") or 0)
    decliners = int(breadth.get("decliners") or 0)
    scan_complete = int(scan.get("scanned_count") or 0) >= int(scan.get("min_rows_target") or 5000)
    stop_confirmed, recovered_count = index_stop_state(payload)
    breadth_healthy = advancers > decliners
    gate_open = scan_complete and breadth_healthy and stop_confirmed
    if not scan_complete:
        gate_reason = "全市场覆盖不足，候选只观察。"
    elif not breadth_healthy:
        gate_reason = "下跌家数占优，未形成可开仓的广度环境。"
    elif not stop_confirmed:
        gate_reason = "核心指数尚未确认止跌，候选只观察。"
    else:
        gate_reason = "覆盖、市场宽度和核心指数止跌条件同时通过；仍需次日人工确认。"
    tiers = broad_market_tiers(scan.get("results") or [], portfolio={})
    candidates = (tiers.get("actionable") or tiers.get("watch") or [])[:3]
    industries = scan.get("industry_breadth") or []
    strong = industries[:5]
    weak = list(reversed(industries[-5:]))
    return {
        "advancers": advancers,
        "decliners": decliners,
        "flat": int(breadth.get("flat") or 0),
        "sample_rows": int(breadth.get("sample_rows") or 0),
        "total_amount": breadth.get("total_amount") or 0,
        "scan_complete": scan_complete,
        "stop_confirmed": stop_confirmed,
        "recovered_count": recovered_count,
        "gate_open": gate_open,
        "gate_reason": gate_reason,
        "candidates": candidates,
        "actionable_candidates": (tiers.get("actionable") or [])[:3],
        "strong_industries": strong,
        "weak_industries": weak,
    }


def index_rows(payload: dict) -> str:
    rows = []
    for code in INDEX_CODES:
        quote = (payload.get("quotes") or {}).get(code) or {}
        change = quote.get("change_pct")
        change_value = as_float(change)
        # Follow the conventional A-share palette: red up, green down, gray flat.
        color = "#b42318" if change_value > 0 else "#18794e" if change_value < 0 else "#5d6b82"
        ma20 = quote.get("ma20")
        state = "MA20 上方" if as_float(quote.get("price")) >= as_float(ma20) and ma20 else "MA20 下方"
        rows.append(
            "<tr>"
            f"<td>{html.escape(code)}</td><td>{html.escape(str(quote.get('name') or '-'))}</td>"
            f"<td style=\"text-align:right\">{html.escape(fmt_price(quote.get('price')))}</td>"
            f"<td style=\"text-align:right;color:{color};font-weight:700\">{html.escape(fmt_pct(change))}</td>"
            f"<td style=\"text-align:right\">{html.escape(state)}</td></tr>"
        )
    return "".join(rows)


def industry_rows(items: list[dict]) -> str:
    if not items:
        return '<span style="color:#5d6b82">扫描不足，暂无行业结论。</span>'
    return "<br>".join(
        f"{html.escape(str(item.get('name') or '-'))} {html.escape(fmt_pct(item.get('avg_pct')))}"
        f"（{int(item.get('advancers') or 0)}/{int(item.get('decliners') or 0)}）"
        for item in items
    )


def candidate_rows(items: list[dict], gate_open: bool) -> str:
    if not items:
        return '<p style="color:#5d6b82">没有通过流动性与量价过滤的观察候选。</p>'
    rows = []
    for item in items:
        action = "次日人工复核" if gate_open else "仅观察，不生成买入计划"
        plan = (
            "条件：板块继续强于市场且不追高；失效：跌破当日低点或失去板块相对强度；"
            "仓位：以最新账户快照和单笔风险预算计算。"
            if gate_open else
            "交易闸门关闭：不提供入场区、止损线或仓位。"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('code') or '-'))} {html.escape(str(item.get('name') or ''))}</td>"
            f"<td>{html.escape(str(item.get('industry') or '-'))}</td>"
            f"<td style=\"text-align:right\">{html.escape(fmt_pct(item.get('pct_change')))}</td>"
            f"<td>{html.escape(action)}<br><span style=\"color:#5d6b82;font-size:12px\">{html.escape(plan)}</span></td>"
            "</tr>"
        )
    return "".join(rows)


def action_card_rows(items: list[dict], gate_open: bool) -> str:
    if not gate_open:
        return '<p style="color:#5d6b82">交易闸门关闭：不生成盘中买入动作卡。</p>'
    if not items:
        return '<p style="color:#5d6b82">闸门开放，但没有通过量价、流动性和主题过滤的标的。</p>'
    rows = []
    for item in items[:3]:
        evidence = (
            f"当日 {fmt_pct(item.get('pct_change'))}；"
            f"成交额 {fmt_amount(item.get('amount'))}"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('code') or '-'))} {html.escape(str(item.get('name') or ''))}</td>"
            f"<td>{html.escape(str(item.get('industry') or '-'))}</td>"
            f"<td>{html.escape(evidence)}</td>"
            "<td>只进入人工复核；不自动下单、不追涨、不给固定金额。</td>"
            "<td>板块转弱、冲高回落、跌破当日低点或数据不可验证即取消。</td>"
            "</tr>"
        )
    return "".join(rows)


def build_html(payload: dict, scan: dict, summary: dict, intraday: bool = False) -> str:
    generated_at = fmt_beijing_time(payload.get("generated_at"))
    status = payload.get("status") or {}
    gate_color = "#18794e" if summary["gate_open"] else "#b42318"
    gate_label = "允许进入次日人工复核" if summary["gate_open"] else "停止新增风险"
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"></head>
<body style="margin:0;background:#f4f6f8;color:#172033;font:14px Arial,'Microsoft YaHei',sans-serif;line-height:1.55">
  <main style="max-width:760px;margin:0 auto;padding:24px">
    <section style="background:#fff;border:1px solid #d8dee8;border-radius:8px;padding:22px">
      <h1 style="margin:0;font-size:24px">Niki 投资决策工作台</h1>
      <p style="color:#5d6b82">{'盘中动作卡扫描' if intraday else '盘后全市场扫描'} | {html.escape(generated_at)}</p>
      <div style="background:#eef5ff;border:1px solid #bdd3f4;padding:12px;border-radius:6px">
        <strong>使用顺序：</strong>先核对券商 App 的账户与可卖份额，再处理已有持仓；扫描报告用于建立次日观察池，不构成买入指令。
      </div>

      <h2 style="font-size:17px;margin-top:22px">1. 大盘与止跌（市场代理，非持仓）</h2>
      <p style="color:#5d6b82">行情快照：报价 {status.get('valid_quote_count', 0)}/{len(INDEX_CODES)}；日线 {status.get('valid_history_count', 0)}/{len(INDEX_CODES)}。涨跌幅为相对昨收的日内变化；这 5 只 ETF 只用于市场风控，并不等于你的账户持仓。核心指数 MA20 上方且当日非跌的数量：{summary['recovered_count']}/{len(CORE_INDEX_CODES)}。</p>
      <table style="width:100%;border-collapse:collapse"><thead><tr style="background:#f7f9fc"><th style="text-align:left;padding:8px">代码</th><th style="text-align:left;padding:8px">标的</th><th style="text-align:right;padding:8px">价格</th><th style="text-align:right;padding:8px">涨跌幅</th><th style="text-align:right;padding:8px">趋势</th></tr></thead><tbody>{index_rows(payload)}</tbody></table>

      <h2 style="font-size:17px;margin-top:22px">2. 风格与行业</h2>
      <p>全市场覆盖 {scan.get('scanned_count', 0)}/{scan.get('min_rows_target', 0)}；上涨 {summary['advancers']}，下跌 {summary['decliners']}，平盘 {summary['flat']}；样本成交额 {html.escape(fmt_amount(summary['total_amount']))}。</p>
      <div style="display:flex;gap:12px"><div style="flex:1;background:#eaf7ef;padding:10px;border-radius:6px"><strong>相对强势</strong><br>{industry_rows(summary['strong_industries'])}</div><div style="flex:1;background:#fff0ee;padding:10px;border-radius:6px"><strong>相对弱势</strong><br>{industry_rows(summary['weak_industries'])}</div></div>

      <h2 style="font-size:17px;margin-top:22px">3. 观察候选（最多 3 个）</h2>
      <table style="width:100%;border-collapse:collapse"><thead><tr style="background:#f7f9fc"><th style="text-align:left;padding:8px">标的</th><th style="text-align:left;padding:8px">行业</th><th style="text-align:right;padding:8px">涨跌幅</th><th style="text-align:left;padding:8px">处理</th></tr></thead><tbody>{candidate_rows(summary['candidates'], summary['gate_open'])}</tbody></table>

      {'<h2 style="font-size:17px;margin-top:22px">盘中动作卡（最多 3 张）</h2><table style="width:100%;border-collapse:collapse"><thead><tr style="background:#f7f9fc"><th style="text-align:left;padding:8px">标的</th><th style="text-align:left;padding:8px">行业</th><th style="text-align:left;padding:8px">触发证据</th><th style="text-align:left;padding:8px">处理</th><th style="text-align:left;padding:8px">失效条件</th></tr></thead><tbody>' + action_card_rows(summary['actionable_candidates'], summary['gate_open']) + '</tbody></table>' if intraday else ''}

      <h2 style="font-size:17px;margin-top:22px">4. 交易闸门</h2>
      <div style="border-left:4px solid {gate_color};background:#f7f9fc;padding:12px"><strong style="color:{gate_color}">{gate_label}</strong><br>{html.escape(summary['gate_reason'])}<br><span style="color:#5d6b82">规则：全市场覆盖、上涨家数占优、核心指数止跌三项同时满足，才允许为候选生成次日人工复核计划。</span></div>
      <p style="margin-top:20px;color:#5d6b82;font-size:12px">数据路由：腾讯实时行情 -> 通达信日线 -> 腾讯前复权 K 线 -> AKShare。本邮件不包含账户、持仓或个人配置；不连接券商、不自动下单、不承诺收益。</p>
    </section>
  </main>
</body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a manual workbench market-scan email")
    parser.add_argument("--dry-run", action="store_true", help="Generate and validate the preview without SMTP")
    parser.add_argument("--intraday", action="store_true", help="Include conditional intraday action cards")
    parser.add_argument("--force", action="store_true", help="Bypass the intraday time guard for manual resend/testing")
    args = parser.parse_args()

    if args.intraday and not args.force:
        allowed, reason = intraday_window_state()
        if not allowed:
            print(reason)
            return 0
        print("intraday_guard=pass " + reason)

    payload = snapshot(INDEX_CODES, bars=65)
    if args.intraday and not args.force and not is_current_a_share_trading_day(payload):
        print("skip_intraday=a_share_closed_or_quote_not_current")
        return 0

    scan = run_broad_market_scan(load_digital_infra_watchlist())
    summary = build_scan_summary(payload, scan)
    email_html = build_html(payload, scan, summary, intraday=args.intraday)
    if args.dry_run:
        print(
            f"dry_run=OK quotes={(payload.get('status') or {}).get('valid_quote_count', 0)}/{len(INDEX_CODES)} "
            f"scan={scan.get('scanned_count', 0)}/{scan.get('min_rows_target', 0)} gate_open={summary['gate_open']} intraday={args.intraday} html_bytes={len(email_html.encode('utf-8'))}"
        )
        return 0

    required = {name: os.getenv(name, "").strip() for name in ("SENDER_EMAIL", "SENDER_PASSWORD", "RECIPIENT_EMAIL")}
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise SystemExit("Missing required email configuration: " + ", ".join(missing))

    now = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d %H:%M 北京时间")
    notifier = EmailNotifier(
        sender_email=required["SENDER_EMAIL"],
        sender_password=required["SENDER_PASSWORD"],
        smtp_server=(os.getenv("SMTP_SERVER") or "smtp.qq.com").strip(),
        smtp_port=int((os.getenv("SMTP_PORT") or "587").strip()),
    )
    label = "盘中动作卡" if args.intraday else "盘后市场扫描"
    sent = notifier.send_html_alert(required["RECIPIENT_EMAIL"], f"Niki 决策工作台 | {label} | {now}", email_html)
    if not sent:
        raise SystemExit("SMTP did not accept the market-scan email")
    print("market_scan_email=sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
