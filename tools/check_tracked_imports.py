"""Reject tracked Python callers whose existing local dependencies are untracked."""
from __future__ import annotations
import ast
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def missing_tracked_dependencies(root: Path, tracked: set[str]) -> list[str]:
    problems = []
    for name in sorted(tracked):
        path = root / name
        if path.suffix != ".py" or not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
                bases = [path.parent, root]
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                modules = [module] if module else []
                modules += [f"{module}.{alias.name}".strip(".") for alias in node.names if alias.name != "*"]
                base = path.parent
                for _ in range(max(0, node.level - 1)):
                    base = base.parent
                bases = [base] if node.level else [path.parent, root]
            else:
                continue
            for module in modules:
                for base in bases:
                    target = base.joinpath(*module.split("."))
                    for candidate in (target.with_suffix(".py"), target / "__init__.py"):
                        if candidate.is_file() and candidate.is_relative_to(root):
                            rel = candidate.relative_to(root).as_posix()
                            if rel not in tracked:
                                problems.append(f"{name}:{node.lineno}: local dependency is not tracked: {rel}")
    return sorted(set(problems))


def main() -> int:
    result = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    tracked = set(result.stdout.decode("utf-8").split("\0")) - {""}
    problems = missing_tracked_dependencies(ROOT, tracked)
    for problem in problems:
        print(problem)
    print("tracked_imports=failed" if problems else "tracked_imports=ok")
    return bool(problems)


if __name__ == "__main__":
    raise SystemExit(main())
