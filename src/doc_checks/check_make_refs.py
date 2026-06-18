"""Make Reference Audit — detects stale ``make <target>`` references in markdown docs.

Parses the project Makefile to build the set of real targets, then scans markdown
files for ``make <target>`` usages **inside code spans or fenced code blocks only**
(prose mentions like "make sure to run the tests" are ignored). Each unknown
target is reported with file:line so it can be fixed or removed.

Usage:
    python -m doc_checks.check_make_refs
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from doc_checks import CheckResult, get_config, repo_root

REPO_ROOT = repo_root()

_cfg = get_config().get("make_refs", {})
MAKEFILE_PATHS: list[str] = _cfg.get("makefiles", ["Makefile"])
SCAN_GLOBS: list[str] = _cfg.get("scan_globs", ["**/*.md"])
EXCLUDE_GLOBS: list[str] = _cfg.get("exclude_globs", [])
IGNORE_TARGETS: set[str] = set(_cfg.get("ignore_targets", []))

# Matches lines like ``foo:`` or ``foo bar baz: deps`` but skips
# variable assignments (``FOO := ...``, ``FOO ?= ...``, ``FOO += ...``)
# and pattern rules (``%.o: %.c``).
_TARGET_LINE_RE = re.compile(r"^(?P<lhs>[A-Za-z_][\w./-]*(?:\s+[A-Za-z_][\w./-]*)*)\s*:(?!=)")
_TOKEN_RE = re.compile(r"^[A-Za-z_][\w./-]*$")

# Fence detection: ``` or ~~~ optionally followed by an info string.
_FENCE_RE = re.compile(r"^(?P<indent>\s{0,3})(?P<marker>`{3,}|~{3,})(?P<info>[^\n]*)")

# Info-string tags considered "shell-like" — only these fences are scanned
# for ``make X`` / ``cd X`` patterns. Python / yaml / json / etc. fences may
# contain ``# make sure to ...`` in comments or string literals and would
# otherwise produce noisy false positives. Empty info string (no tag) is
# also treated as shell-like since the convention in this repo is to omit
# the tag on bash-style snippets.
_SHELL_FENCE_TAGS: frozenset[str] = frozenset(
    {
        "",
        "bash",
        "sh",
        "shell",
        "zsh",
        "console",
        "shellsession",
        "terminal",
    }
)

# Inline code: backtick-delimited span on a single line. We deliberately
# ignore double-backtick spans rather than try to parse CommonMark fully;
# false negatives are cheaper than false positives on a lint hook.
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")

# Shell terminators that end a single command; everything after belongs to a
# new command (``;``, ``&&``, ``||``, ``|``, ``&``, redirection) or is a shell
# comment (`` # ``). Everything after such a token is not part of the current
# ``make`` invocation and must not be parsed as a target.
_CMD_TERMINATOR_RE = re.compile(r"(\|\||&&|[;|&<>]|(?:^|\s)#)")


class StaleRef(BaseModel):
    """Single unknown ``make`` target occurrence."""

    file: str
    line: int
    target: str
    raw: str
    suggestion: str | None = None


def parse_makefile_targets(path: Path) -> set[str]:
    """Extract phony/file targets defined in a Makefile.

    Skips recipe lines (TAB-indented), comments, special targets (``.PHONY``
    et al.) and variable assignments. Multi-target lines like
    ``migrate: migrate-revision migrate-apply`` contribute only the LHS
    (``migrate``); the RHS tokens are prerequisites which will appear as
    targets elsewhere if they are real.
    """
    targets: set[str] = set()
    if not path.exists():
        return targets
    for line in path.read_text().splitlines():
        if not line or line.startswith("\t") or line.lstrip().startswith("#"):
            continue
        stripped = line.lstrip()
        # Skip special targets entirely (``.PHONY``, ``.DEFAULT_GOAL``, ``.ONESHELL``…).
        # In particular ``.PHONY`` is **not** authoritative — listing a name
        # there without a ``name: <recipe>`` block leaves it undefined, so
        # harvesting from it would mask exactly the kind of drift this check
        # is meant to detect (recipe removed, ``.PHONY`` entry left behind).
        if stripped.startswith("."):
            continue
        m = _TARGET_LINE_RE.match(stripped)
        if not m:
            continue
        for token in m.group("lhs").split():
            if _TOKEN_RE.match(token):
                targets.add(token)
    return targets


def _expand_braces(token: str) -> list[str]:
    """Expand a single level of brace alternation, e.g. ``a-{b,c}`` → ``a-b``, ``a-c``.

    Recurses so nested patterns like ``{x,y}-{a,b}`` still expand. Tokens with no
    braces pass through unchanged. We bail out (returning the literal token) if
    a brace block is empty or unbalanced — those are not real targets anyway.
    """
    if "{" not in token:
        return [token]
    depth = 0
    start = -1
    for i, ch in enumerate(token):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                inner = token[start + 1 : i]
                if not inner:
                    return [token]
                prefix, suffix = token[:start], token[i + 1 :]
                results: list[str] = []
                for opt in inner.split(","):
                    for expanded in _expand_braces(prefix + opt + suffix):
                        results.append(expanded)
                return results
    return [token]


def _iter_make_targets_in_segment(segment: str) -> Iterator[str]:
    """Yield each target word following a ``make`` invocation in one code segment.

    Walks left-to-right, finding the standalone keyword ``make`` and then
    consuming tokens until a shell terminator. Tokens that look like flags
    (``-j2``), variable assignments (``EVAL_TAGS=test``), make-internal
    variables (``$(MAKE)``) or brace-expansion shells (``{a,b}``) are
    expanded or skipped as appropriate.
    """
    for m in re.finditer(r"(?<![A-Za-z_0-9./-])make\b", segment):
        rest = segment[m.end() :]
        term = _CMD_TERMINATOR_RE.search(rest)
        chunk = rest[: term.start()] if term else rest
        for raw_token in chunk.split():
            if raw_token.startswith("-"):
                continue  # flag, not a target
            if raw_token.startswith("$"):
                continue  # ``$(MAKE)``, ``$FOO`` — not a literal target
            if "=" in raw_token:
                continue  # ``KEY=value`` env override (before or after target)
            for expanded in _expand_braces(raw_token):
                if _TOKEN_RE.match(expanded):
                    yield expanded


def _iter_code_segments(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(line_no, code)`` for every code region in a markdown document.

    Recognises:
        * Fenced blocks delimited by matching ``````` or ``~~~``
          runs (CommonMark §4.5). Each line inside the fence is yielded with
          its own line number, so reports point at the exact line.
        * Inline code spans delimited by single backticks on a single line
          (CommonMark §6.1, simplified — we don't handle escaped backticks
          inside spans because they are vanishingly rare in Makefile recipes).
    """
    in_fence = False
    fence_marker: str | None = None
    fence_is_shell = True  # inline spans + un-tagged fences are treated as shell
    for lineno, line in enumerate(text.splitlines(), start=1):
        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group("marker")
            if not in_fence:
                info = fence.group("info").strip().split()
                tag = info[0].lower() if info else ""
                in_fence = True
                fence_marker = marker[0] * 3  # canonical 3-char form
                fence_is_shell = tag in _SHELL_FENCE_TAGS
                continue
            # Closing fence must use the same character (``~`` vs `````).
            if fence_marker is not None and marker[0] * 3 == fence_marker:
                in_fence = False
                fence_marker = None
                fence_is_shell = True
                continue
        if in_fence:
            if fence_is_shell:
                yield lineno, line
        else:
            for m in _INLINE_CODE_RE.finditer(line):
                yield lineno, m.group(1)


