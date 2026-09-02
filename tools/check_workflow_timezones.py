#!/usr/bin/env python3
"""Guard GitHub Actions schedules against accidental UTC rewrites.

All recurring report workflows in this project are written in Beijing time and
must pin each schedule entry with ``timezone: Asia/Shanghai``.  The scripts
still perform their own Beijing-time market/trading-day guards, but this check
keeps workflow files readable and prevents agents from converting them back to
UTC by mistake.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
EXPECTED_TZ = "Asia/Shanghai"
FORBIDDEN_TEXT = (
    "GitHub Actions schedules must be written in UTC only",
    "GitHub Actions cron uses UTC; do not add unsupported timezone fields",
    "do not add unsupported timezone fields",
)


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def check_workflow(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    errors: list[str] = []

    for forbidden in FORBIDDEN_TEXT:
        if forbidden in text:
            errors.append(f"contains stale UTC-only instruction: {forbidden!r}")

    for idx, line in enumerate(lines):
        if not re.match(r"^\s*-\s*cron:\s*['\"][^'\"]+['\"]\s*$", line):
            continue

        indent = _line_indent(line)
        timezone_ok = False
        for next_line in lines[idx + 1 :]:
            stripped = next_line.strip()
            if not stripped:
                continue
            next_indent = _line_indent(next_line)
            if next_indent <= indent or re.match(r"^\s*-\s*cron:", next_line):
                break
            if stripped == f"timezone: {EXPECTED_TZ}":
                timezone_ok = True
                break
        if not timezone_ok:
            errors.append(f"line {idx + 1}: cron entry missing timezone: {EXPECTED_TZ}")

    return errors


def main() -> int:
    findings: list[tuple[Path, str]] = []
    for workflow in sorted(WORKFLOW_DIR.glob("*.yml")):
        for error in check_workflow(workflow):
            findings.append((workflow.relative_to(ROOT), error))

    if not findings:
        print("workflow_timezones=ok")
        return 0

    print("workflow_timezones=failed")
    for rel, error in findings:
        print(f"{rel}: {error}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
