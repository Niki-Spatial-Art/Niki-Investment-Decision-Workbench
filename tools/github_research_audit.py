"""Read upstream metadata, release notes and README evidence; never install code."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def fetch(repo: str, endpoint: str) -> dict:
    headers = {"User-Agent": "Niki-Workbench-learning-audit", "Accept": "application/vnd.github+json"}
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    url = f"https://api.github.com/repos/{repo}{endpoint}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as response:
            data = json.load(response)
        return {"url": url, "status": "ok", "data": data}
    except urllib.error.HTTPError as exc:
        return {"url": url, "status": "not_found" if exc.code == 404 else "error", "error": f"HTTP {exc.code}"}
    except (OSError, ValueError) as exc:
        return {"url": url, "status": "error", "error": type(exc).__name__}


def audit_repository(item: dict) -> dict:
    repo = item["repo"]
    meta, release, readme = (fetch(repo, endpoint) for endpoint in ("", "/releases/latest", "/readme"))
    m, r, doc = (entry.get("data") or {} for entry in (meta, release, readme))
    readme_text = ""
    if doc.get("encoding") == "base64":
        readme_text = base64.b64decode(doc.get("content", "")).decode("utf-8", errors="replace")
    return {
        **item, "checked_at": datetime.now(timezone.utc).isoformat(),
        "url": m.get("html_url") or f"https://github.com/{repo}",
        "description": m.get("description"), "archived": m.get("archived"),
        "license": (m.get("license") or {}).get("spdx_id"), "pushed_at": m.get("pushed_at"),
        "default_branch": m.get("default_branch"),
        "latest_release": {key: r.get(key) for key in ("tag_name", "published_at", "html_url", "body")},
        "readme": {"url": doc.get("html_url"), "sha": doc.get("sha"), "text": readme_text},
        "requests": [{key: value for key, value in entry.items() if key != "data"} for entry in (meta, release, readme)],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="watchlists/github_learning.json")
    parser.add_argument("--output", default="codex-work/exports/github_learning_latest.json")
    args = parser.parse_args()
    config = json.loads((ROOT / args.config).read_text(encoding="utf-8-sig"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows = list(pool.map(audit_repository, config["repositories"]))
    result = {"generated_at": datetime.now(timezone.utc).isoformat(), "repositories": rows}
    output = ROOT / args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    for row in rows:
        print(row["repo"], row["latest_release"]["tag_name"], row["pushed_at"], [r["status"] for r in row["requests"]])
    return 1 if any(r["status"] == "error" for row in rows for r in row["requests"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