def _suggest(target: str, known: set[str]) -> str | None:
    matches = difflib.get_close_matches(target, sorted(known), n=1, cutoff=0.7)
    return matches[0] if matches else None


def _iter_markdown_files() -> Iterator[Path]:
    seen: set[Path] = set()
    for glob in SCAN_GLOBS:
        for path in REPO_ROOT.glob(glob):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO_ROOT)
            if any(rel.full_match(pat) for pat in EXCLUDE_GLOBS):
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path


def find_stale_refs(files: list[Path] | None = None) -> tuple[list[StaleRef], set[str]]:
    """Return (stale_refs, known_targets) for the given (or all configured) files."""
    known: set[str] = set()
    for rel in MAKEFILE_PATHS:
        known |= parse_makefile_targets(REPO_ROOT / rel)
    known |= IGNORE_TARGETS

    targets_iter = files if files is not None else list(_iter_markdown_files())
    stale: list[StaleRef] = []
    for path in targets_iter:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, segment in _iter_code_segments(text):
            for target in _iter_make_targets_in_segment(segment):
                if target in known:
                    continue
                stale.append(
                    StaleRef(
                        file=str(path.relative_to(REPO_ROOT)),
                        line=lineno,
                        target=target,
                        raw=segment.strip(),
                        suggestion=_suggest(target, known),
                    )
                )
    return stale, known


