"""Local-only status report; no network, notification or account mutation."""
from __future__ import annotations
import json
from pathlib import Path
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.decision_state import BEIJING, local_decision_state


def read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def main() -> int:
    payload = read(ROOT / "data/broker_account_snapshots.local.json")
    rows = payload.get("snapshots") or []
    latest = max(rows, key=lambda row: row.get("snapshot_time", "")) if rows else {}
    report = read(ROOT / "reports/latest.json")
    route = read(ROOT / "data/a_stock_radar_snapshot.json")
    # Use exactly the UI evidence validator; no separate definition of readiness.
    from tools.local_dashboard import evidence_assessment, read_evidence_cards
    ready = sum(evidence_assessment(item)["ready"] for item in read_evidence_cards())
    state = local_decision_state(report, latest, route, ready)
    history = read(ROOT / "data/historical_review.local.json")
    result = {"generated_at": datetime.now(BEIJING).isoformat(), "decision": state,
              "historical_review": {key: history.get(key) for key in ("coverage_start", "coverage_end", "trade_records", "raw_fill_ledger_imported")},
              "fill_ledger_present": (ROOT / "data/trade_journal.local.csv").exists(),
              "auto_sync_paused": (ROOT / ".git/codex-maintenance.lock").exists()}
    output = ROOT / "data/workbench_health.local.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"health_report": str(output), "review_allowed": state["review_allowed"],
                      "history_available": bool(history), "auto_sync_paused": result["auto_sync_paused"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
