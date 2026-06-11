"""CD Reference Audit — detects stale ``cd <path>`` blocks in markdown docs.

Walks every code span / fenced code block in the configured markdown files,
finds ``cd <path>`` invocations, and flags any path that does not exist in
the repository. Catches the kind of drift that bit ``docs/make.md`` for a
while ("cd lib/ && make migrate-init" pointing at a ``lib/Makefile`` that
had since been merged back into the root).

Conservative on purpose:
    * Only literal directory paths are validated. Globs, shell-substitutions
      (``$VAR``, ``$(...)``), and absolute paths under ``/tmp`` or ``$HOME``
      are skipped — they're either user-supplied or system locations and
      can't be checked statically.
    * Each code fence is treated as an independent shell session: a ``cd``
      doesn't carry over from one fence to another. Inside a single fence
      we DO carry the cwd forward, so ``cd lib && cd subdir`` resolves
      ``subdir`` relative to ``lib/`` as a real shell would.

Usage:
    python -m doc_checks.check_cd_refs
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from pydantic import BaseModel

from doc_checks import CheckResult, get_config, repo_root
from doc_checks.check_make_refs import _iter_code_segments

REPO_ROOT = repo_root()

_cfg = get_config().get("cd_refs", {})
SCAN_GLOBS: list[str] = _cfg.get("scan_globs", ["**/*.md"])
EXCLUDE_GLOBS: list[str] = _cfg.get("exclude_globs", [])
IGNORE_PREFIXES: tuple[str, ...] = tuple(_cfg.get("ignore_prefixes", []))

# ``cd`` at start-of-segment or after a shell separator. The argument can be
# either a quoted string (preserving embedded spaces) or a bare token. Quotes
# and a trailing slash are stripped later.
_CD_RE = re.compile(
    r"(?:^|(?<=[;&|\n]))\s*cd\s+(?P<path>\"[^\"]*\"|'[^']*'|[^\s;&|<>#]+)",
)

# Tokens we never try to resolve — they're either runtime-supplied or live
# outside the repo and are irrelevant to documentation drift.
_DYNAMIC_PREFIXES = ("$", "~", "/tmp", "/var", "/etc", "/usr", "/opt", "..")


class StaleCd(BaseModel):
    file: str
    line: int
    path: str
    raw: str


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


def _is_dynamic(path: str) -> bool:
    """Return True for paths we deliberately skip (variables, globs, system dirs)."""
    if not path or any(ch in path for ch in "*?[]{}"):
        return True
    if path.startswith(_DYNAMIC_PREFIXES):
        return True
    if path.startswith("/") and not path.startswith(str(REPO_ROOT)):
        return True
    if any(path.startswith(p) for p in IGNORE_PREFIXES):
        return True
    return False


def _resolve(cwd: Path, raw: str) -> Path:
    """Resolve a ``cd`` argument against the current segment cwd.

    Bare names (``lib``) and dotted paths (``./lib``) resolve relative to the
    walking cwd, exactly like a real shell. Repo-rooted absolute paths are
    rebased onto REPO_ROOT so the check is portable across checkouts.
    """
    cleaned = _strip_quotes(raw).rstrip("/")
    if cleaned.startswith(str(REPO_ROOT)):
        return Path(cleaned)
    return (cwd / cleaned).resolve()


def find_stale_cd_refs(files: list[Path] | None = None) -> list[StaleCd]:
    files = files if files is not None else list(_iter_markdown_files())
    stale: list[StaleCd] = []

    for path in files:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue

        # ``_iter_code_segments`` yields (line_no, segment). Fenced blocks
        # emit one segment per line — we re-group them so a single ``cd``
        # carries forward inside the same fence (i.e. consecutive lines).
        prev_line: int | None = None
        cwd = REPO_ROOT
        for lineno, segment in _iter_code_segments(text):
            if prev_line is None or lineno != prev_line + 1:
                # Boundary between two non-adjacent code regions: reset cwd
                # so a stale ``cd`` doesn't leak across separate blocks.
                cwd = REPO_ROOT
            prev_line = lineno

            for m in _CD_RE.finditer(segment):
                raw = m.group("path")
                if _is_dynamic(raw):
                    continue
                resolved = _resolve(cwd, raw)
                if resolved.is_dir():
                    cwd = resolved
                    continue
                stale.append(
                    StaleCd(
                        file=str(path.relative_to(REPO_ROOT)),
                        line=lineno,
                        path=raw,
                        raw=segment.strip(),
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
    """Resolve hook-supplied paths, **respecting EXCLUDE_GLOBS**.

    Pre-commit passes every changed markdown file, including ones we want to
    skip (CHANGELOG.md quotes stale paths on purpose). Without this filter
    the hook would block any commit that documents the very drift the check
    is designed to catch.
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
    stale = find_stale_cd_refs(target_files)

    if not stale:
        scope = f"{len(target_files)} file(s)" if target_files is not None else "all docs"
        return CheckResult(passed=True, message=f"No stale `cd` paths in {scope}.")

    details: list[str] = [f"Found {len(stale)} stale `cd` path(s):"]
    last_file: str | None = None
    for ref in sorted(stale, key=lambda r: (r.file, r.line, r.path)):
        if ref.file != last_file:
            details.append(f"  {ref.file}:")
            last_file = ref.file
        details.append(f"    line {ref.line}: `cd {ref.path}` — no such directory")
    details += [
        "",
        "Fix: update the docs to point at the new location, or remove the block",
        "if the workflow is gone. Paths that are runtime-substituted should use",
        "`$VAR` syntax so this check skips them.",
    ]
    return CheckResult(
        passed=False,
        message=f"{len(stale)} stale `cd` path(s) in documentation.",
        details=details,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect stale `cd <path>` references in markdown.")
    parser.add_argument("files", nargs="*", help="Restrict scan to these files (default: configured globs)")
    args = parser.parse_args()

    result = run(files=args.files or None)
    status = "PASS ✅" if result.passed else "FAIL ❌"
    print(f"[CD Ref Audit] {status}: {result.message}")
    for line in result.details:
        print(f"  {line}")
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
