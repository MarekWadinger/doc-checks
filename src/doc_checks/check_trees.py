"""Tree Reference Audit — detects stale paths in ASCII project-trees in markdown.

Scans the configured markdown files for fenced code blocks that look like a
directory tree (they contain box-drawing or ASCII tree connectors such as
``├──``, ``└──``, ``│``, ``|--``, `` `-- ``). For each branch line it extracts
the referenced name and verifies that a matching file or directory still
exists somewhere in the repo. Catches README rot when files are renamed,
moved, or deleted.

Conservative on purpose — to avoid false positives on illustrative trees:
    * Only branch lines (``├──`` / ``└──`` and ASCII equivalents) are checked.
      The unindented root line of a tree is not validated.
    * Matching is by basename, not full reconstructed path. A tree claiming
      ``src/api.ts`` passes if an ``api.ts`` exists anywhere — we trust the
      tree's shape and only catch names that have disappeared entirely.
    * Placeholders (``...``, ``etc.``, ``…``), wildcards (``*.py``), and names
      starting with ``..`` are skipped, as are ``ls -F`` / ``tree -F`` type
      indicators (a trailing ``*``, ``@``, ``=``, ``|``, ``>`` is stripped).

Usage:
    python -m doc_checks.check_trees
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from doc_checks import CheckResult, get_config, repo_root

REPO_ROOT = repo_root()

_cfg = get_config().get("trees", {})
SCAN_GLOBS: list[str] = _cfg.get("scan_globs", ["**/*.md"])
EXCLUDE_GLOBS: list[str] = _cfg.get("exclude_globs", [])
IGNORE_NAMES: set[str] = set(_cfg.get("ignore_names", ["...", "…", "etc", "etc."]))
# Directories never walked when collecting the set of existing basenames.
PRUNE_DIRS: set[str] = set(_cfg.get("prune_dirs", [".git", "node_modules", ".venv", "site-packages", "__pycache__"]))

# A fenced code block is treated as a tree if any line carries one of these
# connectors. Box-drawing (``├ └ │ ─``) and the common ASCII renderings.
TREE_HINT = re.compile(r"[│├└─]|(?:[|`+\\]-{1,2}\s)")

# A branch line: optional indentation made of vertical bars / spaces, a
# connector glyph, one or two dashes, whitespace, then the entry name. Matches
# ``│   ├── name``, ``└── name``, ASCII ``|   |-- name``, ``` `-- name ```,
# ``+-- name`` and ``\-- name``.
BRANCH = re.compile(
    r"^[│|\s]*"  # indentation (vertical bars + spaces)
    r"[├└`+\\|]"  # connector glyph
    r"[─-]{1,2}\s+"  # one/two dashes then whitespace
    r"(?P<name>[^\s#]+)"  # the entry name (stops at whitespace or a comment)
)

# Per-block opt-out for conceptual box-drawn diagrams that aren't project
# trees (decision trees, struct/field listings). Either tag the fence info
# string (```` ```text no-trees ````) or precede the fence with the comment
# ``<!-- doc-checks: ignore-trees -->``. See issue #9.
_IGNORE_TREES_RE = re.compile(r"<!--\s*doc-checks:\s*ignore-trees\s*-->")

# Trailing characters that ``ls -F`` / ``tree -F`` append to indicate file type.
_FTYPE_INDICATORS = "*/@=|>"
_WILDCARD_CHARS = set("*?[]")


def _clean_name(raw: str) -> str | None:
    """Normalize a captured branch name, or return None if it isn't a real path.

    Strips surrounding backticks and a single trailing file-type indicator,
    then rejects placeholders, wildcards, and ``..`` parents.
    """
    name = raw.strip("`").strip()
    if name and name[-1] in _FTYPE_INDICATORS:
        name = name[:-1]
    name = name.rstrip("/")
    if not name or name in IGNORE_NAMES:
        return None
    if name.startswith(".."):
        return None
    if any(ch in _WILDCARD_CHARS for ch in name):
        return None
    # Must contain at least one path-ish character (letter/digit). Pure
    # punctuation rows (e.g. ``---``) are decoration, not entries.
    if not any(ch.isalnum() for ch in name):
        return None
    return name


class StaleTreeEntry(BaseModel):
    file: str
    line: int
    name: str


def _iter_tree_fences(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_no, raw_line)`` for every line inside a tree-shaped fence.

    Supports both ``` and ``~~~`` fences and only emits the lines of fences
    that contain at least one tree connector, so prose code blocks are ignored.
    """
    in_fence = False
    fence_marker: str | None = None
    buffer: list[tuple[int, str]] = []
    fence_has_tree = False
    skip_fence = False  # this fence opted out (info-string or preceding comment)
    pending_ignore = False  # an ignore-trees comment applies to the next fence

    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = True
                fence_marker = stripped[:3]
                fence_has_tree = False
                buffer = []
                skip_fence = pending_ignore or "no-trees" in stripped[3:]
                pending_ignore = False
            elif _IGNORE_TREES_RE.search(line):
                pending_ignore = True
            elif stripped:
                pending_ignore = False  # any other content cancels a pending opt-out
            continue

        # Inside a fence: a line starting with the same marker closes it.
        if fence_marker is not None and stripped.startswith(fence_marker):
            if fence_has_tree and not skip_fence:
                yield from buffer
            in_fence = False
            fence_marker = None
            fence_has_tree = False
            skip_fence = False
            buffer = []
            continue

        if TREE_HINT.search(line):
            fence_has_tree = True
        buffer.append((lineno, line))

    # Unterminated fence at EOF: still report what we gathered if it's a tree.
    if in_fence and fence_has_tree and not skip_fence:
        yield from buffer


