"""Versioned, pure decision checks shared by public reports and the local UI."""
from __future__ import annotations

from datetime import datetime
import math
from zoneinfo import ZoneInfo

RULE_VERSION = "2026-09-18.1"
BEIJING = ZoneInfo("Asia/Shanghai")


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def timestamp(value):
    try:
        text = str(value or "")
        parsed = datetime.strptime(text, "%Y%m%d%H%M%S") if len(text) == 14 and text.isdigit() else datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed.astimezone(BEIJING)
    except ValueError:
        return None


def fresh(value, seconds: int, now: datetime) -> bool:
    parsed = timestamp(value)
    return parsed is not None and 0 <= (now - parsed).total_seconds() <= seconds


def scan_quality(scan: dict) -> dict:
    scanned = number(scan.get("scanned_count"))
    target = number(scan.get("min_rows_target")) or 5000
    breadth = scan.get("breadth") or {}
    counts = [number(breadth.get(k)) for k in ("advancers", "decliners", "flat")]
    valid_counts = all(value is not None and value >= 0 and value.is_integer() for value in counts)
    classified = int(sum(counts)) if valid_counts else None
    reasons = []
    if scanned is None or scanned < target:
        reasons.append("全市场覆盖不足")
    if not valid_counts or not classified or scanned is None or classified > scanned:
        reasons.append("涨跌家数缺失或分母不一致")
    gap = int(scanned - classified) if scanned is not None and classified is not None else None
    return {"ok": not reasons, "reasons": reasons, "scanned": scanned, "target": target,
            "classified": classified, "unclassified": gap,
            "coverage_ratio": scanned / target if scanned is not None and target else None,
            "note": "未分类行需核对停牌、无效字段或股票范围，不自动视为平盘。" if gap else ""}


def public_market_gate(scan: dict, trend_ok: bool, current_day: bool, profile: str) -> dict:
    quality = scan_quality(scan)
    breadth = scan.get("breadth") or {}
    up, down = number(breadth.get("advancers")), number(breadth.get("decliners"))
    breadth_ok = up is not None and down is not None and up > down
    reasons = list(quality["reasons"])
    if not current_day:
        reasons.append("行情日期未确认")
    if not breadth_ok:
        reasons.append("上涨家数未占优")
    if not trend_ok:
        reasons.append("策略趋势条件未满足")
    return {"rule_version": RULE_VERSION, "profile": profile, "data": quality,
            "market": {"ok": bool(trend_ok and breadth_ok and current_day), "trend_ok": bool(trend_ok), "breadth_ok": breadth_ok},
            "open": not reasons, "reasons": reasons,
            "status": "观察：市场层通过初筛，仍需板块和个股证据" if not reasons else "等待：" + "；".join(reasons)}


def account_check(snap: dict, now: datetime) -> dict:
    reasons = []
    if not fresh(snap.get("snapshot_time"), 1800, now):
        reasons.append("账户快照超过30分钟、缺失或时间异常")
    if snap.get("fills_verified") is not True:
        reasons.append("成交与账户余额尚未核验")
    values = [number(snap.get(k)) for k in ("total_assets", "available_cash", "securities_market_value")]
    if any(v is None or v < 0 for v in values) or values[0] == 0:
        reasons.append("账户总资产、现金或市值缺失/无效")
    elif abs(values[0] - values[1] - values[2]) > 0.05:
        reasons.append("现金加证券市值与总资产不符")
    positions = snap.get("positions_visible")
    if not isinstance(positions, list):
        reasons.append("持仓清单缺失")
    else:
        for pos in positions:
            shares, available = number(pos.get("shares")), number(pos.get("available"))
            if shares is None or available is None or not 0 <= available <= shares:
                reasons.append("持仓或可卖数量未确认")
                break
    cash_time = snap.get("cash_snapshot_time")
    if cash_time and cash_time != snap.get("snapshot_time"):
        reasons.append("现金与持仓来自不同时点")
    return {"ok": not reasons, "reasons": reasons, "snapshot_time": snap.get("snapshot_time"),
            "capital_basis": "券商账户总资产；场外资金未计入"}


def route_check(route: dict, required_codes: list[str], now: datetime) -> dict:
    codes = list(dict.fromkeys(required_codes + list(route.get("requested_codes") or [])))
    reasons = []
    if not fresh(route.get("generated_at"), 300, now):
        reasons.append("行情快照超过5分钟、缺失或时间异常")
    quotes = route.get("quotes") or {}
    missing = [code for code in codes if not (number((quotes.get(code) or {}).get("price")) or 0) > 0]
    stale = [code for code in codes if not fresh((quotes.get(code) or {}).get("quote_time"), 300, now)]
    if not codes:
        reasons.append("行情范围缺失")
    if missing:
        reasons.append("缺少有效报价：" + ",".join(missing))
    if stale:
        reasons.append("报价非当前时点：" + ",".join(stale))
    return {"ok": not reasons, "reasons": reasons, "generated_at": route.get("generated_at"), "required_codes": codes}


def local_decision_state(report: dict, snap: dict, route: dict, ready_candidates: int = 0, now=None) -> dict:
    now = now or datetime.now(BEIJING)
    if now.tzinfo is None:
        now = now.replace(tzinfo=BEIJING)
    required = [str(p.get("code")) for p in snap.get("positions_visible") or [] if (number(p.get("shares")) or 0) > 0]
    data = route_check(route, required, now)
    gate = (report.get("action_stack") or {}).get("new_entry_gate") or {}
    generated = report.get("generated_at") or (report.get("metadata") or {}).get("generated_at")
    market_ok = gate.get("blocked") is False and fresh(generated, 5400, now)
    market = {"ok": market_ok, "reasons": [] if market_ok else [gate.get("reason") or "缺少当日明确通过的市场闸门"]}
    account = account_check(snap, now)
    setup = {"ok": ready_candidates > 0, "count": ready_candidates, "reasons": [] if ready_candidates else ["尚无完整候选证据卡"]}
    return {"rule_version": RULE_VERSION, "profile": "local_manual_review", "data": data,
            "market": market, "setup": setup, "account": account,
            "review_allowed": all(item["ok"] for item in (data, market, setup, account)),
            "holdings_review_allowed": True}
