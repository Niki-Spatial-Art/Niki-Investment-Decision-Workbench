"""Local trading workbench.

Run:
    python tools/local_dashboard.py --host 127.0.0.1 --port 8501

This dashboard is a local decision-support surface. It reads structured files
from the repo, does not connect to a broker, and never places orders.
"""

from __future__ import annotations

import argparse
import csv
import html
import importlib.util
import json
import math
import os
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.decision_state import local_decision_state, timestamp, BEIJING
REPORT = ROOT / "reports" / "latest.json"
REPORT_MD = ROOT / "reports" / "latest.md"
IFIND_CLEAN_DIR = ROOT / "reports" / "ifind_clean"
JOURNAL = ROOT / "data" / "paper_trade_journal.csv"
REAL_TRADE_JOURNAL = ROOT / "data" / "trade_journal.local.csv"
TRADE_ATTRIBUTIONS = ROOT / "data" / "trade_attributions.local.csv"
EVIDENCE_CARDS = ROOT / "data" / "research_evidence.local.json"
OPTION_JOURNAL = ROOT / "data" / "option_sim_journal.csv"
BROKER = ROOT / "data" / "broker_account_snapshots.local.json"
BROKER_FALLBACK = ROOT / "data" / "broker_account_snapshots.json"
A_STOCK_ROUTE = ROOT / "data" / "a_stock_radar_snapshot.json"
EASTMONEY_PROBE = ROOT / "data" / "latest_eastmoney_probe.json"
IFIND_PROBE = ROOT / "data" / "latest_ifind_http_probe.json"
IFIND_USAGE = ROOT / "data" / "ifind_usage_snapshot.json"
IFIND_STRUCTURE = ROOT / "data" / "ifind_market_structure.json"
XINGYAO = ROOT / "data" / "latest_xingyao_data_probe.json"
OPTION_CHAIN = ROOT / "data" / "latest_xingyao_option_chain.json"
OPTION_SURFACE = ROOT / "data" / "latest_xingyao_option_surface.json"
XINGYAO_STORE_MANIFEST = ROOT / "data" / "xingyao_research_store" / "latest_manifest.json"
XINGYAO_INTRADAY_ALERTS = ROOT / "data" / "latest_xingyao_intraday_alerts.json"
XINGYAO_DUCK_QUERY_MD = ROOT / "reports" / "latest_xingyao_duck_query.md"
YUHENG = ROOT / "data" / "latest_yuheng_probe.json"
LEARNING = ROOT / "reports" / "learning_intake.md"
POST_CLOSE = ROOT / "data" / "post_close_system_snapshot.json"
PROFIT_TARGETS = ROOT / "data" / "profit_targets.json"
DAILY_REVIEWS = ROOT / "reviews" / "daily"

REFRESH_LOCK = threading.Lock()
REFRESH_PROCESS: subprocess.Popen | None = None
REFRESH_STARTED_AT = ""


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def read_json(path: Path):
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {"_error": f"{type(exc).__name__}: {exc}"}


def read_broker_snapshot() -> dict:
    payload = read_json(BROKER)
    if payload and not payload.get("_error"):
        return payload
    return read_json(BROKER_FALLBACK)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def read_evidence_cards() -> list[dict]:
    """Read local-only candidate research cards without treating them as signals."""
    payload = read_json(EVIDENCE_CARDS)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        return [item for item in (payload.get("cards") or []) if isinstance(item, dict)]
    return []


def latest_real_trade() -> dict[str, str]:
    rows = [row for row in read_csv(REAL_TRADE_JOURNAL) if str(row.get("trade_time") or "").strip()]
    return max(rows, key=lambda row: str(row.get("trade_time") or "")) if rows else {}


def trade_key(row: dict) -> str:
    """Use the locally confirmed timestamp and code to join a fill to its review."""
    return f"{str(row.get('trade_time') or '').strip()}|{str(row.get('code') or '').strip()}"


def file_time(path: Path) -> str:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return "-"


def latest_backtest_path() -> Path:
    rows = sorted((ROOT / "reports").glob("ifind_position_backtest_*.json"), key=lambda p: p.stat().st_mtime)
    return rows[-1] if rows else ROOT / "reports" / "ifind_position_backtest_missing.json"


def latest_ifind_clean_radar_path() -> Path:
    rows = sorted(IFIND_CLEAN_DIR.glob("*_ifind_clean_radar.json"), key=lambda p: p.stat().st_mtime)
    return rows[-1] if rows else IFIND_CLEAN_DIR / "ifind_clean_radar_missing.json"


def latest_ifind_sample_path() -> Path:
    rows = sorted((ROOT / "data").glob("ifind_realtime_20_sample*.json"), key=lambda p: p.stat().st_mtime)
    if rows:
        return rows[-1]
    # quick20 now writes the realtime sample into latest_ifind_http_probe.json.
    # Fall back to it so the dashboard does not show an empty sample card.
    if IFIND_PROBE.exists():
        return IFIND_PROBE
    return ROOT / "data" / "ifind_realtime_20_sample.json"


