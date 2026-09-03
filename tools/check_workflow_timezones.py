#!/usr/bin/env python3
"""Guard GitHub Actions schedules against accidental local-time rewrites.

GitHub Actions scheduled workflows are kept in UTC cron syntax.  Comments next
to the cron entries record the Beijing-time intent, and the Python scripts still
perform their own Asia/Shanghai market/trading-day guards.  This check prevents
agents from adding unsupported local-time fields or claiming the cron is already
written in Beijing hours.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
FORBIDDEN_TEXT = (
    "Times are written directly in Beijing time and pinned with timezone",
    "Do not convert these back to UTC",
    "GitHub Actions supports schedule.timezone",
    "pinned with timezone",
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
        for next_line in lines[idx + 1 :]:
            stripped = next_line.strip()
            if not stripped:
                continue
            next_indent = _line_indent(next_line)
            if next_indent <= indent or re.match(r"^\s*-\s*cron:", next_line):
                break
            if stripped.startswith("timezone:"):
                errors.append(f"line {idx + 1}: cron entry must not use timezone fields; encode Beijing time as UTC cron and document it in a comment")
                break

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
