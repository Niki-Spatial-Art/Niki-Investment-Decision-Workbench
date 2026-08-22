#!/usr/bin/env python3
"""Export the daily whole-market action cards as a markdown table.

This is a reporting helper only. It does not generate new signals; it simply
formats the already-produced whole-market snapshot or history entry.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_history(path: Path, as_of_date: str) -> dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"History file not found: {path}")
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if not line.strip():
            continue
        item = json.loads(line)
        if item.get("as_of_date") == as_of_date:
            return item
    raise SystemExit(f"No history entry for {as_of_date}")


def markdown_table(rows: list[dict[str, Any]]) -> str:
    headers = ["日期", "板块", "股票", "产业催化", "价格/成交证据", "反证条件", "结论（观察/复核）", "梅森标签"]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return "\n".join(lines) + "\n"


def csv_table(rows: list[dict[str, Any]], output: Path) -> None:
    headers = ["日期", "板块", "股票", "产业催化", "价格/成交证据", "反证条件", "结论（观察/复核）", "梅森标签"]
    with output.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        for row in rows:
            writer.writerow({h: row.get(h, "") for h in headers})


def main() -> int:
    parser = argparse.ArgumentParser(description="Export daily action cards")
    parser.add_argument("--source", default="reports/whole_market_watch_latest.json")
    parser.add_argument("--history", default="reports/whole_market_watch_history.jsonl")
    parser.add_argument("--date", default="")
    parser.add_argument("--output", default="reports/daily_action_cards_latest.md")
    parser.add_argument("--csv", action="store_true")
    args = parser.parse_args()

    source = ROOT / args.source
    if args.date:
        report = read_history(ROOT / args.history, args.date)
    else:
        report = read_json(source)
    rows = report.get("action_cards") or []
    if not rows:
        raise SystemExit("No action_cards found in the selected report")

    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.csv:
        csv_table(rows, output.with_suffix(".csv"))
        print(f"exported={output.with_suffix('.csv')}")
    else:
        output.write_text(markdown_table(rows), encoding="utf-8")
        print(f"exported={output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