def as_float(value, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def fmt_number(value, decimals: int = 2) -> str:
    return f"{as_float(value):,.{decimals}f}"


def fmt_money(value, decimals: int = 0) -> str:
    number = as_float(value)
    sign = "-" if number < 0 else ""
    return f"{sign}{abs(number):,.{decimals}f} 元"


def fmt_price(value) -> str:
    if value in (None, ""):
        return "-"
    number = as_float(value)
    if abs(number) >= 100:
        return f"{number:,.2f}"
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def pct(value) -> str:
    if value in (None, ""):
        return "-"
    return f"{as_float(value):.2f}%"


def pct_from_ratio(value) -> str:
    return pct(as_float(value) * 100)


def flag(value) -> bool:
    """Accept booleans and common local JSON spellings for review checklists."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "通过", "已核对"}


def evidence_assessment(item: dict) -> dict:
    """Assess completeness only; a complete card is still not a buy instruction."""
    sources = item.get("sources") or []
    if isinstance(sources, dict):
        sources = [sources]
    if isinstance(sources, str):
        sources = [{"title": sources}]
    sources = [source for source in sources if isinstance(source, dict)]
    valid_sources = [source for source in sources if source.get("title") or source.get("url")]
    timed_sources = [source for source in valid_sources if source.get("published_at") or source.get("captured_at")]
    data_checks = item.get("data_checks") or {}
    logic_checks = item.get("logic_checks") or {}
    required_data = {
        "价格": "price_verified",
        "均线": "ma20_verified",
        "流动性": "liquidity_verified",
        "持仓/可交易权限": "position_verified",
    }
    required_logic = {
        "市场状态": "market_regime_confirmed",
        "产业链共振": "sector_resonance_confirmed",
        "非追高位置": "not_chasing",
        "催化或基本面证据": "catalyst_verified",
    }
    missing = []
    if not valid_sources:
        missing.append("原始来源")
    elif not timed_sources:
        missing.append("来源时间")
    for label, field in {
        "供需/景气逻辑": "supply_demand_thesis",
        "市场未定价点": "market_mispricing",
        "反例": "counter_evidence",
        "触发条件": "trigger",
        "失效条件": "invalidation",
    }.items():
        if not str(item.get(field) or "").strip():
            missing.append(label)
    missing_data = [label for label, field in required_data.items() if not flag(data_checks.get(field))]
    missing_logic = [label for label, field in required_logic.items() if not flag(logic_checks.get(field))]
    missing.extend(f"数据:{label}" for label in missing_data)
    missing.extend(f"逻辑:{label}" for label in missing_logic)
    return {
        "sources": valid_sources,
        "data_passed": not missing_data,
        "logic_passed": not missing_logic and all(
            str(item.get(field) or "").strip()
            for field in ("supply_demand_thesis", "market_mispricing", "counter_evidence", "trigger", "invalidation")
        ) and bool(valid_sources) and bool(timed_sources),
        "ready": not missing,
        "missing": missing,
    }


def split_lines(text: str, limit: int | None = None) -> list[str]:
    raw = [line.strip(" -*\t") for line in str(text or "").replace("；", "\n").replace("。", "。\n").splitlines()]
    lines = [line for line in raw if line]
    return lines[:limit] if limit else lines


def latest_snapshot(payload: dict) -> dict:
    rows = payload.get("snapshots") if isinstance(payload, dict) else None
    if rows:
        valid_rows = [row for row in rows if isinstance(row, dict)]
        if valid_rows:
            # Imports can be newest-first or oldest-first; timestamps are authoritative.
            return max(valid_rows, key=lambda row: str(row.get("snapshot_time") or ""))
    return payload if isinstance(payload, dict) else {}


def is_etf_code(code: str) -> bool:
    return str(code or "").startswith(("510", "512", "513", "515", "516", "518", "588", "159"))


def broker_positions(broker: dict) -> list[dict]:
    return [
        pos for pos in (latest_snapshot(broker).get("positions_visible") or [])
        if as_float(pos.get("shares")) > 0
    ]


def latest_execution_note(broker: dict) -> str:
    trade = latest_real_trade()
    if not trade:
        return '<div class="decision-note"><strong>本地成交联动：</strong>尚未记录真实成交；先录入成交与券商快照，再执行持仓建议。</div>'
    code = str(trade.get("code") or "-")
    held = next((item for item in broker_positions(broker) if str(item.get("code") or "") == code), {})
    remaining = fmt_number(held.get("shares"), 0) if held else "0"
    price = fmt_price(trade.get("price"))
    shares = fmt_number(trade.get("shares"), 0)
    gross = fmt_money(trade.get("gross_amount"), 2)
    side = "卖出" if str(trade.get("side") or "").upper() == "SELL" else str(trade.get("side") or "成交")
    snapshot_time = latest_snapshot(broker).get("snapshot_time") or "-"
    return (
        '<div class="decision-note"><strong>本地成交联动：</strong>'
        f'{esc(trade.get("trade_time"))} 已确认{esc(side)} {esc(code)} {esc(shares)} 份，'
        f'成交价 {esc(price)}，金额约 {esc(gross)}。'
        f'券商快照 {esc(snapshot_time)} 显示剩余 {esc(remaining)} 份。'
        '该记录只保存在本机，用于持仓复核；不会上传或自动下单。'
        '</div>'
    )


def clean_items_by_code(clean_radar: dict) -> dict[str, dict]:
    return {str(item.get("code") or ""): item for item in (clean_radar.get("items") or [])}


def broker_watchlist(broker: dict) -> list[dict]:
    snap = latest_snapshot(broker)
    return [
        item for item in (snap.get("watchlist_visible") or [])
        if str(item.get("code") or "") and not is_etf_code(str(item.get("code") or ""))
    ]


def market_index_summary(snap: dict) -> str:
    rows = snap.get("market_indices") or []
    if not rows:
        return "上证小绿、深成小红、北证偏弱；旧ETF先处理，新A股先观察。"
    weak = [f"{row.get('name')} {pct(row.get('change_pct'))}" for row in rows if as_float(row.get("change_pct")) < 0]
    strong = [f"{row.get('name')} {pct(row.get('change_pct'))}" for row in rows if as_float(row.get("change_pct")) >= 0]
    parts = weak + strong
    return "；".join(parts[:4]) + "。"


def intraday_etf_action(pos: dict, clean_item: dict) -> tuple[str, str, str]:
    code = str(pos.get("code") or "")
    price = as_float(pos.get("price") or clean_item.get("close"))
    stop = as_float(clean_item.get("stop_loss"))
    sell_above = as_float(clean_item.get("sell_above"))
    daily = as_float(pos.get("daily_profit"))
    ref = as_float(pos.get("reference_profit"))
    if code == "588000" and price < 1.76:
        return "利润保护", "午间已跌破 1.76，不补仓；只看能否收回 1.75-1.76，不能收回就继续保护利润。", "warn"
    if code == "588000":
        return "持有", "利润仓，只守不加；1.76 下方不再补仓。", "ok"
    if code == "512100":
        if price < 3.32:
            return "持有/观察", "低于 3.32 修复线，不加仓；午后只看 3.25 防守和 3.30-3.32 反抽。", "warn"
        return "持有", "中证1000仍是主要仓位，站上修复线后再讨论加仓。", "ok"
    if code == "512000" and price < 0.48:
        return "风险线", "已在 0.48 风险线附近，不补仓；午后不能收回 0.48 就只做风险记录。", "danger"
    if code == "512000":
        return "等反弹减压", "弱修复，不补仓；只看 0.49-0.50 反弹区是否减压。", "warn"
    if code == "159870":
        return "持有等压", "今天相对抗跌，但仍是亏损修复仓；0.80 守住先持有，0.82-0.86 反弹区减压。", "warn"
    if code == "513130" and price < 0.59:
        return "风险观察", "低于 0.59，不补仓；午后只看是否收回 0.59。", "danger"
    if code == "513130":
        return "风险观察", "QDII 弱势仓，不补；0.59-0.62 只看风险，不主动扩大仓位。", "danger"
    if stop and price <= stop:
        return "风险线", f"现价 {fmt_price(price)} 已在/低于 {fmt_price(stop)}，先控风险。", "danger"
    if sell_above and price >= sell_above:
        return "处理线", f"现价接近 {fmt_price(sell_above)}，先考虑减压/保护收益。", "warn"
    if ref > 0 and daily >= 0:
        return "持有", "利润仓先看承接，不追加。", "ok"
    return "观察", "没有新买点，先等结构更清楚。", "warn"


def intraday_stock_action(item: dict) -> tuple[str, str, str]:
    code = str(item.get("code") or "")
    change = as_float(item.get("change_pct") if item.get("change_pct") is not None else item.get("change"))
    dist_ma20 = as_float(item.get("dist_ma20"))
    ret20 = as_float(item.get("ret20"))
    trend = as_float(item.get("trend_score"))
    if code in {"600183", "600027", "600110"} and change > 0:
        return "逆势观察", "指数普跌时能红，放观察池；但午后仍不追，只看承接。", "ok"
    if code in {"002463", "600183"}:
        return "观察不追", "AI硬件高位链分化，今天不做追高；只等回踩承接。", "warn"
    if code in {"002747"}:
        return "不抄底", "机器人/高位股今天回撤较深，先看是否止跌，不用盘中接。", "danger"
    if change <= -4:
        return "不接跌刀", "午间跌幅已经偏深，下午即使反抽也先当修复，不升级买入。", "danger"
    if change <= -2.5:
        return "观察不买", "弱于指数，下午只看能否止跌，不做主动买入。", "warn"
    if dist_ma20 > 10 or ret20 > 25:
        return "不追", "离均线仍远，强势票只做观察池，不升级买入。", "danger"
    if trend >= 2:
        return "等确认", "结构还在，但要等分时承接和板块共振。", "warn"
    return "观察", "没有盘中买点。", "warn"


def build_intraday_brief(report: dict, broker: dict, clean_radar: dict, option_chain: dict) -> str:
    snap = latest_snapshot(broker)
    positions = broker_positions(broker)
    item_by_code = clean_items_by_code(clean_radar)
    etf_positions = [pos for pos in positions if is_etf_code(str(pos.get("code") or ""))]
    stock_watch = broker_watchlist(broker)
    if not stock_watch:
        stock_watch = [
            item for item in (clean_radar.get("items") or [])
            if not is_etf_code(str(item.get("code") or "")) and as_float(item.get("shares")) <= 0
        ]
        stock_watch = sorted(
            stock_watch,
            key=lambda item: (-as_float(item.get("trend_score")), -as_float(item.get("ret20")), str(item.get("code") or "")),
        )[:6]
    else:
        stock_watch = sorted(stock_watch, key=lambda item: (as_float(item.get("change_pct")), str(item.get("code") or "")))[:8]

    account_cards = [
        card("账户资产", fmt_money(snap.get("total_assets") or snap.get("broker_total_capital"), 2), f"截图时间 {snap.get('snapshot_time') or '-'}。", "ok"),
        card("可用资金", fmt_money(snap.get("available_cash") or snap.get("cash"), 2), "现金很充足，但今天不是必须买。", "ok"),
        card("股票市值", fmt_money(snap.get("securities_market_value"), 2), f"当日盈亏 {fmt_money(snap.get('daily_profit'), 2)}。", "warn"),
        card("盘面判断", "午间防守", market_index_summary(snap), "danger" if as_float(snap.get("daily_profit")) < -500 else "warn"),
    ]

    etf_cards = []
    for pos in etf_positions:
        clean_item = item_by_code.get(str(pos.get("code") or "")) or {}
        action, note, tone = intraday_etf_action(pos, clean_item)
        etf_cards.append(
            status_source_card(
                f"{pos.get('code')} {pos.get('name') or ''}",
                action,
                f"持仓 {fmt_number(pos.get('shares'), 0)} | 现价 {fmt_price(pos.get('price') or clean_item.get('close'))} | 市值 {fmt_money(pos.get('market_value'), 0)}",
                note,
                f"当日 {fmt_money(pos.get('daily_profit'), 2)}；参考 {fmt_money(pos.get('reference_profit'), 2)}；可卖 {fmt_number(pos.get('available'), 0)}。",
                tone,
            )
        )

    stock_cards = []
    for item in stock_watch:
        action, note, tone = intraday_stock_action(item)
        stock_cards.append(
            status_source_card(
                f"{item.get('code')} {item.get('name') or ''}",
                action,
                (
                    f"现价 {fmt_price(item.get('price'))} | 涨跌幅 {pct(item.get('change_pct'))}"
                    if item.get("price") is not None
                    else f"趋势分 {fmt_number(item.get('trend_score'), 0)} | 20日 {pct(item.get('ret20'))} | 离MA20 {pct(item.get('dist_ma20'))}"
                ),
                note,
                "A股自选/主题票今天只用于观察强弱，不从这里直接下单。",
                tone,
            )
        )

    option_note = (
        f"星耀期权链 {len(option_chain.get('rows') or [])} 条，来源 {option_chain.get('snapshot_source') or '-'}；"
        "期权现在只做研究和风控参考，不放在盘中交易第一屏。"
    )
    body = (
        metric_grid(account_cards, "top-grid")
        + latest_execution_note(broker)
        + '<div class="decision-note"><strong>午间结论：</strong>当前实盘核心是 5 个ETF旧仓；A股自选只看强弱，不追跌后反抽；现金多不是买入理由。期权继续只做研究和风控参考。</div>'
        + "<h3>ETF持仓动作</h3>"
        + (source_grid(etf_cards, "wide") if etf_cards else '<div class="empty">没有ETF持仓。</div>')
        + "<h3>A股观察池</h3>"
        + (source_grid(stock_cards) if stock_cards else '<div class="empty">没有A股观察票。</div>')
        + '<div class="decision-note"><strong>期权位置：</strong>' + esc(option_note) + "</div>"
    )
    return section("盘中三件事", "账户、ETF持仓、A股观察先看这里", body, "intraday")


def is_stale(report: dict) -> bool:
    generated = (report.get("metadata") or {}).get("generated_at")
    if not generated:
        return True
    try:
        dt = datetime.strptime(generated, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True
    return (datetime.now() - dt).total_seconds() > 90 * 60


def card(title: str, value: str, note: str = "", tone: str = "") -> str:
    return f"""
    <article class="metric {esc(tone)}">
      <span>{esc(title)}</span>
      <strong>{esc(value)}</strong>
      <p>{esc(note)}</p>
    </article>
    """


def section(title: str, subtitle: str, body: str, section_id: str = "") -> str:
    id_attr = f' id="{esc(section_id)}"' if section_id else ""
    return f"""
    <section{id_attr} class="panel">
      <div class="panel-header">
        <h2>{esc(title)}</h2>
        <span>{esc(subtitle)}</span>
      </div>
      {body}
    </section>
    """


def metric_grid(items: list[str], klass: str = "") -> str:
    return f'<div class="metric-grid {esc(klass)}">{"".join(items)}</div>'


def source_grid(items: list[str], klass: str = "") -> str:
    return f'<div class="source-grid {esc(klass)}">{"".join(items)}</div>'


def source_card(title: str, meta: str, strong: str, note: str = "", tone: str = "") -> str:
    return f"""
    <article class="source-card {esc(tone)}">
      <div class="source-title">{esc(title)}</div>
      <div class="source-meta">{esc(meta)}</div>
      <strong>{esc(strong)}</strong>
      {f'<p>{esc(note)}</p>' if note else ''}
    </article>
    """


def prose_card(title: str, lines: list[str], meta: str = "", tone: str = "") -> str:
    body = "".join(f"<p>{esc(line)}</p>" for line in lines if line)
    return f"""
    <article class="source-card prose {esc(tone)}">
      <div class="source-title">{esc(title)}</div>
      {f'<div class="source-meta">{esc(meta)}</div>' if meta else ''}
      {body}
    </article>
    """


def status_source_card(title: str, badge: str, meta: str, strong: str, note: str = "", tone: str = "") -> str:
    return f"""
    <article class="source-card {esc(tone)}">
      <div class="source-head">
        <div class="source-title">{esc(title)}</div>
        <span class="tag {esc(tone)}">{esc(badge)}</span>
      </div>
      <div class="source-meta">{esc(meta)}</div>
      <strong>{esc(strong)}</strong>
      {f'<p>{esc(note)}</p>' if note else ''}
    </article>
    """


def pick_primary_stock_card(report: dict, broker: dict) -> dict:
    held_codes = {
        str(pos.get("code") or "")
        for pos in (latest_snapshot(broker).get("positions_visible") or [])
        if as_float(pos.get("shares")) > 0
    }
    for item in ((report.get("action_stack") or {}).get("short_term_cards") or []):
        code = str(item.get("code") or "")
        if code in held_codes:
            continue
        max_capital = as_float((item.get("position_permission") or {}).get("max_capital") or item.get("capital"))
        est_shares = as_float((item.get("position_permission") or {}).get("estimated_shares") or item.get("shares"))
        if max_capital > 0 and est_shares > 0:
            return item
    return {}


def classify_holding_action(pos: dict) -> tuple[str, str, str, int]:
    rule = str(pos.get("tomorrow_rule") or "")
    available = as_float(pos.get("available"))
    ref_profit = as_float(pos.get("reference_profit"))
    daily_profit = as_float(pos.get("daily_profit"))
    urgent_words = ("跌破", "止损", "减仓", "减风险", "减亏", "出清")
    profit_words = ("保护利润", "止盈", "落袋", "强势利润仓")
    if available <= 0:
        return ("等", "warn", "T+1 等明天", 0)
    if any(word in rule for word in urgent_words):
        if ref_profit <= 0 or daily_profit < 0:
            return ("卖/减", "danger", "先减风险", 1)
        return ("卖/减", "danger", "先锁利润", 2)
    if any(word in rule for word in profit_words):
        return ("卖/减", "warn", "冲高先收", 3)
    if "不加仓" in rule or "不补仓" in rule or "不是加仓线" in rule:
        if ref_profit > 0:
            return ("持有", "ok", "只持有不加仓", 4)
        return ("等", "warn", "反弹减亏", 5)
    if ref_profit > 0:
        return ("持有", "ok", "利润仓观察", 6)
    return ("等", "warn", "观察确认", 7)


def monthly_goal_snapshot(profit_targets: dict, broker: dict, report: dict) -> tuple[float, float, float, int, str]:
    target = as_float(profit_targets.get("monthly_sprint_target"))
    snap = latest_snapshot(broker)
    progress = as_float(snap.get("reference_profit"))
    if not progress:
        progress = as_float((report.get("portfolio") or {}).get("reference_profit"))
    gap = max(target - progress, 0.0)
    target_month = str(profit_targets.get("target_month") or "")
    report_month = str((report.get("metadata") or {}).get("generated_at") or "")[:7]
    if target_month and report_month and target_month != report_month:
        return target, progress, gap, 0, f"目标文件仍是 {target_month}，未切到 {report_month}"
    days_left = int(as_float(profit_targets.get("remaining_trading_days")))
    return target, progress, gap, days_left, ""


def clean_backtest_status(code: str, backtest_by_code: dict[str, dict]) -> tuple[str, str, str]:
    row = backtest_by_code.get(str(code)) or {}
    if not row:
        return "未回测", "当前回测文件还没覆盖这只，先按观察处理。", "warn"
    sample = row.get("similar_backtest") or {}
    win1 = pct(as_float(sample.get("next1_win_rate")) * 100)
    med2 = pct(as_float(sample.get("next2_median")) * 100)
    return "已回测", f"1日胜率 {win1}；2日中位 {med2}；状态 {row.get('regime') or '-'}。", "ok"


def clean_holding_action(item: dict) -> tuple[str, str, str]:
    close = as_float(item.get("close"), math.nan)
    stop_loss = as_float(item.get("stop_loss"), math.nan)
    sell_above = as_float(item.get("sell_above"), math.nan)
    buy_below = as_float(item.get("buy_below"), math.nan)
    pnl = as_float(item.get("pnl"))
    trend_score = as_float(item.get("trend_score"))
    if not math.isnan(stop_loss) and not math.isnan(close) and close <= stop_loss:
        return "卖", "danger", f"跌到止损线附近，先按 {fmt_price(stop_loss)} 风险线处理。"
    if not math.isnan(sell_above) and not math.isnan(close) and close >= sell_above:
        return "减/卖", "warn", f"到处理线附近，先看 {fmt_price(sell_above)} 一带减压或止盈。"
    if not math.isnan(buy_below) and not math.isnan(close) and close <= buy_below:
        return "等", "warn", f"碰到计划买线 {fmt_price(buy_below)}，但先不自动加仓，要等明日确认。"
    if trend_score < 0 and pnl <= 0:
        return "等/减反弹", "warn", "趋势偏弱且浮亏，优先等反弹减压，不做摊平。"
    if pnl > 0:
        return "持有", "ok", "利润仓继续看承接，不追着加。"
    return "等", "warn", "先看修复，不抢动作。"


def clean_watch_action(item: dict) -> tuple[str, str, str]:
    change = as_float(item.get("change"))
    dist_ma20 = as_float(item.get("dist_ma20"))
    trend_score = as_float(item.get("trend_score"))
    amount_ratio = as_float(item.get("amount_ratio"))
    ret20 = as_float(item.get("ret20"))
    if change >= 5 or dist_ma20 >= 4 or ret20 >= 15:
        return "不做", "danger", "明天默认不追高，只留强势观察。"
    if trend_score >= 4 and amount_ratio >= 1 and dist_ma20 > 0:
        return "等", "ok", "只等回踩确认，再决定要不要出新动作卡。"
    if trend_score >= 2 and dist_ma20 > 0:
        return "等", "warn", "结构还行，但还不到直接买的程度。"
    return "不做", "warn", "走势不够干净，先排除。"


def build_ifind_clean_panel(clean_radar: dict, backtest: dict) -> str:
    if not clean_radar or clean_radar.get("_error"):
        return section(
            "Clean Radar 动作台",
            "缺少干净 iFind 报告",
            '<div class="empty">先运行 python tools\\ifind_clean_radar.py，再刷新网页。</div>',
            "clean-radar",
        )

    items = clean_radar.get("items") or []
    holdings = [item for item in items if as_float(item.get("shares")) > 0]
    watch = [item for item in items if as_float(item.get("shares")) <= 0]
    strong_watch = sorted(
        [item for item in watch if as_float(item.get("trend_score")) >= 2 and as_float(item.get("dist_ma20")) > 0],
        key=lambda item: (-as_float(item.get("trend_score")), -as_float(item.get("change")), -as_float(item.get("amount_ratio"))),
    )
    backtest_by_code = {str(row.get("code")): row for row in (backtest.get("summaries") or [])}
    latest_path = latest_ifind_clean_radar_path()
    metrics = [
        card("Clean Radar", clean_radar.get("generated_at") or "-", f"文件 {file_time(latest_path)}；工作台优先信这个。", "ok"),
        card("持仓动作卡", str(len(holdings)), "先处理旧仓，不因旧 etf_radar 再误判。", "ok"),
        card("强势观察", str(len(strong_watch[:6])), "只保留明天还值得盯的强势票。", "ok" if strong_watch else "warn"),
        card("明日新开仓", "等", "默认不追高，只在回踩确认后再给新动作卡。", "warn"),
    ]

    holding_cards = []
    ranked_holdings = sorted(
        holdings,
        key=lambda item: (
            0 if as_float(item.get("close")) <= as_float(item.get("stop_loss"), math.inf) else 1,
            0 if as_float(item.get("close")) >= as_float(item.get("sell_above"), math.inf) else 1,
            as_float(item.get("trend_score")),
            as_float(item.get("pnl")),
        ),
    )
    for item in ranked_holdings:
        action, tone, headline = clean_holding_action(item)
        holding_cards.append(
            status_source_card(
                f"{item.get('code')} {item.get('name') or ''}",
                action,
                f"现价 {fmt_price(item.get('close'))} | 仓位 {pct(item.get('weight'))} | 浮盈亏 {fmt_money(item.get('pnl'), 0)}",
                headline,
                f"止损 {fmt_price(item.get('stop_loss'))}；处理线 {fmt_price(item.get('sell_above'))}；备注 {item.get('note') or '-'}",
                tone,
            )
        )

    watch_cards = []
    for item in strong_watch[:6]:
        action, tone, headline = clean_watch_action(item)
        bt_badge, bt_note, bt_tone = clean_backtest_status(str(item.get("code") or ""), backtest_by_code)
        note = (
            f"5日 {pct(item.get('ret5'))}；20日 {pct(item.get('ret20'))}；离 MA20 {pct(item.get('dist_ma20'))}；"
            f"量能 5/20 {as_float(item.get('amount_ratio')):.2f}；{bt_note}"
        )
        if bt_badge == "未回测" and tone == "ok":
            tone = "warn"
        watch_cards.append(
            status_source_card(
                f"{item.get('code')} {item.get('name') or ''}",
                f"{action} / {bt_badge}",
                f"涨跌 {pct(item.get('change'))} | 趋势分 {fmt_number(item.get('trend_score'), 0)} | 成交额 {fmt_money(item.get('amount'), 0)}",
                headline,
                note,
                "ok" if bt_tone == "ok" and tone == "ok" else tone,
            )
        )

    coverage_note = ""
    missing_backtests = [item.get("code") for item in strong_watch[:6] if str(item.get("code") or "") not in backtest_by_code]
    if missing_backtests:
        coverage_note = (
            '<div class="decision-note"><strong>回测覆盖提醒：</strong>'
            f"当前短线回测文件还没覆盖 {esc(' / '.join(str(code) for code in missing_backtests))}，"
            "所以这些强势票先只给“等 / 不做”，不直接升级成明日买入。</div>"
        )

    body = (
        metric_grid(metrics)
        + '<div class="decision-note"><strong>明日结论：</strong>新票默认 `等`，不追今天已经急拉的强势股；先处理旧弱仓，只对回踩确认的强势观察票再补新动作卡。</div>'
        + "<h3>持仓动作卡</h3>"
        + (source_grid(holding_cards) if holding_cards else '<div class="empty">当前没有持仓动作卡。</div>')
        + "<h3>强势观察</h3>"
        + (source_grid(watch_cards) if watch_cards else '<div class="empty">当前没有强势观察票。</div>')
        + coverage_note
    )
    subtitle = f"来源 {clean_radar.get('source') or 'iFind HTTP only'}；文件 {latest_path.name}"
    return section("Clean Radar 动作台", subtitle, body, "clean-radar")


def build_top_sync(report: dict, broker: dict, ifind_probe: dict, ifind_sample: dict, clean_radar: dict) -> str:
    meta = report.get("metadata") or {}
    snap = latest_snapshot(broker)
    probe_time = ifind_probe.get("generated_at") or "-"
    sample_time = ifind_sample.get("generated_at") or "-"
    clean_time = clean_radar.get("generated_at") or "-"
    stale = is_stale(report)
    a_stock = report.get("a_stock_data_status") or {}
    a_stock_status = a_stock.get("status") or {}
    a_stock_route = " -> ".join(a_stock.get("route") or []) or "not generated"
    status = "旧报告，仅复盘" if stale else "新鲜，可复核"
    command = (
        f'cd "{ROOT}"; $env:PYTHONIOENCODING="utf-8"; '
        "python .\\tools\\xingyao_data_probe.py; "
        "python .\\tools\\xingyao_option_analytics.py --expiry-limit 2 --strikes-each-side 2; "
        "python .\\tools\\xingyao_option_surface.py; "
        "python .\\tools\\xingyao_research_store.py --fetch-timeout 20; "
        "python .\\tools\\xingyao_intraday_alerts.py; "
        "python .\\tools\\a_stock_radar_snapshot.py; "
        "python .\\tools\\ifind_clean_radar.py; "
        "python .\\tools\\ifind_http_probe.py --all --wencai"
    )
    items = [
        card("工作台状态", "星耀盘中优先", f"页面渲染 {datetime.now():%Y-%m-%d %H:%M:%S}", "ok"),
        card("星耀研究链", file_time(XINGYAO_INTRADAY_ALERTS), f"研究库 {file_time(XINGYAO_STORE_MANIFEST)}；先看星耀快照、提醒和期权链。", "ok"),
        card("Clean Radar", clean_time, f"文件 {file_time(latest_ifind_clean_radar_path())}；现在放在星耀盘中主屏之后作候选明细。", "ok"),
        card("结构化报告", meta.get("generated_at") or "-", f"文件 {file_time(REPORT)}；latest.json 现在只作旧链路参考。", "warn" if stale else ""),
        card("券商快照", snap.get("snapshot_time") or "-", f"来源 {snap.get('source') or '-'}；持仓和可卖数量优先级最高", "ok"),
        card("本地A股路由", a_stock.get("generated_at") or "-", f"{a_stock_route}；报价 {a_stock_status.get('quote_coverage_pct', 0)}% / 日线 {a_stock_status.get('history_coverage_pct', 0)}%。", "ok" if a_stock.get("available") else "warn"),
        card("iFind实时样本", sample_time, f"探针 {probe_time}；现在主要做A股基础/公告/候选校验", "warn" if stale else "ok"),
    ]
    return f"""
    <section class="hero">
      <div>
        <h1>星耀执行盘</h1>
        <p>先看星耀实时层，再看ETF和A股动作卡。这个盘面是给你明天盘中直接用的。</p>
      </div>
      <div class="hero-actions">
        <button id="refresh-data" type="button">刷新雷达/接口</button>
        <a href="/api/status" target="_blank">状态 JSON</a>
      </div>
    </section>
    {metric_grid(items, "top-grid")}
    <div class="command-box">
      <strong>刷新命令</strong>
      <code>{esc(command)}</code>
      <span id="sync-note">页面每 30 秒轮询状态；先确认星耀链时间，再看ETF和A股动作卡。</span>
    </div>
    """


def build_new_entry_gate(report: dict) -> str:
    action_stack = report.get("action_stack") or {}
    gate = action_stack.get("new_entry_gate") or {}
    gates = gate.get("gates") or []
    blocked = bool(gate.get("blocked"))
    tone = "danger" if blocked else "ok"
    label = "停止新开仓" if blocked else "可进入人工复核"
    cards = [
        card("新开仓闸门", label, str(gate.get("reason") or "Waiting for the next radar refresh."), tone),
    ]
    for item in gates:
        item_tone = "danger" if item.get("blocked") else "ok"
        cards.append(card(str(item.get("level") or "gate"), "blocked" if item.get("blocked") else "passed", str(item.get("reason") or "-"), item_tone))
    note = "先修复数据和账户状态，再看题材；已有仓位的减仓、止损和持有复核不受此闸门阻断。"
    return section("执行闸门", "真实快照、行情新鲜度和全市场覆盖必须同时通过", metric_grid(cards) + f'<div class="decision-note">{esc(note)}</div>', "entry-gate")


def build_execution_panel(
    report: dict,
    broker: dict,
    eastmoney: dict,
    ifind_probe: dict,
    ifind_sample: dict,
    xingyao: dict,
    profit_targets: dict,
) -> str:
    session = report.get("session") or {}
    meta = report.get("metadata") or {}
    snap = latest_snapshot(broker)
    positions = [p for p in (snap.get("positions_visible") or []) if as_float(p.get("shares")) > 0]
    ranked_positions = sorted(
        positions,
        key=lambda pos: (
            classify_holding_action(pos)[3],
            -abs(as_float(pos.get("reference_profit"))),
            -abs(as_float(pos.get("daily_profit"))),
        ),
    )
    action_stack = report.get("action_stack") or {}
    risk_gate = action_stack.get("risk_gate") or {}
    stop_gate = action_stack.get("stop_new_entries_gate") or {}
    tactical_cash = action_stack.get("tactical_cash_decision") or {}
    stale = is_stale(report)
    primary_card = pick_primary_stock_card(report, broker)
    target, progress, gap, days_left, month_note = monthly_goal_snapshot(profit_targets, broker, report)
    east_status = str(eastmoney.get("status") or "-")
    sample_time = ifind_sample.get("generated_at") or "-"
    probe_time = ifind_probe.get("generated_at") or "-"
    option_basic = xingyao.get("option_basic") or {}
    derivative_note = "期货未接入实盘"
    if option_basic.get("contract_count"):
        derivative_note = f"期权仿真可用，合约 {option_basic.get('contract_count')}"
    month_detail = month_note if month_note else f"剩余 {days_left or '-'} 个交易日。"

    if stale:
        buy_badge, buy_tone = "等", "warn"
        buy_headline = "先刷新，不开新仓"
        buy_detail = f"报告时间 {meta.get('generated_at') or '-'} 已偏旧。"
    elif risk_gate.get("blocked") or stop_gate.get("blocked"):
        buy_badge, buy_tone = "停手", "danger"
        buy_headline = "停止新开仓"
        buy_detail = str(stop_gate.get("reason") or risk_gate.get("reason") or "风险闸门关闭")
    elif primary_card:
        buy_badge, buy_tone = "买", "ok"
        buy_headline = f"只看 {primary_card.get('code')} {primary_card.get('name') or ''}"
        buy_detail = (
            f"区间 {fmt_price(primary_card.get('entry_low'))}-{fmt_price(primary_card.get('entry_high'))}；"
            f"止损 {fmt_price(primary_card.get('stop_loss'))}；"
            f"止盈 {fmt_price(primary_card.get('take_profit_1'))}/{fmt_price(primary_card.get('take_profit_2'))}"
        )
    else:
        buy_badge, buy_tone = "等", "warn"
        buy_headline = "当前无新买点"
        buy_detail = "今天只处理持仓，不为月目标强开仓。"

    top_pos = ranked_positions[0] if ranked_positions else {}
    top_action, top_tone, top_summary, _ = classify_holding_action(top_pos) if top_pos else ("等", "warn", "暂无持仓", 9)
    hero_cards = [
        card("新开仓", buy_badge, buy_headline + "；" + buy_detail, buy_tone),
        card(
            "持仓优先动作",
            top_action,
            (
                f"{top_pos.get('code')} {top_pos.get('name') or ''}：{top_summary}；{str(top_pos.get('tomorrow_rule') or '')}"
                if top_pos else
                "暂无持仓，等待下一次候选生成。"
            ),
            top_tone,
        ),
        card(
            "数据接口",
            "可执行" if not stale else "需刷新",
            f"iFind {sample_time}；探针 {probe_time}；东财 {east_status}；{derivative_note}",
            "ok" if not stale and east_status == "OK" else "warn",
        ),
        card(
            "月冲刺 6 万",
            fmt_money(gap, 0),
            f"目标 {fmt_money(target, 0)}；当前参考收益 {fmt_money(progress, 0)}；{month_detail}",
            "warn" if gap > 0 else "ok",
        ),
        card(
            "下一刷新",
            str(session.get('next_decision_time') or tactical_cash.get('next_check') or '-'),
            f"战术现金最多可准备 {fmt_money(tactical_cash.get('allowed_transfer_amount'), 0)}；只在确认窗口执行。",
            "",
        ),
    ]

    hold_cards = []
    for pos in ranked_positions[:4]:
        label, tone, summary, _ = classify_holding_action(pos)
        hold_cards.append(
            status_source_card(
                f"{pos.get('code')} {pos.get('name') or ''}",
                label,
                f"持仓 {fmt_number(pos.get('shares'), 0)} 股 | 可卖 {fmt_number(pos.get('available'), 0)} | 现价 {fmt_price(pos.get('price'))}",
                summary,
                str(pos.get("tomorrow_rule") or ""),
                tone,
            )
        )

    note = """
    <div class="decision-note">
      <strong>盘中用法：</strong>每小时刷新一次，先看“新开仓”和“持仓优先动作”；只有在数据可执行、风险闸门打开、动作卡还有效时，才进入买卖确认。
    </div>
    """
    body = metric_grid(hero_cards, "execution-grid") + note + (source_grid(hold_cards) if hold_cards else '<div class="empty">当前没有持仓需要排队处理。</div>')
    return section("执行总控台", "先看 买 / 卖 / 等 / 停", body, "execution")


def journal_realized_today(rows: list[dict[str, str]], report_date: str) -> float:
    total = 0.0
    for row in rows:
        if str(row.get("date") or "") != report_date:
            continue
        entry = as_float(row.get("actual_entry_price"))
        exit_price = as_float(row.get("actual_exit_price"))
        shares = as_float(row.get("actual_shares"))
        if entry and exit_price and shares:
            total += (exit_price - entry) * shares
    return total


def build_pnl_reconciliation(report: dict, broker: dict, journal: list[dict[str, str]]) -> str:
    meta = report.get("metadata") or {}
    report_date = str(meta.get("generated_at") or datetime.now().strftime("%Y-%m-%d"))[:10]
    portfolio = report.get("portfolio") or {}
    snap = latest_snapshot(broker)
    positions = snap.get("positions_visible") or []
    broker_daily = as_float(snap.get("daily_profit"))
    broker_ref = as_float(snap.get("reference_profit"))
    old_report_daily = as_float(portfolio.get("daily_profit"))
    old_report_time = portfolio.get("snapshot_time") or "-"
    position_daily = sum(as_float(pos.get("daily_profit")) for pos in positions)
    realized = journal_realized_today(journal, report_date)
    spread = max(abs(old_report_daily - broker_daily), abs(position_daily - broker_daily))
    verdict = "今日盈亏口径冲突，先对账，不用 128 或 2000+ 单独下结论。" if spread > 500 else "口径差异较小，可继续复盘。"
    rows = [
        card("券商当日盈亏", fmt_money(broker_daily, 2), "最新券商快照字段；之前 128 元来自这里。", "warn"),
        card("旧报告组合口径", fmt_money(old_report_daily, 2), f"来自 latest.json 的 portfolio，快照时间 {old_report_time}，可能就是你看到的两千多。", "danger"),
        card("券商参考盈亏", fmt_money(broker_ref, 2), "更像浮动/参考收益，不等于当日已实现。", "ok"),
        card("逐仓当日合计", fmt_money(position_daily, 2), "把当前可见持仓日盈亏相加；会漏掉已卖出、手续费和券商重算。", "danger"),
        card("今日已实现估算", fmt_money(realized, 2), "按模拟日志已卖出记录估算；诺德300股止盈已计入。", "ok"),
    ]
    note = f"""
    <div class="decision-note">
      <strong>{esc(verdict)}</strong>
      明天复盘必须补三项：券商资产最终截图、当日成交流水、持仓浮盈明细。顶部不再只显示“今日盈亏 128 元”，改成多口径对账。
    </div>
    """
    return section("今日盈亏复盘", "把 128、2000+、参考盈亏拆开看", note + metric_grid(rows), "pnl")


def tomorrow_priority_for(pos: dict, backtest_by_code: dict[str, dict]) -> tuple[str, str]:
    code = str(pos.get("code") or "")
    rule = str(pos.get("tomorrow_rule") or "明早先刷新价格，再按止盈/止损线处理。")
    bt = backtest_by_code.get(code) or {}
    regime = str(bt.get("regime") or "")
    win1 = as_float((bt.get("similar_backtest") or {}).get("next1_win_rate")) * 100
    med2 = as_float((bt.get("similar_backtest") or {}).get("next2_median")) * 100
    if code == "600021":
        return (
            "已有持仓，不是加仓",
            f"{rule} 回测显示强势延续但次日胜率仅 {pct(win1)}、两日中位 {pct(med2)}，所以只做持仓处理，不因旧动作卡再买。",
        )
    if code == "588000":
        return (
            "ETF利润保护",
            f"{rule} 回测样本偏强但当前被归为{regime or '震荡'}，只分批止盈，不补仓。",
        )
    if code in {"512000", "159870", "513130", "512100"}:
        return ("ETF清理线", f"{rule} 弱势或震荡ETF只看防线和反弹减亏，不补仓摊薄。")
    if as_float(pos.get("available")) <= 0:
        return ("T+1不可卖", f"{rule} 今日买入可用为0，明天才处理，不能盘中焦虑卖。")
    return ("持仓纪律", rule)


def build_tomorrow_plan(broker: dict, backtest: dict) -> str:
    snap = latest_snapshot(broker)
    positions = [p for p in (snap.get("positions_visible") or []) if as_float(p.get("shares")) > 0]
    backtest_by_code = {str(row.get("code")): row for row in (backtest.get("summaries") or [])}
    priority = ["600021", "600110", "002241", "603083", "000725", "588000", "512100", "513130", "159870", "512000", "518880"]
    positions.sort(key=lambda p: priority.index(str(p.get("code"))) if str(p.get("code")) in priority else 99)
    rows = []
    for pos in positions:
        title, rule = tomorrow_priority_for(pos, backtest_by_code)
        code = str(pos.get("code") or "")
        rows.append(
            source_card(
                f"{code} {pos.get('name') or ''}",
                f"{title} | 持仓 {fmt_number(pos.get('shares'), 0)} 股 | 可卖 {fmt_number(pos.get('available'), 0)} | 价 {fmt_price(pos.get('price'))}",
                rule,
                f"成本 {fmt_price(pos.get('cost'))}；当日 {fmt_money(pos.get('daily_profit'), 2)}；参考 {fmt_money(pos.get('reference_profit'), 2)}。",
                "warn" if code == "600021" else "",
            )
        )
    return section("明日计划", "先处理持仓风险，再考虑新机会", source_grid(rows), "tomorrow")


def check_status(checks: dict, key: str) -> str:
    item = checks.get(key)
    if item is None:
        return "本轮未运行"
    return "已接通" if item.get("ok") else "未接通"


def check_rows(checks: dict, key: str) -> str:
    item = checks.get(key) or {}
    rows = item.get("row_estimate")
    return "-" if rows is None else str(rows)


def check_tone(checks: dict, key: str) -> str:
    item = checks.get(key)
    if item is None:
        return "warn"
    return "ok" if item.get("ok") else "danger"


def probe_note(probe: dict, key: str) -> str:
    item = (probe.get("checks") or {}).get(key)
    if item is None:
        return "本轮没有执行这一项；这表示未运行，不表示接口坏了。"
    return "辅助决策位置已映射到本地工作台。"


def probe_meta(probe: dict, key: str) -> str:
    item = (probe.get("checks") or {}).get(key)
    if item is None:
        return f"rows~-；{probe.get('generated_at') or '-'}；未运行"
    rows = item.get("row_estimate")
    return f"rows~{rows if rows is not None else '-'}；{probe.get('generated_at') or '-'}"


def list_check(payload: dict, name: str) -> dict:
    for item in payload.get("checks") or []:
        if item.get("name") == name:
            return item
    return {}


def build_interface_row(report: dict, broker: dict, eastmoney: dict, ifind_probe: dict, ifind_sample: dict, xingyao: dict, yuheng: dict) -> str:
    checks = ifind_probe.get("checks") or {}
    coverage = report.get("coverage") or {}
    option_basic = xingyao.get("option_basic") or {}
    snapshot_probe = xingyao.get("snapshot_probe") or {}
    kline_probe = xingyao.get("kline_probe") or {}
    y_inventory = yuheng.get("inventory") or {}
    east_stock = list_check(eastmoney, "eastmoney_stock_push2delay")
    east_broad = list_check(eastmoney, "eastmoney_clist_push2delay")
    east_status = eastmoney.get("status") or "-"
    east_tone = "ok" if east_status == "OK" else ("warn" if east_status == "DEGRADED" else "danger")
    xingyao_snapshot_tone = "ok" if snapshot_probe.get("valid_quote_count") or snapshot_probe.get("row_count") else "warn"
    xingyao_option_tone = "ok" if option_basic.get("contract_count") else "warn"
    cards = [
        source_card("东方财富行情", east_status, "主入口/备用入口分开探测；备用 push2delay 可用时标记为降级可读。", f"单票 {east_stock.get('rows', 0)} 行；全市场页 {east_broad.get('rows', 0)} 行；探针 {eastmoney.get('generated_at') or '-'}。", east_tone),
        source_card("iFind实时行情", check_status(checks, "realtime_quotes"), "用于 9:40 复核持仓价、动作卡价、量价是否仍成立。", f"样本 {ifind_sample.get('generated_at') or '-'}；rows {check_rows((ifind_sample.get('checks') or {}), 'realtime_quotes')}。", "ok"),
        source_card("iFind基础/公告", f"基础 {check_status(checks, 'basic_data')} / 公告 {check_status(checks, 'report_query')}", "用于买入前排除 ST、停牌、板块权限、公告风险。", f"探针 {ifind_probe.get('generated_at') or '-'}。", "ok"),
        source_card("全市场扫描", f"{coverage.get('broad_rows_seen') or '-'} / {coverage.get('broad_scan_min_rows_target') or 5000}", "覆盖不足时动作卡全部降级观察；当前只把扫描当候选来源。", f"候选 {coverage.get('broad_candidates') or '-'}；缺口 {coverage.get('broad_missing_estimate') or 0}。", "ok"),
        source_card("券商快照", latest_snapshot(broker).get("snapshot_time") or "-", "持仓、可卖数量、当日盈亏的最高优先级证据。", "仍是手动/截图链路，未连接真实券商下单。", "warn"),
        source_card("星耀TGW快照", f"快照 {snapshot_probe.get('valid_quote_count') or snapshot_probe.get('row_count') or 0}；K线 {kline_probe.get('row_count') or 0}", "ETF/A股快照已单独验证；K线仍待星耀技术确认 QueryKline 参数。", xingyao.get("run_id") or "-", xingyao_snapshot_tone),
        source_card("星耀期权基础", f"基础合约 {option_basic.get('contract_count') or 0}；实时权利金 0", "只能做合约匹配和仿真；基础合约缓存不是实时盘口，也没有 IV/Greeks/OI。", option_basic.get("cache_updated_at") or "-", xingyao_option_tone),
        source_card("玉衡仿真", yuheng.get("status") or "-", "用于期权仿真客户端存在性、合约字典和日志复核；不读密码目录，不下单。", f"目录 {len((y_inventory.get('directories') or []))}；文件 {len((y_inventory.get('files') or []))}。", "ok"),
    ]
    return section("数据接口第三排", "先确认接口是否正常，再看动作", source_grid(cards, "interfaces"), "interfaces")


def build_xingyao_intraday_panel(alerts: dict, manifest: dict) -> str:
    if not alerts and not manifest:
        return section("星耀盘中研究", "等待研究库和提醒生成", '<div class="empty">运行 python tools\\xingyao_research_store.py 和 python tools\\xingyao_intraday_alerts.py。</div>', "xingyao-research")
    manifest = manifest or {}
    errors = manifest.get("errors") or {}
    table_status = manifest.get("table_status") or {}
    fresh_rows = manifest.get("fresh_rows") or {}
    live_errors = [name for name, message in errors.items() if message]
    live_note = "TGW 本轮正常写入。"
    live_tone = "ok"
    if live_errors:
        live_note = "TGW 本轮超时/失败：" + "；".join(f"{name} {errors.get(name)}" for name in live_errors[:3])
        live_tone = "warn"
    status_text = (
        f"snapshot {table_status.get('snapshot') or '-'} / "
        f"min1 {table_status.get('kline_min1') or '-'} / "
        f"day {table_status.get('kline_day') or '-'} / "
        f"option {table_status.get('option_chain') or '-'}"
    )
    alert_cards = []
    for item in (alerts.get("alerts") or [])[:6]:
        tone = str(item.get("tone") or "warn")
        delta = item.get("daily_profit_change_from_prev")
        delta_text = "" if delta is None else f"；较上次 {fmt_money(delta, 2)}"
        alert_cards.append(
            status_source_card(
                f"{item.get('code')} {item.get('name') or ''}",
                str(item.get("title") or "观察"),
                f"现价 {fmt_price(item.get('price'))} | 当日 {fmt_money(item.get('daily_profit'), 2)}{delta_text}",
                str(item.get("action") or ""),
                "这是盘中提醒，不是自动交易指令；仍需结合盘口和尾盘确认。",
                tone,
            )
        )
    watch_cards = []
    for item in (alerts.get("watch_alerts") or [])[:6]:
        watch_cards.append(
            status_source_card(
                f"{item.get('code')} {item.get('name') or ''}",
                str(item.get("title") or "观察"),
                f"涨跌幅 {pct(item.get('change_pct'))}",
                "只进观察池，不升级成买入。",
                "强票等回踩，弱票不接跌后反抽。",
                str(item.get("tone") or "warn"),
            )
        )
    store = alerts.get("xingyao_store") or manifest or {}
    query_note = (
        f"SQLite {((manifest.get('paths') or {}).get('sqlite') if manifest else '') or '-'}；"
        f"DuckDB 查询报告 {file_time(XINGYAO_DUCK_QUERY_MD)}。"
    )
    store_cards = [
        card("提醒时间", alerts.get("snapshot_time") or "-", f"生成 {alerts.get('generated_at') or '-'}。", "ok" if alerts else "warn"),
        card("当日盈亏变化", fmt_money(alerts.get("daily_profit_change_from_prev"), 2), f"最新当日盈亏 {fmt_money(alerts.get('daily_profit'), 2)}。", "ok" if as_float(alerts.get("daily_profit_change_from_prev")) > 0 else "warn"),
        card("研究库行数", f"{store.get('snapshot_rows') or 0}/{store.get('kline_min1_rows') or 0}/{store.get('kline_day_rows') or 0}/{store.get('option_chain_rows') or 0}", f"snapshot / min1 / day / option；{status_text}。", "ok"),
        card("本轮新取数", f"{fresh_rows.get('snapshot') or 0}/{fresh_rows.get('kline_min1') or 0}/{fresh_rows.get('kline_day') or 0}/{fresh_rows.get('option_chain') or 0}", live_note, live_tone),
        card("查询层", "DuckDB/SQLite", query_note, "ok" if manifest else "warn"),
    ]
    body = (
        metric_grid(store_cards, "top-grid")
        + '<div class="decision-note"><strong>下午结论：</strong>不新买。星耀午后主要用于修工作台、保留研究缓存、盘后复盘和明日动作卡，不把期权研究误放成盘中买入信号。</div>'
        + "<h3>ETF持仓提醒</h3>"
        + (source_grid(alert_cards, "wide") if alert_cards else '<div class="empty">暂无ETF提醒。</div>')
        + "<h3>A股观察提醒</h3>"
        + (source_grid(watch_cards) if watch_cards else '<div class="empty">暂无A股异动提醒。</div>')
    )
    return section("星耀盘中研究", "A股/ETF优先；期权只做研究和风控", body, "xingyao-research")


def build_action_cards(report: dict, broker: dict, post_close: dict | None = None) -> str:
    cards = ((report.get("action_stack") or {}).get("short_term_cards") or [])
    stop_gate = ((report.get("action_stack") or {}).get("stop_new_entries_gate") or {})
    held_codes = {str(p.get("code")) for p in (latest_snapshot(broker).get("positions_visible") or []) if as_float(p.get("shares")) > 0}
    buy_codes_today = {
        str(item.get("symbol") or "")
        for item in ((post_close or {}).get("orders_today") or [])
        if str(item.get("side") or "").lower() == "buy" and str(item.get("filled_quantity") or "0") not in {"", "0", "0.0"}
    }
    stale = is_stale(report)
    cards = sorted(cards, key=lambda item: (str(item.get("code") or "") not in buy_codes_today, ))
    rows = []
    for item in cards:
        code = str(item.get("code") or "")
        held = code in held_codes
        decision = str(item.get("decision") or "")
        permission = "原信号有效，但当前卡已过窗口，仅供复盘" if stale else "待人工二次确认"
        if held:
            permission = "已有持仓，不是加仓指令"
        title = f"{code} {item.get('name') or ''}"
        meta = f"{permission} | 原始结论 {decision} | 主线 {item.get('layer_resonance_score', '-')} / 执行 {item.get('execution_quality_score', '-')}"
        if code in buy_codes_today:
            meta += " | 今日新买入优先显示"
        add_on_text = "允许" if item.get("add_on_allowed") else "不允许"
        turtle_line = (
            f"风险预算 {fmt_money(item.get('risk_per_trade_amount'), 0)}；"
            f"止损亏损约 {fmt_money(item.get('max_loss_if_stopped'), 0)}；"
            f"止损反推 {int(as_float(item.get('sized_by_stop_shares')))} 股；"
            f"加仓 {add_on_text}。"
        )
        if stale:
            strong = "这张卡对应的是盘中窗口信号；收盘后不能直接拿来下明天单，只能用于复盘和生成明日新卡。"
        elif held:
            strong = f"先处理既有仓位；若要加仓，必须重新生成明日动作卡。{turtle_line}"
        else:
            strong = (
                f"买入区 {fmt_price(item.get('entry_low'))}-{fmt_price(item.get('entry_high'))}；"
                f"止盈 {fmt_price(item.get('take_profit_1'))}/{fmt_price(item.get('take_profit_2'))}；"
                f"止损 {fmt_price(item.get('stop_loss'))}。{turtle_line}"
            )
        note_parts = [str(item.get("reason") or "")]
        if item.get("add_on_reason"):
            note_parts.append(str(item.get("add_on_reason")))
        rows.append(source_card(title, meta, strong, "；".join(part for part in note_parts if part), "danger" if stale else ("warn" if held else "")))
    if not rows:
        rows.append(source_card("暂无动作卡", "-", "先刷新雷达。"))
    subtitle = "旧动作卡默认展开；持仓标的不会被误读成加仓"
    if stop_gate:
        subtitle += f"；今日试错失败 {stop_gate.get('failed_count_today', 0)}/{stop_gate.get('failure_limit', 2)}"
        if stop_gate.get("blocked"):
            subtitle += "，新开仓已停手"
    return section("旧动作卡归因", subtitle, source_grid(rows), "cards")


def build_tactical_cash(report: dict) -> str:
    decision = ((report.get("action_stack") or {}).get("tactical_cash_decision") or {})
    if not decision:
        return section("战术现金调度", "等待下一次雷达生成", '<div class="empty">先运行 python monitor.py，让 latest.json 写入 tactical_cash_decision。</div>', "tactical-cash")
    status = str(decision.get("status") or "")
    tone = "ok" if status in {"ALLOW_BATCH_1", "CONDITIONAL_BATCH_1"} else ("warn" if status == "WATCH" else "")
    cards = [
        card("调度结论", str(decision.get("label") or "-"), str(decision.get("reason") or ""), tone),
        card("总可调动资金", fmt_money(decision.get("total_callable_capital"), 0), f"场内 {fmt_money(decision.get('on_exchange_asset_snapshot'), 0)} + 战术现金 {fmt_money(decision.get('tactical_callable_cash'), 0)}", tone),
        card("本次允许转入", fmt_money(decision.get("allowed_transfer_amount"), 0), f"战术现金总额 {fmt_money(decision.get('tactical_callable_cash'), 0)}；单批 {fmt_money(decision.get('batch_size'), 0)}", tone),
        card("动作卡数量", f"{decision.get('actionable_count', 0)} 可执行 / {decision.get('watchable_count', 0)} 接近", "只看 09:40/10:45 二次确认后的动作卡。", ""),
        card("下一检查", str(decision.get("next_check") or "-"), "不用于摊平宽基/弱ETF，不用于补救盘中情绪。", ""),
    ]
    forbidden = decision.get("forbidden_uses") or []
    note = ""
    if forbidden:
        note = '<div class="decision-note"><strong>禁止用途：</strong><ol>' + "".join(f"<li>{esc(item)}</li>" for item in forbidden[:6]) + "</ol></div>"
    return section("战术现金调度", "15万随时可进账户，但只按信号分批", metric_grid(cards) + note, "tactical-cash")


def build_risk_budget(report: dict, broker: dict, route: dict) -> str:
    budget = ((report.get("action_stack") or {}).get("risk_budget") or {})
    if not budget:
        return section("风险预算", "等待下一次雷达生成", '<div class="empty">先刷新雷达，生成单笔风险、试错额度和停手线。</div>', "risk-budget")
    status = str(budget.get("status") or "")
    freshness, _ = snapshot_age_label(latest_snapshot(broker))
    route_status = route.get("status") or {}
    route_available = bool(route.get("available")) or as_float(route_status.get("valid_quote_count")) > 0
    state = decision_checks(report, broker, route)
    executable = status == "TRIAL_ALLOWED" and state["review_allowed"]
    reason = str(budget.get("reason") or "")
    if not executable:
        if freshness != "新鲜":
            reason = f"券商快照为{freshness}，先以券商 App 更新总览和可卖份额。"
        elif not route_available:
            reason = "本地行情快照未生成或不完整。"
    if not executable:
        reason = "；".join(reason for key in ("data", "market", "setup", "account") for reason in state[key]["reasons"]) or reason
    label = str(budget.get("label") or "-") if executable else "额度归零，等待复核"
    tone = "ok" if executable else ("danger" if status == "DAILY_STOP" else "warn")
    today_allowed_loss = budget.get("today_allowed_loss") if executable else 0
    today_trial_capital = budget.get("today_trial_capital") if executable else 0
    cards = [
        card("今日状态", label, reason, tone),
        card("单笔最大亏损", fmt_money(today_allowed_loss, 0), f"完整风险预算 {fmt_money(budget.get('single_trial_loss'), 0)}；先算亏多少，再谈买多少。", tone),
        card("本次试错额度", fmt_money(today_trial_capital, 0), f"累计试错上限 {fmt_money(budget.get('max_cumulative_trial_capital'), 0)}。", tone),
        card("日停手线", fmt_money(budget.get("daily_loss_stop"), 0), f"月度回撤线 {fmt_money(budget.get('monthly_drawdown_stop'), 0)}。触线后不新增风险。", "warn"),
    ]
    note = (
        '<div class="decision-note"><strong>预算规则：</strong>'
        + esc(budget.get("profit_target_note") or "收益目标不触发买入。")
        + " 券商快照、行情快照或市场闸门任一未通过时，本次试错额度自动归零；已有持仓只做风险复核，不被当成加仓指令。</div>"
    )
    return section("风险预算", "先定义可承受损失，再决定是否产生候选和动作卡", metric_grid(cards) + note, "risk-budget")


def build_backtest_panel(backtest: dict) -> str:
    if not backtest or backtest.get("_error"):
        return section("iFind短线回测", "缺少回测文件", '<div class="empty">运行 python tools\\ifind_position_backtest.py --days 120 后再看。</div>', "backtest")
    names = backtest.get("names") or {}
    rows = backtest.get("summaries") or []
    held = [r for r in rows if as_float(r.get("position_shares")) > 0]
    weak = [names.get(r.get("code"), r.get("code")) for r in held if str(r.get("regime")) in {"弱势下行", "震荡偏弱"}]
    strong = [names.get(r.get("code"), r.get("code")) for r in held if str(r.get("regime")) in {"强势延续", "震荡偏强"}]
    advice = []
    for row in held:
        code = str(row.get("code"))
        name = names.get(code, code)
        bt = row.get("similar_backtest") or {}
        win1 = as_float(bt.get("next1_win_rate")) * 100
        med2 = as_float(bt.get("next2_median")) * 100
        regime = str(row.get("regime") or "")
        if code == "600021":
            advice.append(f"{name}: 强势延续但次日胜率 {pct(win1)}、两日中位 {pct(med2)}，只处理已有500股，不加仓。")
        elif code == "588000":
            advice.append(f"{name}: 样本次日胜率 {pct(win1)}，但处于{regime}，守1.90，1.98-2.00分批止盈，不追。")
        elif regime in {"弱势下行", "震荡偏弱"}:
            advice.append(f"{name}: {regime}，只看减仓/止损线，不补仓。")
        else:
            advice.append(f"{name}: {regime}，先保护利润，只有回踩承接确认才继续持有。")
    summary = f"""
    <div class="decision-note">
      <strong>回测反馈到明天下单：</strong>
      <ol>{"".join(f"<li>{esc(x)}</li>" for x in advice[:8])}</ol>
      <p>偏强：{esc(" / ".join(str(x) for x in strong) or "-")}；偏弱：{esc(" / ".join(str(x) for x in weak) or "-")}。</p>
    </div>
    """
    cards = []
    priority = {"600021", "588000", "600110", "000725", "002241", "603083", "512100", "513130", "159870", "512000", "518880"}
    for row in [r for r in rows if str(r.get("code")) in priority]:
        bt = row.get("similar_backtest") or {}
        cards.append(
            source_card(
                f"{row.get('code')} {names.get(row.get('code'), row.get('code'))}",
                f"{row.get('regime') or '-'} | 收盘 {fmt_price(row.get('close'))}",
                f"5日 {pct_from_ratio(row.get('ret_5d'))}；20日 {pct_from_ratio(row.get('ret_20d'))}；MA20距离 {pct_from_ratio(row.get('distance_ma20'))}",
                f"相似1日胜率 {pct(as_float(bt.get('next1_win_rate')) * 100)}；相似2日中位 {pct(as_float(bt.get('next2_median')) * 100)}。处理线：守 {fmt_price(row.get('protect_line'))}；反弹看 {fmt_price(row.get('rebound_line'))}。",
            )
        )
    subtitle = f"更新 {backtest.get('generated_at') or '-'}；区间 {backtest.get('start_date') or '-'} 至 {backtest.get('end_date') or '-'}"
    return section("iFind短线回测", subtitle, summary + source_grid(cards), "backtest")


def build_holdings_table(broker: dict) -> str:
    snap = latest_snapshot(broker)
    freshness, _ = snapshot_age_label(snap)
    positions = [p for p in (snap.get("positions_visible") or []) if as_float(p.get("shares")) > 0]
    rows = []
    for pos in positions:
        code = str(pos.get("code") or "")
        rows.append(
            f"""
            <tr class="{'watch-row' if code in {'600021', '588000'} else ''}">
              <td><strong>{esc(code)}</strong><br>{esc(pos.get('name') or '')}</td>
              <td>{fmt_number(pos.get('shares'), 0)}</td>
              <td>{fmt_number(pos.get('available'), 0)}</td>
              <td>{fmt_price(pos.get('price'))}</td>
              <td>{fmt_money(pos.get('daily_profit'), 2)}</td>
              <td>{fmt_money(pos.get('reference_profit'), 2)}</td>
              <td>{esc(pos.get('tomorrow_rule') or '')}</td>
            </tr>
            """
        )
    table = f"""
    <div class="table-wrap">
      <table>
        <thead><tr><th>标的</th><th>持仓</th><th>可卖</th><th>价格</th><th>当日</th><th>参考</th><th>明日处理线</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
    </div>
    """
    return section("券商快照明细", f"快照 {snap.get('snapshot_time') or '-'}（{freshness}）；请以券商 App 当前持仓为准", table, "holdings-detail")


def build_ifind_use_plan(usage: dict, probe: dict) -> str:
    checks = probe.get("checks") or {}
    tasks = usage.get("super_command_tasks") or []
    quotas = usage.get("quotas") or []
    scorecard = usage.get("scorecard") or []
    probe_cards = []
    labels = [
        ("鉴权", "access_token"),
        ("实时行情", "realtime_quotes"),
        ("基础数据", "basic_data"),
        ("智能选股", "smart_stock_picking"),
        ("历史行情", "history_quotes"),
        ("日内快照", "snap_shot"),
        ("公告查询", "report_query"),
    ]
    for label, key in labels:
        probe_cards.append(source_card(label, check_status(checks, key), probe_note(probe, key), probe_meta(probe, key), check_tone(checks, key)))
    task_cards = [
        source_card(
            task.get("module") or "-",
            task.get("status") or "-",
            task.get("ui_path") or "-",
            f"样本：{task.get('sample') or '-'}；字段：{task.get('expected_output') or '-'}；落地：{task.get('local_target') or '-'}",
        )
        for task in tasks
    ]
    quota_cards = [
        source_card(
            item.get("name") or "-",
            f"{item.get('used') or 0} / {item.get('quota') or '-'}",
            item.get("decision_use") or item.get("plan") or "按额度纪律使用。",
            item.get("note") or "",
            "warn" if as_float(item.get("used_pct")) > 25 else "",
        )
        for item in quotas[:8]
    ]
    score_cards = [source_card(row.get("metric") or "-", row.get("status") or "-", row.get("target") or "-") for row in scorecard]
    body = (
        '<div class="decision-note"><strong>iFind要体现为能力，不停在标题：</strong>实时行情校验价格，基础数据排除不可交易，历史/日内快照反馈到买卖线，智能选股只做候选交叉验证，公告查询做事件风险闸门。</div>'
        + source_grid(probe_cards, "compact")
        + "<h3>本月要用透的具体任务</h3>"
        + source_grid(task_cards)
        + "<h3>额度和价值检查</h3>"
        + source_grid(score_cards + quota_cards)
    )
    return section("iFind用透", "账号里可用的东西要落到决策动作", body, "ifind")


def build_data_upgrade_plan(report: dict, ifind_probe: dict, xingyao: dict, yuheng: dict) -> str:
    coverage = report.get("coverage") or {}
    checks = ifind_probe.get("checks") or {}
    rows = [
        ("A股全市场行情", f"东财扫描 {coverage.get('broad_rows_seen') or '-'}；iFind实时 {check_status(checks, 'realtime_quotes')}", "决定候选池覆盖、涨幅不过热、成交额和量比是否可信", "开盘前和9:40必须刷新，否则动作卡降级观察"),
        ("ETF/QDII", f"ETF监控 {coverage.get('etf_watch_count') or '-'}；QDII仍需溢价/IOPV", "决定588000、512100、513130、QDII是否持有/减仓/不追", "补QDII溢价、汇率和隔夜美股联动字段"),
        ("期权数据", f"星耀基础合约 {((xingyao.get('option_basic') or {}).get('contract_count') or 0)}；玉衡 {yuheng.get('status') or '-'}", "只做仿真和合约匹配；没有权利金/IV/Greeks/OI不进实盘", "向银河确认实时期权链权限"),
        ("财报与基本面", f"iFind基础 {check_status(checks, 'basic_data')}；公告 {check_status(checks, 'report_query')}", "短线买入前避开财报、停牌、重大公告、ST/退市风险", "把公告关键字检查接到动作卡生成前"),
        ("新闻与舆情", "研究证据池，非直接买入信号", "只做主线证据，不允许单条新闻触发交易", "接RSS/公告摘要，和量价共振后才进入候选"),
        ("数据校验规则", "主源 + 备用源 + 券商快照", "价格不准就不交易；缓存、估算、实时必须分开标注", "页面持续显示来源、时间、缺口、是否可执行"),
    ]
    cards = [source_card(name, status, use, next_step) for name, status, use, next_step in rows]
    return section("数据底座升级", "每一层都写清楚当前状态、决策用途和下一步", source_grid(cards), "data-upgrade")


def build_post_close_snapshot(snapshot: dict) -> str:
    if not snapshot:
        return section("收盘快照", "等待券商截图结构化录入", '<div class="empty">尚未读取 data/post_close_system_snapshot.json。</div>', "post-close")
    account = snapshot.get("account") or {}
    freshness = snapshot.get("freshness") or {}
    positions = snapshot.get("positions") or []
    active_positions = [p for p in positions if as_float(p.get("market_value")) > 0]
    largest_loss = min(active_positions, key=lambda p: as_float(p.get("reference_pnl")), default={})
    largest_value = max(active_positions, key=lambda p: as_float(p.get("market_value")), default={})
    cash_surplus = as_float(account.get("cash_above_tactical_floor"))
    tone = "ok" if cash_surplus >= 0 else "danger"
    cards = [
        card("总资产", fmt_money(account.get("total_assets"), 2), f"截图更新时间 {freshness.get('account') or '-'}", "ok"),
        card("现金", fmt_money(account.get("cash"), 2), f"现金比例 {pct(account.get('cash_ratio_pct'))}", tone),
        card("股票市值", fmt_money(account.get("stock_market_value"), 2), f"股票暴露 {pct(account.get('stock_exposure_pct'))}", ""),
        card("战术现金余量", fmt_money(cash_surplus, 2), f"底线 {fmt_money(account.get('tactical_cash_floor'), 0)}", tone),
        card("最大亏损源", f"{largest_loss.get('symbol', '-')} {largest_loss.get('name', '')}", fmt_money(largest_loss.get("reference_pnl"), 2), "danger"),
        card("最大市值仓", f"{largest_value.get('symbol', '-')} {largest_value.get('name', '')}", fmt_money(largest_value.get("market_value"), 2), "warn"),
    ]
    orders = snapshot.get("orders_today") or []
    order_rows = []
    for order in orders:
        order_rows.append(
            f"<tr><td>{esc(order.get('time'))}</td><td>{esc(order.get('side'))}</td><td>{esc(order.get('symbol'))} {esc(order.get('name'))}</td><td>{fmt_price(order.get('order_price'))}</td><td>{fmt_number(order.get('filled_quantity'), 0)}</td><td>{esc(order.get('status'))}</td></tr>"
        )
    orders_table = """
    <div class="table-wrap">
      <table>
        <thead><tr><th>时间</th><th>方向</th><th>标的</th><th>成交价</th><th>成交量</th><th>状态</th></tr></thead>
        <tbody>{}</tbody>
      </table>
    </div>
    """.format("".join(order_rows) if order_rows else '<tr><td colspan="6">暂无今日成交录入</td></tr>')
    note = """
    <div class="decision-note">
      <strong>明日优先级：</strong>先处理 513130 恒生科技ETF、159870 化工ETF、600027 华电国际；不沿用旧动作卡直接开新仓。现金高于 15 万底线，但超出部分不等于必须买入。
    </div>
    """
    return section("收盘快照", f"{snapshot.get('date') or '-'} 券商截图结构化", metric_grid(cards) + orders_table + note, "post-close")


def build_toolchain_status(snapshot: dict) -> str:
    toolchain = snapshot.get("toolchain_status") or {}
    vibe = toolchain.get("vibe_trading") or {}
    lean = toolchain.get("quantconnect_lean") or {}
    finance = toolchain.get("finance_skills") or {}
    skill_root = Path.home() / ".codex" / "skills"
    core_skills = [
        "personal-quant-command-center",
        "tonghuashun-ifind-skill",
        "vibe-trading",
        "quantconnect-lean",
        "trade-journal",
        "factor-research",
        "risk-analysis",
    ]
    installed_skills = [name for name in core_skills if (skill_root / name).exists()]
    routes = finance.get("routing") or installed_skills
    lean_cli = lean.get("cli_version") or ("已安装" if shutil.which("lean") else "-")
    docker_cli = lean.get("docker_image") or ("已安装" if shutil.which("docker") else "-")
    vibe_skill_installed = (skill_root / "vibe-trading").exists()
    vibe_py_installed = importlib.util.find_spec("vibe_trading") is not None
    rows = [
        source_card("Vibe-Trading", f"skill={vibe_skill_installed} / python包={vibe_py_installed}", vibe.get("role") or "trade journal, shadow account, factor research", "Codex skill 已安装时可用于研究流程；Python 包未装不等于 skill 没装，只是本项目运行时暂不能 import。", "ok" if vibe_skill_installed else "warn"),
        source_card("QuantConnect LEAN", f"cli={lean_cli} / docker={docker_cli}", "sample backtest passed" if lean.get("sample_backtest_passed") else "本机 CLI 已可用，样例回测待验证", lean.get("role") or "local research/backtest engine", "ok" if shutil.which("lean") and shutil.which("docker") else "warn"),
        source_card("金融 skill 路由", f"{len(routes)} 个核心路由", " / ".join(routes[:6]) if routes else "未录入", "后续回答买卖、复盘、风控、回测时按这些技能分流。", "ok" if routes else "warn"),
        source_card("展示规则", "已接入", "每次盘面显示数据源、工具链、是否验证、下一步用途", "避免 skill 只是安装了但没有进入日常决策流。", "ok"),
    ]
    return section("技能与量化工具链", "新安装能力必须在盘面可见", source_grid(rows), "toolchain")


def build_research_skill_plan() -> str:
    rows = [
        ("交易研究Skill", "已接入个人量化工作流", "回答买卖/复盘/网页更新时，按券商快照、iFind校验、风险优先、人工确认执行。", "作用：防止旧邮件、旧动作卡、旧持仓互相打架。"),
        ("盘面回放", "用iFind + 东财关键时间快照", "9:25、9:40、10:45、14:40保存价格、量比、成交额、行业强弱。", "作用：复盘“该买没买、买了会怎样、过滤条件是否挡风险”。"),
        ("回测闭环", "iFind历史回测已进入页面", "强势、弱势、相似次日胜率、两日中位都反馈到持仓处理线。", "作用：上海电力这类强势但次日胜率低的标的，不因旧卡加仓。"),
        ("海龟纪律层", "已纳入当前复盘逻辑", "先算止损，再反推仓位；只对确认盈利仓讨论加仓；连续试错失败后停手。", "作用：把仓位规则、止损纪律、加减仓标准写成机械流程。"),
        ("学习摄取", "书/论文/项目进入证据池", "只沉淀规则，例如IV贵不贵、Theta损耗、回撤阈值、回测偏差。", "作用：提高判断框架，不直接给买卖。"),
    ]
    cards = [source_card(*row) for row in rows]
    return section("研究技能与盘面回测", "具体能力要落到辅助决策", source_grid(cards), "research")


def build_showcase_notes() -> str:
    cards = [
        prose_card(
            "对外一句话",
            [
                "这是一个“少架构”的个人 A股盘中交易决策工作台：iFind 做高质量数据底座，通义千问做可选中文策略简报，本地工作台把盘中动作卡、回测反馈、纸面交易日志和每日维护记录放在一起。",
            ],
            "适合下午交流时开场介绍",
            "ok",
        ),
        prose_card(
            "相关的五点",
            [
                "少架构：没有引入复杂服务链路，优先用本地文件、探针和一个浏览器工作台闭环。",
                "数据透明：每张卡都显示数据来源、更新时间、是否可执行，而不是只给一个买卖结论。",
                "人工确认：系统只生成动作卡和风险线，不连接券商、不自动下单。",
                "可复盘：没买的票也保留 no-trade 样本，用来校正过滤条件。",
                "可降级：没有 iFind 时仍能作为交易纪律模板使用，但高置信动作卡会降级。",
            ],
            "README.md / 对外展示亮点",
        ),
        prose_card(
            "iFind 授权边界",
            [
                "公开仓库只提供框架和探针；使用者都须配置自己的 iFind 账号和接口权限。",
                "没有 iFind 也能作为交易纪律模板运行，但实时行情、历史回测、公告闸门和智能选股会降级，不能输出高置信买入动作卡。",
            ],
            "docs/ifind_auth_and_privacy.md",
            "warn",
        ),
        prose_card(
            "阿里云 API",
            [
                "当前已接入的是通义千问 / DashScope OpenAI-compatible Chat Completions，用于中文策略简报、风险提醒和盘后总结。",
                "默认 provider=qwen，模型 qwen-plus，环境变量 DASHSCOPE_API_KEY，端点 https://dashscope.aliyuncs.com/compatible-mode/v1。",
                "当前重点展示百炼 / DashScope 在中文策略简报、风险提醒和盘后复盘里的作用。",
            ],
            "docs/alicloud_api_usage.md",
        ),
        prose_card(
            "优秀案例借鉴",
            [
                "OpenBB：借鉴金融数据统一入口和 AI Agent 友好的数据平台思路。",
                "QuantConnect LEAN：借鉴研究、回测、执行分层，以及策略必须可验证的工程流程。",
                "Freqtrade：借鉴 dry-run、回测、风险保护、配置边界和 WebUI 思路。",
                "MongoDB / AI Search：借鉴更少架构的方向，未来可把动作卡、复盘和公告摘要做成统一检索层。",
            ],
            "docs/reference_cases.md",
        ),
    ]
    body = (
        '<div class="decision-note"><strong>会场说明：</strong>这部分用于快速介绍项目边界、阿里云 API 使用和参考案例；适合演示前先过一遍。</div>'
        + source_grid(cards, "wide")
    )
    return section("会场说明", "iFind边界、阿里云API、优秀案例和少架构定位", body, "showcase")


def build_market_structure(report: dict) -> str:
    policy = report.get("market_structure_policy") or {}
    structure = read_json(IFIND_STRUCTURE)
    breadth = structure.get("breadth") or {}
    cards = [
        card("结构状态", f"{policy.get('regime') or '-'} / {policy.get('risk_level') or '-'}", f"来源 {policy.get('source') or '-'}；日期 {policy.get('as_of') or '-'}", "ok"),
        card("追高阈值", pct(policy.get("chase_limit_pct")), "超过阈值不追，尤其旧动作卡。", "warn"),
        card("涨跌/样本", f"{breadth.get('advancers', '-')} / {breadth.get('decliners', '-')}", f"扫描 {breadth.get('scan_seen') or '-'} / {breadth.get('scan_target') or '-'}", ""),
    ]
    pref = " / ".join(policy.get("preferred_industries") or [])
    avoid = " / ".join(policy.get("avoid_industries") or [])
    body = metric_grid(cards) + source_grid([
        source_card("优先方向", "只提高观察优先级", pref or "-", "不等于买入。"),
        source_card("降级方向", "降低追高权限", avoid or "-", "AI/半导体过热时尤其要等回踩。"),
    ])
    return section("iFind市场结构", "风格、宽度、行业强弱反馈到短线纪律", body, "structure")


def build_etf_radar(report: dict) -> str:
    rows = []
    for item in (report.get("results") or [])[:18]:
        strategy = item.get("strategy") or {}
        reasons = "；".join((strategy.get("reasons") or []) + (item.get("reasons") or []))
        rows.append(
            source_card(
                f"{item.get('code')} {item.get('name')}",
                f"{item.get('level') or '-'} | 最新价 {fmt_price(item.get('price'))} | 涨跌 {pct(item.get('pct_change'))}",
                item.get("action") or strategy.get("decision") or "观察",
                reasons[:260],
                "danger" if str(item.get("level")).upper() == "RED" else "",
            )
        )
    return section("ETF雷达", "完整展开，但只保留可执行纪律", source_grid(rows), "etf")


def build_options(report: dict, xingyao: dict, yuheng: dict, option_chain: dict, option_surface: dict, research_manifest: dict) -> str:
    option = report.get("option_sim_radar") or {}
    chain_rows = option_chain.get("rows") or []
    chain_summary = option_chain.get("summary") or []
    term_structure = option_surface.get("term_structure") or []
    skew_summary = option_surface.get("skew_summary") or []
    manifest_paths = (research_manifest.get("paths") or {}) if isinstance(research_manifest, dict) else {}
    real_cards = []
    for row in chain_rows[:12]:
        greek_line = (
            f"IV {pct_from_ratio(row.get('implied_volatility'))} | "
            f"Delta {fmt_number(row.get('delta'), 3)} | "
            f"Gamma {fmt_number(row.get('gamma'), 4)} | "
            f"Theta/日 {fmt_number(row.get('theta_per_day'), 4)} | "
            f"Vega {fmt_number(row.get('vega_per_1pct_vol'), 4)}"
        )
        quote_line = (
            f"中间价 {fmt_price(row.get('mark_price'))} ({row.get('mark_source') or '-'}) | "
            f"买一/卖一 {fmt_price(row.get('bid_price1'))}/{fmt_price(row.get('ask_price1'))} | "
            f"OI {fmt_number(row.get('open_interest'), 0)} | 成交量 {fmt_number(row.get('volume'), 0)}"
        )
        real_cards.append(
            source_card(
                f"{row.get('underlying_code')} {row.get('direction_cn')} {fmt_price(row.get('strike'))}",
                f"真实链 | 到期 {row.get('expiry_date') or '-'} | {row.get('option_code') or '-'}",
                quote_line,
                f"{greek_line}；标的 {fmt_price(row.get('underlying_price'))}；虚值/实值 {pct(row.get('moneyness_pct'))}",
                "ok" if row.get("implied_volatility") else "warn",
            )
        )
    rows = []
    for item in option.get("results") or []:
        code = item.get("code") or "-"
        name = item.get("name") or "-"
        direction = item.get("direction") or "-"
        contract_code = item.get("xingyao_contract_code")
        contract_name = item.get("xingyao_contract_name")
        premium = item.get("premium")
        contract_cost = item.get("contract_cost")
        break_even = item.get("break_even")
        match_note = (
            f"星耀合约：{contract_code} {contract_name or ''}".strip()
            if contract_code
            else "星耀缓存未匹配：通常是该ETF不在当前星耀基础合约缓存中，或该月份/行权价不存在。"
        )
        quote_note = item.get("quote_note") or "权利金仍为模拟估算，不是真实期权链报价。"
        rows.append(
            source_card(
                f"{code} {name} {direction}",
                f"仿真 | 行权 {fmt_price(item.get('strike'))} | 到期 {item.get('expiry') or option.get('expiry') or '-'}",
                f"权利金 {fmt_price(premium)} / 张约 {fmt_money(contract_cost)}；盈亏平衡 {fmt_price(break_even)}；最大亏损 {fmt_money(item.get('max_loss'))}",
                f"{item.get('suitability') or ''}；{match_note}；{quote_note}",
                "" if contract_code else "warn",
            )
        )
    matched = sum(1 for item in option.get("results") or [] if item.get("xingyao_contract_code"))
    total = len(option.get("results") or [])
    if chain_rows:
        real_status = f"真实期权链 {len(chain_rows)} 条；摘要 {len(chain_summary)} 组；"
    else:
        real_status = "真实期权链未生成；"
    surface_status = f"曲面 {len(term_structure)} 个期限组 / {len(skew_summary)} 个偏斜组；" if term_structure or skew_summary else "曲面未生成；"
    store_status = (
        f"研究库 快照 {research_manifest.get('snapshot_rows') or 0} / min1 {research_manifest.get('kline_min1_rows') or 0} / day {research_manifest.get('kline_day_rows') or 0} / 期权 {research_manifest.get('option_chain_rows') or 0}。"
        if research_manifest
        else "研究库存储未生成。"
    )
    status = f"星耀基础合约 {((xingyao.get('option_basic') or {}).get('contract_count') or 0)}；本页匹配 {matched}/{total}；{real_status}{surface_status}玉衡 {yuheng.get('status') or '-'}；{store_status}"
    note = """
    <div class="decision-note">
      <strong>为什么之前显示 None / 0 / 未匹配：</strong>
      页面字段读错了。报告里的字段是 code、name、direction、premium、contract_cost、break_even、xingyao_contract_code；
      旧页面去读 underlying、option_type、premium_cost、breakeven、xingyao_contract，所以显示空。现在已按真实 JSON 字段渲染。
      另外 512100 在当前星耀基础合约缓存里没有合约记录，所以它会保留为“未匹配”，不能用于星耀合约训练。
    </div>
    """
    if chain_summary:
        summary_cards = []
        for item in chain_summary[:8]:
            summary_cards.append(
                source_card(
                    f"{item.get('underlying_code')} {item.get('underlying_name') or ''}",
                    f"到期 {item.get('expiry_date') or '-'} | 合约 {item.get('contracts') or 0}",
                    f"PCR OI {fmt_number(item.get('put_call_oi_ratio'), 2)} | PCR Vol {fmt_number(item.get('put_call_volume_ratio'), 2)}",
                    f"ATM跨式 {fmt_price(item.get('atm_straddle_mid'))}；隐含波动幅度 {pct(item.get('atm_implied_move_pct'))}；沽减购IV偏斜 {pct(item.get('atm_put_minus_call_iv_pct'))}",
                    "ok",
                )
            )
        note += "<h3>真实期权链摘要</h3>" + source_grid(summary_cards)
    if term_structure:
        surface_cards = []
        for item in term_structure[:8]:
            surface_cards.append(
                source_card(
                    f"{item.get('underlying_code')} {item.get('underlying_name') or ''}",
                    f"期限结构 | 近端 {item.get('near_expiry') or '-'} -> 远端 {item.get('far_expiry') or '-'}",
                    f"ATM IV {pct_from_ratio(item.get('near_atm_iv'))} -> {pct_from_ratio(item.get('far_atm_iv'))}",
                    f"斜率 {fmt_number(item.get('term_slope_pct_per_day'), 4)} pct/天；近端跨式 {fmt_price(item.get('near_atm_straddle'))}；远端跨式 {fmt_price(item.get('far_atm_straddle'))}",
                    "ok",
                )
            )
        note += "<h3>期限结构</h3>" + source_grid(surface_cards)
    if skew_summary:
        skew_cards = []
        for item in skew_summary[:8]:
            skew_cards.append(
                source_card(
                    f"{item.get('underlying_code')} {item.get('underlying_name') or ''}",
                    f"波动率偏斜 | 到期 {item.get('expiry_date') or '-'}",
                    f"25Δ 风险逆转 {pct(item.get('risk_reversal_25d_pct'))} | Butterfly {pct(item.get('butterfly_25d_pct'))}",
                    f"ATM Call/Put IV {pct_from_ratio(item.get('atm_call_iv'))}/{pct_from_ratio(item.get('atm_put_iv'))}；近25Δ Call/Put {pct_from_ratio(item.get('call_25d_iv'))}/{pct_from_ratio(item.get('put_25d_iv'))}",
                    "ok" if item.get("risk_reversal_25d_pct") not in (None, "") else "warn",
                )
            )
        note += "<h3>偏斜结构</h3>" + source_grid(skew_cards)
    if real_cards:
        note += "<h3>真实期权链样本</h3>" + source_grid(real_cards)
    if research_manifest:
        note += (
            "<h3>研究库存储</h3>"
            + source_grid(
                [
                    source_card(
                        "本地研究库",
                        f"生成时间 {research_manifest.get('generated_at') or '-'}",
                        f"快照 {research_manifest.get('snapshot_rows') or 0}；min1 {research_manifest.get('kline_min1_rows') or 0}；day {research_manifest.get('kline_day_rows') or 0}；期权 {research_manifest.get('option_chain_rows') or 0}",
                        f"SQLite {manifest_paths.get('sqlite') or '-'}；快照文件 {manifest_paths.get('snapshot') or '-'}",
                        "ok",
                    )
                ]
            )
        )
    note += "<h3>仿真视角</h3>"
    return section("期权仿真与真实链", status, note + source_grid(rows), "options")


def build_ai_summary(report: dict) -> str:
    lines = split_lines(report.get("ai_summary") or "", 8)
    body = '<ol class="summary-list">' + "".join(f"<li>{esc(line)}</li>" for line in lines) + "</ol>"
    return section("AI策略简报", "用于复盘假设，不直接触发交易", body, "ai")


def build_journal(rows: list[dict[str, str]]) -> str:
    recent = rows[-20:][::-1]
    cards = []
    for row in recent:
        code_name = f"{row.get('code')} {row.get('name')}".strip()
        is_no_trade = str(row.get("grade") or "").upper() == "NO_TRADE" or as_float(row.get("planned_shares")) <= 0
        planned = f"计划 {row.get('planned_capital') or '-'} 元 / {row.get('planned_shares') or '-'} 股。"
        levels = f"观察区 {fmt_price(row.get('entry_low'))}-{fmt_price(row.get('entry_high'))}；止盈 {fmt_price(row.get('take_profit_1'))}/{fmt_price(row.get('take_profit_2'))}；止损 {fmt_price(row.get('stop_loss'))}。"
        if is_no_trade:
            actual = "没有实际成交，因为这条卡的结论就是不做。"
            result = "PnL 不计算；后续看它次日表现，用来判断过滤条件是不是太严或太松。"
        else:
            actual = f"实际买入 {row.get('actual_entry_price') or '-'}；实际卖出 {row.get('actual_exit_price') or '-'}。"
            result = f"结果 {row.get('outcome') or '-'}；PnL {row.get('pnl') or '-'}。"
        review = row.get("review") or "还没有成交和复盘字段；这条先作为 no-trade 样本，后续继续轮动回测它有没有错过机会。"
        cards.append(
            prose_card(
                code_name,
                [
                    f"{row.get('date')}，结论是 {row.get('decision') or '-'}，等级 {row.get('grade') or '-'}。",
                    planned,
                    levels,
                    f"不做原因：{row.get('reason') or '-'}",
                    actual,
                    result,
                    f"复盘：{review}",
                ],
            )
        )
    return section("模拟交易日志", "没买的票也保留样本，继续轮动回测和复盘", source_grid(cards), "journal")


def build_daily_learning_cards(report: dict, broker: dict, journal: list[dict[str, str]], backtest: dict) -> str:
    latest_date = ""
    for row in journal:
        latest_date = max(latest_date, str(row.get("date") or ""))
    today_rows = [row for row in journal if str(row.get("date") or "") == latest_date]
    buys = [row for row in today_rows if str(row.get("decision") or "") in {"买入", "做"} and as_float(row.get("actual_shares")) > 0]
    sells = [row for row in today_rows if str(row.get("decision") or "") == "卖出" and as_float(row.get("actual_exit_price")) > 0]
    open_reviews = [row for row in today_rows if as_float(row.get("actual_shares")) > 0 and not row.get("actual_exit_price")]
    snap = latest_snapshot(broker)
    positions = snap.get("positions_visible") or []
    portfolio = report.get("portfolio") or {}
    broker_daily = as_float(snap.get("daily_profit"))
    report_daily = as_float(portfolio.get("daily_profit"))
    position_daily = sum(as_float(pos.get("daily_profit")) for pos in positions)
    pnl_gap = max(abs(report_daily - broker_daily), abs(position_daily - broker_daily))

    backtest_by_code = {str(row.get("code")): row for row in (backtest.get("summaries") or [])}
    weak_open = []
    for row in open_reviews:
        bt = backtest_by_code.get(str(row.get("code"))) or {}
        regime = str(bt.get("regime") or "")
        if regime in {"弱势下行", "震荡偏弱"}:
            weak_open.append(f"{row.get('code')} {row.get('name')}({regime})")

    lessons = []
    if pnl_gap > 500:
        lessons.append(
            source_card(
                "盈亏口径错误",
                f"券商 {fmt_money(broker_daily, 2)} / 报告 {fmt_money(report_daily, 2)} / 逐仓 {fmt_money(position_daily, 2)}",
                "今天不能再用单一数字判断表现；先补券商资产、流水、持仓三表对账。",
                "明日优化：顶部继续保留多口径对账，收盘后强制写入最终券商截图时间。",
                "danger",
            )
        )
    if buys:
        buy_text = "；".join(f"{row.get('code')} {row.get('name')} {row.get('actual_shares')}股" for row in buys)
        lessons.append(
            source_card(
                "T+1与试单复盘",
                f"{latest_date} 买入 {len(buys)} 笔",
                f"{buy_text}。买入后当天不可卖，必须提前写好明日退出线。",
                "明日优化：买入卡必须同时生成第二天卖出/止损计划，不允许只写入场。",
                "warn",
            )
        )
    if sells:
        sell_text = "；".join(f"{row.get('code')} {row.get('name')} PnL {row.get('pnl') or '-'}" for row in sells)
        lessons.append(
            source_card(
                "已实现收益复核",
                f"{latest_date} 卖出 {len(sells)} 笔",
                sell_text,
                "明日优化：卖出后剩余仓位要单独生成保护利润线，不能回头追买。",
                "ok",
            )
        )
    if weak_open:
        lessons.append(
            source_card(
                "回测反馈不足",
                "持仓里存在弱势/震荡偏弱标的",
                "；".join(weak_open),
                "明日优化：弱势标的不补仓，只看减仓/止损线；强势但次日胜率低的上海电力也只处理已有仓位。",
                "warn",
            )
        )
    lessons.append(
        source_card(
            "QuantConnect式研究流水线",
            "Idea -> Research -> Backtest -> Paper -> Review",
            "借鉴 QuantConnect Strategies / Research Pipeline：每个交易想法都要有假设、样本、回测、纸面记录、盘后复盘。",
            "落地到本工作台：每天自动生成错误卡、明日实验卡、数据缺口卡，而不是只贴资料链接。",
        )
    )
    if not lessons:
        lessons.append(source_card("学习结论", latest_date or "-", "暂无交易日志可归因；先补成交和复盘字段。"))
    return source_grid(lessons)


def build_learning_preview(report: dict, broker: dict, journal: list[dict[str, str]], backtest: dict) -> str:
    text = ""
    if LEARNING.exists():
        try:
            text = LEARNING.read_text(encoding="utf-8")
        except Exception:
            text = ""
    learning_cards = []
    if text:
        current_title = "学习报告"
        current_lines: list[str] = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("#"):
                if current_lines:
                    learning_cards.append(prose_card(current_title, current_lines[:8], file_time(LEARNING)))
                current_title = line.lstrip("#").strip() or "学习报告"
                current_lines = []
                continue
            if line.startswith("|"):
                continue
            current_lines.append(line)
        if current_lines:
            learning_cards.append(prose_card(current_title, current_lines[:8], file_time(LEARNING)))
    else:
        learning_cards.append(source_card("学习报告", file_time(LEARNING), "学习摄取未生成，运行 python tools\\learning_intake.py。"))
    body = (
        '<div class="decision-note"><strong>每天都要更新的卡片：</strong>根据当天成交、持仓、盈亏口径、回测结果自动查漏补缺。资料库只做输入，真正重要的是今天哪里没做好、明天怎么修。</div>'
        + build_daily_learning_cards(report, broker, journal, backtest)
        + "<h3>简洁学习报告</h3>"
        + source_grid(learning_cards[:8])
        + "<h3>研究资料库</h3>"
        + source_grid([
            source_card("QuantConnect Strategies", "策略库/研究流水线参考", "可借鉴它的 Strategy Explorer、Research Pipeline、回测和样本外表现组织方式。", "本地落地：不是照搬美股策略，而是给每张A股动作卡增加假设、回测、纸面交易、复盘字段。"),
            source_card("vectorbt / backtesting.py", "回测工具", "把动作卡规则变成胜率、回撤、触发条件，而不是凭感觉追单。"),
            source_card("OpenBB / Lean / 金融Agent参考", "架构参考", "学习数据接入、研究命令、人工签字和复盘流水线。"),
        ])
    )
    return section("学习摄取与研究库", "每天按盘面表现查漏补缺，而不是静态贴资料", body, "learning")


def build_daily_maintenance_records() -> str:
    files = sorted(DAILY_REVIEWS.glob("*_trade_review.md"), key=lambda p: p.name, reverse=True) if DAILY_REVIEWS.exists() else []
    cards = []
    for path in files[:8]:
        try:
            text = path.read_text(encoding="utf-8-sig")
        except Exception:
            continue
        title = path.stem.replace("_trade_review", " 维护复盘")
        lines = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("|") or line.startswith("---"):
                continue
            if line.startswith("#"):
                title = line.lstrip("#").strip() or title
                continue
            if line.startswith("## "):
                lines.append(line.lstrip("#").strip())
                continue
            lines.append(line.lstrip("- ").strip())
            if len(lines) >= 10:
                break
        cards.append(prose_card(title, lines or ["这天有维护记录，但正文暂未解析。"], file_time(path)))
    if not cards:
        cards.append(source_card("暂无每日维护记录", str(DAILY_REVIEWS), "收盘后写入 reviews/daily/YYYY-MM-DD_trade_review.md。", "这里会展示系统改动、交易复盘、明日规则和数据缺口。", "warn"))
    body = (
        '<div class="decision-note"><strong>维护记录用途：</strong>每天不只看买卖结果，还要记录系统哪里改了、哪条纪律有效、哪条规则明天要更硬。</div>'
        + source_grid(cards, "wide")
    )
    return section("每日维护记录", "最近盘后复盘、系统更新和明日修正规则", body, "maintenance")


def snapshot_age_label(snapshot: dict) -> tuple[str, str]:
    parsed = timestamp(snapshot.get("snapshot_time"))
    if parsed is None:
        return "未导入或时间异常", "danger"
    now = datetime.now(BEIJING)
    age = (now - parsed).total_seconds()
    if age < 0:
        return "未来时间，需核对", "danger"
    if age <= 1800:
        return "新鲜", "ok"
    if parsed.date() == now.date():
        return "今日较早", "warn"
    return "需人工确认", "warn"


def decision_checks(report: dict, broker: dict, route: dict) -> dict:
    ready = sum(evidence_assessment(item)["ready"] for item in read_evidence_cards())
    return local_decision_state(report, latest_snapshot(broker), route, ready)


def build_decision_checks(report: dict, broker: dict, route: dict) -> str:
    state = decision_checks(report, broker, route)
    cards = [card(label, "通过" if state[key]["ok"] else "待核验",
                  "；".join(state[key]["reasons"]) or "仅代表此项条件满足", "ok" if state[key]["ok"] else "warn")
             for key, label in (("data", "行情数据"), ("market", "市场许可"), ("setup", "候选条件"), ("account", "账户许可"))]
    return section("决策条件", f"规则 {state['rule_version']} · {state['account']['capital_basis']} · 持仓风险复核持续可用",
                   metric_grid(cards), "decision-checks")


def build_historical_review() -> str:
    data = read_json(ROOT / "data" / "historical_review.local.json")
    if not data or data.get("_error"):
        return section("真实交易复盘", "历史核账与当日成交分别记录", '<div class="empty">尚未导入历史核账证据；缺失不等于零交易。</div>', "real-review")
    stats = (data.get("cycle_stats") or {}).get("all") or {}
    cards = [card("历史买卖记录", str(data.get("trade_records", "-")), "记录条数不等于独立交易次数"),
             card("完整持仓周期", str(stats.get("count", "-")), f"盈利 {stats.get('wins', '-')} 个；周期胜率 {stats.get('winRate', '-')}%"),
             card("覆盖期重建损益", fmt_money(data.get("reconstructed_pnl"), 2), "包含期末未平仓参考收益、分红与利息；含待确认费用", "warn")]
    notes = "；".join(data.get("limitations") or [])
    return section("真实交易复盘", f"历史范围 {data.get('coverage_start')}—{data.get('coverage_end')}；不代表当前策略业绩",
                   metric_grid(cards) + '<div class="decision-note">' + esc(notes) + '。原始成交台账仍需接入，后续盈亏未知。</div>', "real-review")

def build_decision_home(report: dict, broker: dict, route: dict) -> str:
    """The primary screen: account safety and existing positions before research."""
    snap = latest_snapshot(broker)
    assets = as_float(snap.get("total_assets") or snap.get("broker_total_capital"))
    cash = as_float(snap.get("available_cash") or snap.get("cash"))
    market_value = as_float(snap.get("securities_market_value"))
    cash_ratio = cash / assets if assets else 0.0
    freshness, freshness_tone = snapshot_age_label(snap)
    route_status = route.get("status") or {}
    route_available = bool(route.get("available")) or as_float(route_status.get("valid_quote_count")) > 0
    route_note = (
        f"报价 {route_status.get('valid_quote_count', 0)}/{len(route.get('requested_codes') or [])}；"
        f"日线 {route_status.get('valid_history_count', 0)}/{len(route.get('requested_codes') or [])}。"
        if route_available else "本地行情快照尚未生成；不新增风险。"
    )
    gate = ((report.get("action_stack") or {}).get("new_entry_gate") or {})
    state = decision_checks(report, broker, route)
    gate_closed = not state["review_allowed"]
    route_available = state["data"]["ok"]
    action = "持仓优先，不新增风险" if gate_closed else "仅人工复核后观察"
    if freshness != "新鲜":
        discipline_reason = f"券商快照为{freshness}；更新账户总览和可卖份额前，新开仓额度为 0。"
    elif not route_available:
        discipline_reason = "本地行情快照未生成或不完整；新开仓额度为 0。"
    elif is_stale(report):
        discipline_reason = "市场报告已过期；刷新后才可重新评估候选。"
    else:
        discipline_reason = str(gate.get("reason") or "账户、持仓、行情三者一致后再做决定。")
    discipline_reason = "；".join(reason for key in ("data", "market", "setup", "account") for reason in state[key]["reasons"]) or "全部条件满足后仍需人工确认。"
    cards = [
        card("账户快照", fmt_money(snap.get("total_assets"), 2) if snap.get("total_assets") is not None else "待核对", f"{freshness}；{snap.get('snapshot_time') or '-'}。", freshness_tone),
        card("现金 / 权益", f"{pct_from_ratio(cash_ratio)} / {pct_from_ratio(1 - cash_ratio)}" if assets else "待核对", f"现金 {fmt_money(cash, 0)}；市值 {fmt_money(market_value, 0)}。", "ok"),
        card("持仓参考盈亏（非累计收益）", fmt_money(snap.get("reference_profit"), 2), f"当日 {fmt_money(snap.get('daily_profit'), 2)}。", "ok" if as_float(snap.get("reference_profit")) >= 0 else "warn"),
        card("今日纪律", action, discipline_reason, "danger" if gate_closed else "warn"),
        card("行情快照", "可复核" if route_available else "待刷新", route_note, "ok" if route_available else "warn"),
    ]
    return f"""
    <section class="hero">
      <div>
        <h1>Niki 投资决策工作台</h1>
        <p>先管理真实账户，再看市场；观察不是买入指令，消息面只进入盘后复盘。</p>
      </div>
      <div class="hero-actions">
        <button id="refresh-data" type="button">刷新行情快照</button>
        <a href="/api/status" target="_blank">状态 JSON</a>
      </div>
    </section>
    {metric_grid(cards, "top-grid")}
    {latest_execution_note(broker)}
    <div class="decision-note"><strong>当前工作顺序：</strong>1. 核对券商快照和可卖份额；2. 只处理已有持仓的风险或利润；3. 收盘后再用消息、公告、龙虎榜和资金流更新观察池。月度收益目标不能反过来制造交易。</div>
    """


def build_holding_focus(broker: dict) -> str:
    snap = latest_snapshot(broker)
    freshness, _ = snapshot_age_label(snap)
    positions = [pos for pos in (snap.get("positions_visible") or []) if as_float(pos.get("shares")) > 0]
    positions.sort(key=lambda pos: (classify_holding_action(pos)[3], -abs(as_float(pos.get("reference_profit")))))
    cards = []
    for pos in positions:
        action, tone, headline, _ = classify_holding_action(pos)
        cards.append(status_source_card(
            f"{pos.get('code') or '-'} {pos.get('name') or ''}",
            action,
            f"持仓 {fmt_number(pos.get('shares'), 0)} | 可卖 {fmt_number(pos.get('available'), 0)} | 现价 {fmt_price(pos.get('price'))}",
            headline,
            f"当日 {fmt_money(pos.get('daily_profit'), 2)}；参考 {fmt_money(pos.get('reference_profit'), 2)}。{str(pos.get('tomorrow_rule') or '未设置处理线')}",
            tone,
        ))
    notice = ""
    if freshness != "新鲜":
        notice = (
            '<div class="decision-note"><strong>快照已降级：</strong>'
            f'券商快照时间为 {esc(snap.get("snapshot_time") or "-")}（{esc(freshness)}）。'
            '这里的持仓只用于与券商 App 核对，不能据此买卖；成交后请先导入新的账户总览。</div>'
        )
    body = notice + (source_grid(cards, "wide") if cards else '<div class="empty">尚未导入有效持仓快照；不要根据候选池新开仓。</div>')
    return section("快照持仓处理", "按可卖份额排序；默认不补仓、不做盘中临时切换", body, "holdings")


def build_market_observation(report: dict, route: dict) -> str:
    policy = report.get("market_structure_policy") or {}
    indices = route.get("market_indices") or []
    index_cards = [
        source_card(
            f"{item.get('code') or ''} {item.get('name') or ''}",
            f"现价 {fmt_price(item.get('price'))}",
            f"涨跌 {pct(item.get('change_pct'))}",
            "只用于判断环境强弱，不生成买入动作。",
            "danger" if as_float(item.get("change_pct")) < -1 else "",
        )
        for item in indices[:6]
    ]
    preferred = " / ".join(policy.get("preferred_industries") or []) or "等待盘后形成观察主题"
    avoided = " / ".join(policy.get("avoid_industries") or []) or "不追涨、不因消息临时换仓"
    body = (
        '<div class="decision-note"><strong>市场规则：</strong>大盘与风格没有确认时，候选池只保留观察资格。上涨、龙虎榜、热榜或一条快讯都不足以单独成为开仓理由。</div>'
        + source_grid(index_cards)
        + source_grid([
            source_card("观察主题", "盘后更新", preferred, "需要行业共振、位置和风险线同时满足。"),
            source_card("暂不参与", "风险过滤", avoided, "先把已有仓位和交易频率控制住。", "warn"),
        ])
    )
    return section("市场与观察", "行情只回答环境问题；候选池不在盘中推送买入", body, "market")


def build_evidence_cards(report: dict, broker: dict, route: dict) -> str:
    """Render the research gate between market observation and any action card."""
    cards = read_evidence_cards()
    gate = ((report.get("action_stack") or {}).get("new_entry_gate") or {})
    freshness, _ = snapshot_age_label(latest_snapshot(broker))
    route_status = route.get("status") or {}
    route_available = bool(route.get("available")) or as_float(route_status.get("valid_quote_count")) > 0
    market_open = decision_checks(report, broker, route)["review_allowed"]
    assessed = [(item, evidence_assessment(item)) for item in cards]
    complete = sum(1 for _, assessment in assessed if assessment["ready"])
    data_passed = sum(1 for _, assessment in assessed if assessment["data_passed"])
    logic_passed = sum(1 for _, assessment in assessed if assessment["logic_passed"])
    actionable = complete if market_open else 0
    metrics = [
        card("候选证据卡", str(len(cards)), "候选不是动作卡；缺卡默认只观察。", "ok" if cards else "warn"),
        card("数据验证通过", str(data_passed), "价格、均线、流动性、账户权限。", "ok" if data_passed else "warn"),
        card("逻辑验证通过", str(logic_passed), "供需、共振、位置、催化与反例。", "ok" if logic_passed else "warn"),
        card(
            "可人工复核",
            str(actionable),
            str(gate.get("reason") or "市场闸门仍需通过。") if market_open else f"账户快照 {freshness} 或行情快照未满足新开仓条件。",
            "ok" if actionable else "warn",
        ),
    ]
    if not cards:
        body = (
            metric_grid(metrics)
            + '<div class="decision-note"><strong>候选升级规则：</strong>先从 '
            '<code>examples/research_evidence.example.json</code> 复制到 '
            '<code>data/research_evidence.local.json</code>，再填写原始来源、供需逻辑、反例、触发和失效条件。'
            '没有完整证据卡的标的只能停在观察池。</div>'
        )
        return section("候选证据与人工复核", "AI 可以整理线索；人必须验证逻辑和仓位", body, "evidence")

    rendered = []
    for item, assessment in assessed[:12]:
        code = str(item.get("code") or "-")
        name = str(item.get("name") or "")
        if not assessment["ready"]:
            badge, tone = "仅观察", "warn"
            headline = "证据卡未完整，不能升级为动作卡。"
        elif not market_open:
            badge, tone = "等待市场闸门", "warn"
            headline = "证据完整，但账户/市场闸门未开。"
        else:
            badge, tone = "提交人工复核", "ok"
            headline = "双重验证已完成；仍需你确认仓位与执行窗口。"
        source_text = "；".join(
            f"{source.get('title') or source.get('url') or '未命名来源'} ({source.get('published_at') or source.get('captured_at') or '未记录时间'})"
            for source in assessment["sources"][:3]
        ) or "未提供原始来源"
        lines = [
            headline,
            f"供需逻辑：{item.get('supply_demand_thesis') or '-'}",
            f"市场未定价点：{item.get('market_mispricing') or '-'}",
            f"反例：{item.get('counter_evidence') or '-'}",
            f"触发：{item.get('trigger') or '-'}；失效：{item.get('invalidation') or '-'}",
            f"数据验证：{'通过' if assessment['data_passed'] else '待补 ' + '、'.join(item for item in assessment['missing'] if item.startswith('数据:'))}",
            f"逻辑验证：{'通过' if assessment['logic_passed'] else '待补 ' + '、'.join(item for item in assessment['missing'] if item.startswith('逻辑:'))}",
            f"来源：{source_text}",
        ]
        rendered.append(
            prose_card(
                f"{code} {name}".strip(),
                lines,
                f"{item.get('theme') or '未分类'} | {item.get('horizon') or '5-20个交易日'} | {item.get('status') or 'research'}",
                tone,
            )
        )
    body = (
        metric_grid(metrics)
        + '<div class="decision-note"><strong>双重验证：</strong>数据层核对价格、均线、成交、持仓与交易权限；逻辑层核对供需、共振、位置、催化和反例。'
        '任一缺失，候选只能观察。群消息、热榜和 AI 摘要只能提供线索。</div>'
        + source_grid(rendered, "wide")
    )
    return section("候选证据与人工复核", "产业供需/景气拐点 -> 主线共振 -> 回踩确认", body, "evidence")


def build_trade_attribution() -> str:
    """Make every locally confirmed fill either reviewed or visibly incomplete."""
    fills = sorted(read_csv(REAL_TRADE_JOURNAL), key=lambda row: str(row.get("trade_time") or ""), reverse=True)
    attributions = {trade_key(row): row for row in read_csv(TRADE_ATTRIBUTIONS) if trade_key(row) != "|"}
    reviewed = 0
    cards = []
    for fill in fills[:12]:
        attribution = attributions.get(trade_key(fill))
        code_name = f"{fill.get('code') or '-'} {fill.get('name') or ''}".strip()
        if not attribution:
            cards.append(
                source_card(
                    code_name,
                    f"{fill.get('trade_time') or '-'} | {fill.get('side') or '-'} {fill.get('shares') or '-'} 份",
                    "未完成归因：先补当时市场闸门、买卖理由、仓位和结果，再评价这笔交易。",
                    "在 data/trade_attributions.local.csv 新增同一 trade_time 与 code 的一行。",
                    "warn",
                )
            )
            continue
        reviewed += 1
        primary = attribution.get("primary_cause") or "未分类"
        cards.append(
            prose_card(
                code_name,
                [
                    f"成交：{fill.get('trade_time') or '-'} | {fill.get('side') or '-'} {fill.get('shares') or '-'} 份 @ {fill.get('price') or '-'}。",
                    f"市场闸门：{attribution.get('market_state') or '-'}；执行原因：{attribution.get('decision_context') or '-'}。",
                    f"仓位/执行：{attribution.get('positioning') or '-'}；{attribution.get('execution_quality') or '-'}。",
                    f"结果：{attribution.get('outcome') or '-'}；主因：{primary}；次因：{attribution.get('secondary_cause') or '-'}。",
                    f"下一规则：{attribution.get('next_rule') or '-'}",
                ],
                f"复盘时间 {attribution.get('reviewed_at') or '-'}",
                "ok" if primary not in {"未分类", ""} else "warn",
            )
        )
    pending = max(len(fills) - reviewed, 0)
    metrics = [
        card("本地确认成交", str(len(fills)), "只读取本机成交台账，不上传。", "ok" if fills else "warn"),
        card("已完成归因", str(reviewed), "市场、选品、仓位、执行和结果已分开记录。", "ok" if reviewed else "warn"),
        card("待复盘成交", str(pending), "未归因的成交不能被当作系统有效样本。", "danger" if pending else "ok"),
    ]
    note = (
        '<div class="decision-note"><strong>归因规则：</strong>亏损不只写“判断错了”。必须归到市场状态、选品/供需、买点、仓位、卖点或纪律。'
        '只有完成归因的成交，才能进入后续胜率、盈亏比和资金扩大统计。</div>'
    )
    body = metric_grid(metrics) + note + (source_grid(cards, "wide") if cards else '<div class="empty">尚未记录本地确认成交。</div>')
    return section("成交归因队列", "每笔成交都要解释：为什么做、环境如何、结果来自哪里", body, "attribution")


def build_research_sources(route: dict) -> str:
    route_names = " -> ".join(route.get("route") or []) or "腾讯实时行情 -> 通达信 K 线 -> 腾讯前复权 K 线 -> AKShare"
    cards = [
        source_card("行情主路由", "本地默认", route_names, "刷新仅拉取行情和 K 线；失败时降级，不阻塞持仓复核。", "ok"),
        source_card("a-stock-data 能力", "按需研究", "公告、资金流、龙虎榜、行业、新闻只服务于盘后复盘与观察池。", "不直接成为盘中买入按钮。", "ok"),
        source_card("星耀", "可选本地增强", "保留探针与研究脚本，不作为主报价或云端依赖。", "连接失败自动忽略；不进入首页刷新链。", "warn"),
        source_card("iFind", "默认关闭", "保留适配接口，未来需要时用于公告、基础信息的交叉校验。", "当前不自动调用，也不影响工作台。", "warn"),
        source_card("期权", "研究归档", "保留仿真代码和历史数据接口，不展示期权链或生成实盘提示。", "当前账户目标应先验证现货/ETF纪律，期权不进入日常流程。", "warn"),
    ]
    return section("数据与研究边界", "少而稳定的主路由；高噪声能力只在盘后按需使用", source_grid(cards), "research")


def build_html() -> str:
    report = read_json(REPORT)
    broker = read_broker_snapshot()
    route = read_json(A_STOCK_ROUTE)
    title = "Niki 投资决策工作台"
    html_body = f"""
    {build_decision_home(report, broker, route)}
    {build_decision_checks(report, broker, route)}
    {build_risk_budget(report, broker, route)}
    {build_holding_focus(broker)}
    {build_holdings_table(broker)}
    {build_market_observation(report, route)}
    {build_evidence_cards(report, broker, route)}
    {build_historical_review()}
    {build_trade_attribution()}
    {build_research_sources(route)}
    {build_daily_maintenance_records()}
    <footer>本页只读取本地账户快照与公开行情快照，用于纪律提醒和复盘；不连接券商、不自动下单、不承诺收益。</footer>
    """
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <style>{styles()}</style>
</head>
<body>
  <nav>
    <strong>Niki 决策工作台</strong>
    <a href="#decision-checks">决策条件</a>
    <a href="#risk-budget">风险预算</a>
    <a href="#real-review">真实复盘</a>
    <a href="#holdings">持仓</a>
    <a href="#market">市场</a>
    <a href="#evidence">证据卡</a>
    <a href="#attribution">归因</a>
    <a href="#research">数据与研究</a>
    <a href="#maintenance">维护</a>
  </nav>
  <main>{html_body}</main>
  <script>{script()}</script>
</body>
</html>"""


def styles() -> str:
    return """
    :root {
      --bg: #f4f6f8;
      --panel: #ffffff;
      --ink: #172033;
      --muted: #5d6b82;
      --line: #d8dee8;
      --blue: #2367c9;
      --green: #18794e;
      --amber: #9a5b00;
      --red: #b42318;
      --soft-blue: #eef5ff;
      --soft-green: #eaf7ef;
      --soft-amber: #fff5de;
      --soft-red: #fff0ee;
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--bg); color: var(--ink); font-family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif; font-size: 14px; line-height: 1.55; }
    nav { position: sticky; top: 0; z-index: 5; display: flex; gap: 14px; align-items: center; padding: 10px 18px; background: rgba(255,255,255,.96); border-bottom: 1px solid var(--line); overflow-x: auto; white-space: nowrap; }
    nav strong { color: var(--blue); }
    nav a { color: var(--muted); text-decoration: none; font-weight: 700; font-size: 13px; }
    main { max-width: 1440px; margin: 0 auto; padding: 16px; }
    .hero { display: grid; grid-template-columns: 1fr auto; gap: 16px; align-items: center; padding: 22px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
    h2 { margin: 0; font-size: 20px; letter-spacing: 0; }
    h3 { margin: 18px 0 10px; font-size: 16px; }
    p { margin: 6px 0 0; color: var(--muted); }
    button, .hero-actions a { border: 1px solid var(--blue); background: var(--blue); color: white; border-radius: 6px; padding: 9px 13px; font-weight: 800; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; min-height: 38px; }
    .hero-actions { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }
    .hero-actions a { background: white; color: var(--blue); }
    .panel { margin-top: 14px; padding: 16px; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .panel-header { display: flex; justify-content: space-between; gap: 12px; align-items: baseline; padding-bottom: 12px; border-bottom: 1px solid var(--line); }
    .panel-header span { color: var(--muted); font-size: 13px; font-weight: 700; }
    .metric-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
    .top-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
    .execution-grid { grid-template-columns: repeat(5, minmax(0, 1fr)); }
    .metric, .source-card { border: 1px solid var(--line); border-radius: 8px; background: white; padding: 12px; min-width: 0; }
    .metric span, .source-meta { display: block; color: var(--muted); font-size: 12px; font-weight: 800; }
    .metric strong { display: block; margin-top: 4px; font-size: 22px; color: var(--ink); overflow-wrap: anywhere; }
    .metric p, .source-card p { font-size: 13px; overflow-wrap: anywhere; }
    .metric.ok, .source-card.ok { background: var(--soft-green); border-color: #a7d9bc; }
    .metric.warn, .source-card.warn { background: var(--soft-amber); border-color: #f0cc7a; }
    .metric.danger, .source-card.danger { background: var(--soft-red); border-color: #f1a29b; }
    .source-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
    .source-grid.compact, .source-grid.interfaces { grid-template-columns: repeat(6, minmax(0, 1fr)); }
    .source-grid.wide, #maintenance .source-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    #journal .source-grid, #learning .source-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    #learning .source-grid:first-of-type { grid-template-columns: 1fr; }
    .source-card.prose { padding: 14px 16px; }
    .source-card.prose p { color: var(--ink); font-size: 14px; line-height: 1.65; }
    .source-head { display: flex; justify-content: space-between; gap: 8px; align-items: flex-start; }
    .source-title { font-weight: 900; font-size: 15px; color: var(--ink); overflow-wrap: anywhere; }
    .source-card strong { display: block; margin-top: 7px; color: var(--ink); font-size: 14px; overflow-wrap: anywhere; }
    .tag, .table-tag { display: inline-flex; align-items: center; justify-content: center; min-height: 24px; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 800; white-space: nowrap; }
    .tag.ok, .table-tag.ok { background: var(--soft-green); color: var(--green); border: 1px solid #a7d9bc; }
    .tag.warn, .table-tag.warn { background: var(--soft-amber); color: var(--amber); border: 1px solid #f0cc7a; }
    .tag.danger, .table-tag.danger { background: var(--soft-red); color: var(--red); border: 1px solid #f1a29b; }
    .table-tag { margin-bottom: 6px; }
    .decision-note { margin-top: 12px; padding: 12px 14px; background: var(--soft-blue); border: 1px solid #bdd3f4; border-radius: 8px; color: var(--ink); }
    .decision-note ol { margin: 8px 0 0 20px; padding: 0; }
    .command-box { margin-top: 10px; display: grid; grid-template-columns: 140px minmax(0, 1fr) auto; gap: 10px; align-items: center; padding: 12px; background: #111827; color: white; border-radius: 8px; }
    code { display: block; color: #dbeafe; overflow-wrap: anywhere; font-family: Consolas, "Courier New", monospace; }
    .table-wrap { overflow-x: auto; margin-top: 12px; }
    table { width: 100%; border-collapse: collapse; min-width: 980px; }
    th, td { text-align: left; vertical-align: top; padding: 10px; border-bottom: 1px solid var(--line); }
    th { background: #f7f9fc; color: var(--muted); font-size: 12px; }
    td { color: var(--ink); }
    .watch-row { background: #fffdf2; }
    .summary-list { margin: 12px 0 0 22px; padding: 0; }
    .summary-list li { margin: 8px 0; }
    .empty { margin-top: 12px; padding: 18px; color: var(--muted); border: 1px dashed var(--line); border-radius: 8px; }
    footer { margin: 18px 0 28px; color: var(--muted); font-size: 12px; }
    @media (max-width: 1100px) {
      .metric-grid, .top-grid, .source-grid, .source-grid.compact, .source-grid.interfaces, .source-grid.wide, #journal .source-grid, #learning .source-grid, #maintenance .source-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .hero { grid-template-columns: 1fr; }
      .command-box { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      main { padding: 10px; }
      .metric-grid, .top-grid, .source-grid, .source-grid.compact, .source-grid.interfaces, .source-grid.wide, #journal .source-grid, #learning .source-grid, #maintenance .source-grid { grid-template-columns: 1fr; }
      .panel-header { flex-direction: column; }
      h1 { font-size: 24px; }
    }
    """


def script() -> str:
    return """
    const button = document.getElementById("refresh-data");
    const note = document.getElementById("sync-note");
    async function pollStatus() {
      try {
        const res = await fetch("/api/status", {cache: "no-store"});
        const data = await res.json();
        note.textContent = `实时同步：latest.json ${data.report_mtime}；券商快照 ${data.broker_snapshot_time}；最近成交 ${data.latest_real_trade_time || "-"}；刷新任务 ${data.refresh_running ? "运行中" : "空闲"}`;
      } catch (err) {
        note.textContent = "实时同步：状态读取失败，请看命令行日志。";
      }
    }
    button?.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "刷新中...";
      try {
        await fetch("/refresh", {method: "POST"});
        await pollStatus();
      } finally {
        setTimeout(() => {
          button.disabled = false;
          button.textContent = "刷新行情快照";
        }, 5000);
      }
    });
    pollStatus();
    setInterval(pollStatus, 30000);
    """


def refresh_status() -> dict:
    global REFRESH_PROCESS
    running = REFRESH_PROCESS is not None and REFRESH_PROCESS.poll() is None
    snap = latest_snapshot(read_broker_snapshot())
    return {
        "refresh_running": running,
        "refresh_started_at": REFRESH_STARTED_AT,
        "report_mtime": file_time(REPORT),
        "broker_snapshot_time": snap.get("snapshot_time") or "-",
        "latest_real_trade_time": (latest_real_trade().get("trade_time") or "-"),
        "evidence_cards_mtime": file_time(EVIDENCE_CARDS),
        "trade_attributions_mtime": file_time(TRADE_ATTRIBUTIONS),
        "a_stock_route_mtime": file_time(A_STOCK_ROUTE),
    }


def a_stock_python() -> str:
    configured = os.environ.get("A_STOCK_PYTHON")
    if configured and Path(configured).is_file():
        return configured
    local_python = ROOT / ".venv-a-stock" / "Scripts" / "python.exe"
    if local_python.is_file():
        return str(local_python)
    return sys.executable


def start_refresh() -> dict:
    global REFRESH_PROCESS, REFRESH_STARTED_AT
    with REFRESH_LOCK:
        if REFRESH_PROCESS is not None and REFRESH_PROCESS.poll() is None:
            return refresh_status()
        refresh_code = (
            "import os, subprocess, sys; "
            "os.environ['PYTHONIOENCODING']='utf-8'; "
            "steps=["
            f"('a-share quote and K-line snapshot',[{a_stock_python()!r},'tools/a_stock_radar_snapshot.py'],120)"
            "]; "
            "\nfor name, cmd, timeout in steps:\n"
            "    print(f'=== {name} start ===', flush=True)\n"
            "    try:\n"
            "        rc=subprocess.run(cmd, timeout=timeout).returncode\n"
            "        print(f'=== {name} done rc={rc} ===', flush=True)\n"
            "    except subprocess.TimeoutExpired:\n"
            "        print(f'=== {name} timeout after {timeout}s ===', flush=True)\n"
        )
        command = [sys.executable, "-c", refresh_code]
        REFRESH_STARTED_AT = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        out = ROOT / "data" / "local_dashboard_refresh.out.log"
        err = ROOT / "data" / "local_dashboard_refresh.err.log"
        REFRESH_PROCESS = subprocess.Popen(
            command,
            cwd=ROOT,
            stdout=out.open("ab"),
            stderr=err.open("ab"),
            shell=False,
        )
    return refresh_status()


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, content: str, content_type: str = "text/html; charset=utf-8") -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._send(200, json.dumps(refresh_status(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        if path in {"/", "/index.html"}:
            self._send(200, build_html())
            return
        self._send(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/refresh":
            self._send(200, json.dumps(start_refresh(), ensure_ascii=False), "application/json; charset=utf-8")
            return
        self._send(404, "Not found", "text/plain; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - - [%s] %s\n" % (self.address_string(), self.log_date_time_string(), fmt % args))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8501)
    parser.add_argument("--write-preview", default="", help="Write the rendered HTML to a file and exit")
    args = parser.parse_args()
    if args.write_preview:
        preview_path = Path(args.write_preview)
        preview_path.parent.mkdir(parents=True, exist_ok=True)
        preview_path.write_text(build_html(), encoding="utf-8")
        print(f"local_dashboard_preview={preview_path}")
        return 0
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Local dashboard listening on http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
