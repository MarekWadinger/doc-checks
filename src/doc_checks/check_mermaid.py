"""Mermaid Audit — validates syntax of ``mermaid`` fenced blocks via ``mmdc``.

Extracts each ``mermaid`` block from the configured markdown files and pipes
it to ``mmdc`` (``@mermaid-js/mermaid-cli``). Non-zero exit means the block
won't render on GitHub/GitLab — that's exactly what readers see as a broken
diagram.

Gracefully skips when ``mmdc`` is not installed. The hook is meant to be
opt-in for contributors with Node/mmdc; CI runs with mmdc installed and
will catch regressions there.

Usage:
    python -m doc_checks.check_mermaid
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from doc_checks import CheckResult, get_config, repo_root

REPO_ROOT = repo_root()

_cfg = get_config().get("mermaid", {})
SCAN_GLOBS: list[str] = _cfg.get("scan_globs", ["**/*.md"])
EXCLUDE_GLOBS: list[str] = _cfg.get("exclude_globs", [])
# Allow CI to demand mmdc presence even when locally it would skip; otherwise
# absence silently passes so contributors without Node aren't blocked.
REQUIRE_MMDC: bool = bool(_cfg.get("require_mmdc", False)) or bool(os.environ.get("DOC_CHECK_MERMAID_REQUIRE"))


class BadDiagram(BaseModel):
    file: str
    line: int
    error: str


def _iter_mermaid_fences(text: str) -> Iterator[tuple[int, str]]:
    in_fence = False
    fence_marker: str | None = None
    is_mermaid = False
    body: list[str] = []
    body_start = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.lstrip()
        if not in_fence:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                marker = stripped[:3]
                info = stripped[3:].strip().split()
                tag = info[0] if info else ""
                in_fence = True
                fence_marker = marker
                is_mermaid = tag == "mermaid"
                body = []
                body_start = lineno + 1
            continue
        if fence_marker is not None and stripped.startswith(fence_marker):
            if is_mermaid and body:
                yield body_start, "\n".join(body)
            in_fence = False
            fence_marker = None
            is_mermaid = False
            body = []
            continue
        if is_mermaid:
            body.append(line)


def _validate_with_mmdc(body: str, mmdc: str) -> str | None:
    """Run mmdc on a diagram body, returning an error string or None on success.

    We render to a throwaway SVG in a tmpdir; rendering is the only way mmdc
    surfaces parse errors. The SVG is discarded.
    """
    with tempfile.TemporaryDirectory() as tmp:
        in_path = Path(tmp) / "diagram.mmd"
        out_path = Path(tmp) / "diagram.svg"
        in_path.write_text(body)
        try:
            result = subprocess.run(
                [mmdc, "-i", str(in_path), "-o", str(out_path), "--quiet"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return "mmdc timed out (30s)"
        if result.returncode == 0:
            return None
        return (result.stderr or result.stdout or "unknown error").strip().splitlines()[-1]


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
    """Resolve hook-supplied paths, respecting EXCLUDE_GLOBS."""
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
    mmdc = shutil.which("mmdc")
    if mmdc is None:
        if REQUIRE_MMDC:
            return CheckResult(
                passed=False,
                message="`mmdc` not found on PATH (required by config / DOC_CHECK_MERMAID_REQUIRE).",
                details=["Install: `npm install -g @mermaid-js/mermaid-cli`"],
            )
        return CheckResult(
            passed=True,
            message="`mmdc` not installed — mermaid validation skipped (set DOC_CHECK_MERMAID_REQUIRE=1 to fail).",
        )

    target_files = _resolve_hook_files(files) if files else None
    files_to_scan = target_files if target_files is not None else _iter_markdown_files()

    bad: list[BadDiagram] = []
    diagram_count = 0
    for path in files_to_scan:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for start, body in _iter_mermaid_fences(text):
            diagram_count += 1
            err = _validate_with_mmdc(body, mmdc)
            if err:
                bad.append(
                    BadDiagram(
                        file=str(path.relative_to(REPO_ROOT)),
                        line=start,
                        error=err,
                    )
                )

    if not bad:
        scope = f"{len(files_to_scan)} file(s)" if target_files is not None else "all docs"
        return CheckResult(passed=True, message=f"All {diagram_count} mermaid diagram(s) parse cleanly in {scope}.")

    details: list[str] = [f"Found {len(bad)} broken mermaid diagram(s):"]
    last_file: str | None = None
    for ref in sorted(bad, key=lambda r: (r.file, r.line)):
        if ref.file != last_file:
            details.append(f"  {ref.file}:")
            last_file = ref.file
        details.append(f"    line {ref.line}: {ref.error}")
    return CheckResult(
        passed=False,
        message=f"{len(bad)}/{diagram_count} mermaid diagram(s) fail to parse.",
        details=details,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate mermaid diagrams in markdown via mmdc.")
    parser.add_argument("files", nargs="*", help="Restrict scan to these files (default: configured globs)")
    args = parser.parse_args()
    result = run(files=args.files or None)
    status = "PASS ✅" if result.passed else "FAIL ❌"
    print(f"[Mermaid Audit] {status}: {result.message}")
    for line in result.details:
        print(f"  {line}")
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
