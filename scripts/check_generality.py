"""Generality and boundary check, run by `uv run tasks lint`.

Enforces three ground rules from CLAUDE.md on the agent code and prompts:

1. No names from the sandbox world (vendors, people, companies from the seed
   generator's name pools) and no task ids appear in `breadcrumb/` or `prompts/`.
2. `breadcrumb/` never imports from `sandbox` or `harness`.
3. Nothing in the agent refers to the oracle (its name, port or config key).

The deny-list comes from whole name pools, not one seed's output, so names that
only appear under other seeds (as in held-out runs) are caught too.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

AGENT_DIRS = ("breadcrumb", "prompts")
FORBIDDEN_IMPORTS = ("sandbox", "harness")
ORACLE_TERMS = ("oracle", "8109", "ORACLE_URL")
NAME_POOLS_FILE = Path("sandbox/seed/name_pools.json")
TASK_DIRS = (Path("harness/tasks/dev"), Path("harness/tasks/heldout"))
TEXT_SUFFIXES = {".py", ".txt", ".md", ".j2", ".jinja", ".html", ".yaml", ".yml", ".json"}


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    rule: str
    detail: str

    def __str__(self) -> str:
        return f"{self.path.as_posix()}:{self.line}: [{self.rule}] {self.detail}"


def load_deny_terms(root: Path) -> list[str]:
    """Every name any seed could produce, plus every task id."""
    terms: set[str] = set()
    pools_path = root / NAME_POOLS_FILE
    if pools_path.exists():
        pools = json.loads(pools_path.read_text(encoding="utf-8"))
        for names in pools.values():
            terms.update(str(name) for name in names)
    for task_dir in TASK_DIRS:
        for task_file in sorted((root / task_dir).glob("*.y*ml")):
            task = yaml.safe_load(task_file.read_text(encoding="utf-8")) or {}
            if task.get("id"):
                terms.add(str(task["id"]))
    return sorted(t for t in terms if t.strip())


def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(term)}(?!\w)", re.IGNORECASE)


def _agent_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for d in AGENT_DIRS:
        base = root / d
        if base.exists():
            files.extend(
                p for p in sorted(base.rglob("*")) if p.is_file() and p.suffix in TEXT_SUFFIXES
            )
    return files


def _import_violations(path: Path, rel: Path, source: str) -> list[Violation]:
    found: list[Violation] = []
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules = [node.module]
        else:
            continue
        for module in modules:
            if module.split(".")[0] in FORBIDDEN_IMPORTS:
                found.append(Violation(rel, node.lineno, "boundary", f"imports {module}"))
    return found


def check(root: Path) -> list[Violation]:
    deny = [(term, _term_pattern(term)) for term in load_deny_terms(root)]
    oracle = [(term, _term_pattern(term)) for term in ORACLE_TERMS]
    violations: list[Violation] = []
    for path in _agent_files(root):
        rel = path.relative_to(root)
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".py" and rel.parts[0] == "breadcrumb":
            violations.extend(_import_violations(path, rel, source))
        for line_no, line in enumerate(source.splitlines(), start=1):
            for term, pattern in oracle:
                if pattern.search(line):
                    violations.append(Violation(rel, line_no, "oracle", f"mentions {term!r}"))
            for term, pattern in deny:
                if pattern.search(line):
                    violations.append(Violation(rel, line_no, "generality", f"mentions {term!r}"))
    return violations


def main(root: Path | None = None) -> int:
    root = root or Path.cwd()
    violations = check(root)
    for v in violations:
        print(v)
    if violations:
        print(f"generality check: {len(violations)} violation(s)")
        return 1
    print(f"generality check: ok ({len(load_deny_terms(root))} deny-listed names)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