def _resolve_hook_files(arg_files: list[str]) -> list[Path] | None:
    """Resolve CLI/hook-supplied paths into the set of markdown files to scan.

    Returns ``None`` to mean "scan everything configured" — used when the
    Makefile itself was modified, because a removed target could invalidate
    references anywhere in the docs and pre-commit will not list those
    markdown files as changed. Otherwise returns the changed markdown files
    so the hook stays cheap on the common path.
    """
    rescan_all = False
    out: list[Path] = []
    for raw in arg_files:
        path = (REPO_ROOT / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        if str(rel) in MAKEFILE_PATHS:
            rescan_all = True
            continue
        if not path.exists() or path.suffix.lower() not in {".md", ".markdown"}:
            continue
        # Apply EXCLUDE_GLOBS here too. _iter_markdown_files honours them, but pre-commit
        # passes explicit changed files directly to this hook path, which previously bypassed
        # the configured exclusions (e.g. CHANGELOG.md is intentionally excluded but kept
        # firing on every changelog edit).
        if any(rel.full_match(pat) for pat in EXCLUDE_GLOBS):
            continue
        out.append(path)
    if rescan_all:
        return None
    return out


def run(files: list[str] | None = None) -> CheckResult:
    """Entry point for the runner.

    Args:
        files: Optional list of paths to restrict scanning to (used by the
            pre-commit hook). When ``None`` or empty the configured globs are
            scanned in full.

    """
    target_files = _resolve_hook_files(files) if files else None
    stale, known = find_stale_refs(target_files)

    # No references to validate ⇒ nothing can be stale. This must be checked
    # *before* ``not known`` so a repo with no Makefile and no ``make`` refs
    # passes cleanly (e.g. projects using ``just`` / ``task`` / npm scripts).
    if not stale:
        scope = f"{len(target_files)} file(s)" if target_files is not None else "all docs"
        return CheckResult(passed=True, message=f"No stale `make` references in {scope}.")

    # References exist but there is no Makefile to validate them against. Every
    # ref is "stale" by default here; report it as a configuration gap rather
    # than as drift, since the fix is to add a Makefile or ignore the refs.
    if not known:
        details = [
            f"Looked at: {', '.join(MAKEFILE_PATHS)}",
            "",
            f"Found {len(stale)} `make` reference(s) but no Makefile targets to validate against:",
        ]
        last_file: str | None = None
        for ref in sorted(stale, key=lambda r: (r.file, r.line, r.target)):
            if ref.file != last_file:
                details.append(f"  {ref.file}:")
                last_file = ref.file
            details.append(f"    line {ref.line}: `make {ref.target}`")
        return CheckResult(
            passed=False,
            message="`make` references found but no Makefile to validate against.",
            details=details,
        )

    details: list[str] = [
        f"Found {len(stale)} stale `make` reference(s):",
    ]
    last_file: str | None = None
    for ref in sorted(stale, key=lambda r: (r.file, r.line, r.target)):
        if ref.file != last_file:
            details.append(f"  {ref.file}:")
            last_file = ref.file
        hint = f" (did you mean `{ref.suggestion}`?)" if ref.suggestion else ""
        details.append(f"    line {ref.line}: `make {ref.target}`{hint}")
    details += [
        "",
        "Fix: update the docs to use a real target, or add the target to the Makefile.",
        "If the reference is intentional (e.g. third-party project), add it to",
        "`make_refs.ignore_targets` in `.doc-checks.yaml` at your repo root.",
    ]
    return CheckResult(
        passed=False,
        message=f"{len(stale)} stale `make` reference(s) in documentation.",
        details=details,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect stale `make <target>` references in markdown.")
    parser.add_argument("files", nargs="*", help="Restrict scan to these files (default: configured globs)")
    args = parser.parse_args()

    result = run(files=args.files or None)

    status = "PASS ✅" if result.passed else "FAIL ❌"
    print(f"[Make Ref Audit] {status}: {result.message}")
    for line in result.details:
        print(f"  {line}")

    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
