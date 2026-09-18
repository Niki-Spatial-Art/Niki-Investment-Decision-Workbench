"""Public-market event deduplication; contains no account or recipient data."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path


def event_state(summary: dict, day: str) -> dict:
    payload = {"day": day, "gate_open": bool(summary.get("gate_open")),
               "rule_version": (summary.get("decision") or {}).get("rule_version"),
               "candidates": sorted(str(x.get("code")) for x in summary.get("actionable_candidates") or []) if summary.get("gate_open") else []}
    payload["fingerprint"] = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return payload


def notification_decision(summary: dict, previous: dict, day: str) -> tuple[bool, str, dict]:
    current = event_state(summary, day)
    same_day = previous.get("day") == day
    if same_day and previous.get("gate_open") is True and not current["gate_open"]:
        return True, "market_permission_withdrawn", current
    if current["gate_open"] and current["candidates"]:
        if same_day and previous.get("fingerprint") == current["fingerprint"] and previous.get("smtp_accepted"):
            return False, "duplicate_actionable_event", current
        return True, "new_actionable_event", current
    return False, "gate_closed_no_action" if not current["gate_open"] else "gate_open_but_no_candidate", current


def read_state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
