"""Import an already reconciled historical analysis into local-only review data.

This is evidence import, not a fabricated fill ledger or a live performance feed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare_summary(data: dict, source_hash: str) -> dict:
    required = ("cashRowsReconciled", "shareRowsReconciled", "endingSharesReconciled", "cashFlowEqualsCycles", "attributionReconciled", "sept4OrdersFullyFilled")
    checks = data.get("checks") or {}
    if any(checks.get(key) is not True for key in required):
        raise ValueError("Historical reconciliation checks are incomplete; do not import as reconciled evidence")
    dates = [d for row in data.get("fileSummary", []) for d in row.get("dates", [])]
    dates += [row["date"] for row in data.get("orders", [])]
    if not dates:
        raise ValueError("Missing coverage dates")
    for date in dates:
        datetime.strptime(date, "%Y%m%d")
    rec = data["reconciliation"]
    return {"schema_version": 1, "source_type": "previously_reconciled_analysis", "source_sha256": source_hash,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "coverage_start": min(dates), "coverage_end": max(dates),
            "trade_records": data["tradeCount"] + len(data.get("orders", [])),
            "cycle_stats": data["cycleStats"], "checks": checks,
            "reconstructed_pnl": round(rec["pnlBeforeSept4Fees"] - rec["inferredSept4Fees"], 2),
            "inferred_fees": rec["inferredSept4Fees"],
            "limitations": ["复用已核账历史分析，本次未重新计算交割单", "覆盖系统建立前交易，不代表当前策略实盘绩效",
                            "期末费用含反推金额，待正式交割单确认", "覆盖期后的成交与出入金尚未接入，不是今日或全年实时收益"],
            "raw_fill_ledger_imported": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    args = parser.parse_args()
    raw = Path(args.source).read_bytes()
    summary = prepare_summary(json.loads(raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest())
    dest = ROOT / "data" / "historical_review.local.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        previous = json.loads(dest.read_text(encoding="utf-8"))
        if previous.get("source_sha256") == summary["source_sha256"]:
            print("historical_review=unchanged")
            return 0
        archive = dest.parent / "review_evidence_archive"
        archive.mkdir(exist_ok=True)
        digest = hashlib.sha256(dest.read_bytes()).hexdigest()[:16]
        (archive / (digest + ".json")).write_bytes(dest.read_bytes())
    temp = dest.with_suffix(".tmp")
    temp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(dest)
    print("historical_review=imported; raw fill ledger remains separate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
