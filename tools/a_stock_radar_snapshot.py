#!/usr/bin/env python3
"""Write a local A-share/ETF source snapshot used by the radar safety gate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    from .a_stock_market_data import snapshot
except ImportError:
    from a_stock_market_data import snapshot


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RADAR_CODES = [
    "513310", "159696", "513500", "510300", "510500", "512100", "512880",
    "588000", "512760", "513180", "518880", "510050",
]


def load_portfolio_codes(path: Path) -> list[str]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    codes = [str(item.get("code") or "") for item in (payload.get("positions") or [])]
    codes.extend(str(code) for code in (payload.get("watchlist") or []))
    codes.extend(DEFAULT_RADAR_CODES)
    return [code for code in codes if code]


def load_workbench_codes(root: Path, portfolio: Path) -> list[str]:
    """Holdings + three benchmark proxies + at most three research candidates.

    A missing optional portfolio must not prevent the default refresh.
    """
    codes = []
    broker_path = root / "data" / "broker_account_snapshots.local.json"
    if broker_path.exists():
        payload = json.loads(broker_path.read_text(encoding="utf-8-sig"))
        rows = payload.get("snapshots") or []
        latest = max(rows, key=lambda row: row.get("snapshot_time", "")) if rows else {}
        codes.extend(str(p.get("code")) for p in latest.get("positions_visible") or [] if float(p.get("shares") or 0) > 0)
    codes.extend(["510300", "512100", "588000"])
    research_path = root / "data" / "research_evidence.local.json"
    if research_path.exists():
        payload = json.loads(research_path.read_text(encoding="utf-8-sig"))
        cards = payload if isinstance(payload, list) else payload.get("cards") or []
        codes.extend(str(card.get("code")) for card in cards[:3])
    if not broker_path.exists() and portfolio.exists():
        codes.extend(load_portfolio_codes(portfolio))
    return list(dict.fromkeys(code for code in codes if len(code) == 6 and code.isdigit()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Create an auditable A-share radar source snapshot")
    parser.add_argument("--codes", help="comma-separated six-digit A-share/ETF codes")
    parser.add_argument("--portfolio", default="portfolio.local.json")
    parser.add_argument("--bars", type=int, default=65)
    parser.add_argument("--output", default="data/a_stock_radar_snapshot.json")
    args = parser.parse_args()

    codes = [item.strip() for item in (args.codes or "").split(",") if item.strip()]
    if not codes:
        codes = load_workbench_codes(ROOT, ROOT / args.portfolio)
    if not codes:
        print("No portfolio positions or watchlist codes found.", file=sys.stderr)
        return 2

    payload = snapshot(codes, bars=max(1, args.bars))
    payload["portfolio_file"] = args.portfolio
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = output.with_suffix(output.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(output)
    status = payload.get("status") or {}
    print(
        f"A-share radar snapshot: quotes {status.get('valid_quote_count')}/{len(payload.get('requested_codes') or [])}; "
        f"daily bars {status.get('valid_history_count')}/{len(payload.get('requested_codes') or [])}; {output}"
    )
    return 0 if status.get("valid_quote_count") else 1


if __name__ == "__main__":
    raise SystemExit(main())