def extract_tree_entries(text: str) -> list[tuple[int, str]]:
    """Return ``[(line_no, name), ...]`` for branch lines in tree fences."""
    entries: list[tuple[int, str]] = []
    for lineno, line in _iter_tree_fences(text):
        m = BRANCH.match(line)
        if not m:
            continue
        name = _clean_name(m.group("name"))
        if name is not None:
            entries.append((lineno, name))
    return entries


def existing_basenames(root: Path | None = None) -> set[str]:
    """All file and directory basenames under ``root``, for membership checks.

    Directories in ``PRUNE_DIRS`` are skipped entirely (both as candidates and
    as walk targets) so vendored/build trees don't mask real drift.
    """
    root = root or REPO_ROOT
    names: set[str] = set()
    for _dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        names.update(dirnames)
        names.update(filenames)
    return names


def find_stale_trees(
    files: list[Path] | None = None,
    names: set[str] | None = None,
) -> list[StaleTreeEntry]:
    """Find tree entries whose basename no longer exists in the repo.

    ``names`` is the set of existing basenames; it defaults to a fresh scan of
    the repo and is injectable for testing.
    """
    files = files if files is not None else _iter_markdown_files()
    names = names if names is not None else existing_basenames()

    stale: list[StaleTreeEntry] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, name in extract_tree_entries(text):
            # ``src/api.ts`` style entries: check the leaf basename, since we
            # don't reconstruct the full path from the tree's indentation.
            leaf = name.rsplit("/", 1)[-1]
            if name in names or leaf in names:
                continue
            stale.append(
                StaleTreeEntry(
                    file=str(path.relative_to(REPO_ROOT)),
                    line=lineno,
                    name=name,
                )
            )
    return stale


def _iter_markdown_files() -> list[Path]:
    seen: set[Path] = set()
    out: list[Path] = []
    for glob in SCAN_GLOBS:
        for p in REPO_ROOT.glob(glob):
            if not p.is_file() or p in seen:
                continue
            rel = p.relative_to(REPO_ROOT)
            if any(rel.full_match(pat) for pat in EXCLUDE_GLOBS):
                continue
            seen.add(p)
            out.append(p)
    return out


def _resolve_hook_files(arg_files: list[str]) -> list[Path]:
    """Resolve hook-supplied paths, respecting EXCLUDE_GLOBS.

    Pre-commit passes every changed markdown file, including ones we skip on
    purpose (e.g. ``CHANGELOG.md`` may quote a historical tree). Filtering here
    keeps the hook from blocking commits that touch those files.
    """
    out: list[Path] = []
    for raw in arg_files:
        path = (REPO_ROOT / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        if not path.exists() or path.suffix.lower() not in {".md", ".markdown"}:
            continue
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        if any(rel.full_match(pat) for pat in EXCLUDE_GLOBS):
            continue
        out.append(path)
    return out


def run(files: list[str] | None = None) -> CheckResult:
    target_files = _resolve_hook_files(files) if files else None
    stale = find_stale_trees(target_files)

    if not stale:
        scope = f"{len(target_files)} file(s)" if target_files is not None else "all docs"
        return CheckResult(passed=True, message=f"All tree entries reference existing paths in {scope}.")

    details: list[str] = [f"Found {len(stale)} stale tree entry(ies):"]
    last_file: str | None = None
    for ref in sorted(stale, key=lambda r: (r.file, r.line, r.name)):
        if ref.file != last_file:
            details.append(f"  {ref.file}:")
            last_file = ref.file
        details.append(f"    line {ref.line}: `{ref.name}` — not found in repo")
    details += [
        "",
        "Fix: update the tree to match the current layout, or remove the entry",
        "if the file is gone. Placeholders should read `...` so this check skips",
        "them.",
    ]
    return CheckResult(
        passed=False,
        message=f"{len(stale)} stale tree entry(ies) in documentation.",
        details=details,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect stale ASCII project-tree entries in markdown.")
    parser.add_argument("files", nargs="*", help="Restrict scan to these files (default: configured globs)")
    args = parser.parse_args()

    result = run(files=args.files or None)
    status = "PASS ✅" if result.passed else "FAIL ❌"
    print(f"[Tree Ref Audit] {status}: {result.message}")
    for line in result.details:
        print(f"  {line}")
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
