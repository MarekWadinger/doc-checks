"""Python Import Audit — detects stale imports of project-local symbols in markdown.

Walks every ``python`` fenced block in the configured markdown files, parses
each block as Python (so we also surface syntax errors), and verifies that
``from <project>.X import Y`` / ``import <project>.X`` references resolve
inside the running source tree.

Why a custom check instead of ``pytest-markdown-docs``?

    Most ``python`` fences in this repo's READMEs are illustrative — dangling
    ``__all__ = [...]`` lists, snippets that omit surrounding context, etc.
    Executing them via ``pytest-markdown-docs`` would mass-fail unless every
    snippet were annotated with a skip marker. The actual reader-pain we
    want to catch is the *import* going stale after a rename or move, which
    we can validate statically via ``importlib.util.find_spec`` without
    running the snippet at all.

External imports (``pydantic``, ``sqlmodel``, …) are deliberately not
validated — they're version-pinned in ``pyproject.toml`` and a missing
external dep would surface there first.

Usage:
    python -m doc_checks.check_py_imports
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel

from doc_checks import CheckResult, get_config, repo_root

REPO_ROOT = repo_root()

# ``find_spec`` resolves against sys.path. Installed as a pre-commit hook this
# package runs from pre-commit's isolated venv, so the consumer repo's own
# packages aren't importable unless its root is added explicitly (this mirrors
# the implicit cwd-on-sys.path behavior of the old ``python -m`` invocation).
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_cfg = get_config().get("py_imports", {})
SCAN_GLOBS: list[str] = _cfg.get("scan_globs", ["**/*.md"])
EXCLUDE_GLOBS: list[str] = _cfg.get("exclude_globs", [])
# Top-level packages considered "project-local" and therefore worth validating.
# Anything else (third-party, stdlib) is left to the regular dependency tooling.
PROJECT_PACKAGES: set[str] = set(_cfg.get("project_packages", ["src", "lib", "scripts"]))


class StaleImport(BaseModel):
    file: str
    line: int  # line in the markdown
    module: str
    name: str | None = None
    reason: str


class FenceError(BaseModel):
    file: str
    line: int
    error: str


def _iter_python_fences(text: str) -> Iterator[tuple[int, str]]:
    """Yield ``(start_line, body)`` for each ``python`` fenced code block.

    We only look at fences whose info string starts with ``python`` (so
    ``python``, ``py``, ``python title="x"`` all qualify; ``pycon``,
    ``python-example`` do not — those are intentionally illustrative).
    """
    in_fence = False
    fence_marker: str | None = None
    fence_is_python = False
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
                fence_is_python = tag in {"python", "py"}
                body = []
                body_start = lineno + 1
            continue

        if fence_marker is not None and stripped.startswith(fence_marker):
            if fence_is_python and body:
                yield body_start, "\n".join(body)
            in_fence = False
            fence_marker = None
            fence_is_python = False
            body = []
            continue

        if fence_is_python:
            body.append(line)


def _is_project_local(module: str) -> bool:
    top = module.split(".", 1)[0]
    return top in PROJECT_PACKAGES


def _module_exists(module: str) -> bool:
    """Check whether a dotted module path resolves to something importable.

    Uses ``importlib.util.find_spec`` so we don't actually execute the
    module body (avoids side effects of database connections, env-var reads,
    etc. that the project's modules perform at import time).
    """
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _analyze_fence(body: str, fence_start_line: int, md_path: Path) -> tuple[list[StaleImport], list[FenceError]]:
    stale: list[StaleImport] = []
    errors: list[FenceError] = []
    try:
        tree = ast.parse(body)
    except SyntaxError as exc:
        # Don't fail the whole run on illustrative snippets that aren't
        # valid Python; surface them as warnings via a second list so the
        # caller can decide what to do.
        errors.append(
            FenceError(
                file=str(md_path.relative_to(REPO_ROOT)),
                line=fence_start_line + (exc.lineno or 1) - 1,
                error=f"SyntaxError: {exc.msg}",
            )
        )
        return stale, errors

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name
                if not _is_project_local(module):
                    continue
                if not _module_exists(module):
                    stale.append(
                        StaleImport(
                            file=str(md_path.relative_to(REPO_ROOT)),
                            line=fence_start_line + node.lineno - 1,
                            module=module,
                            reason="module not found",
                        )
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # Relative imports (``from . import x``) only make sense
                # inside a package; a markdown snippet has no package
                # context, so skip them.
                continue
            module = node.module or ""
            if not module or not _is_project_local(module):
                continue
            if not _module_exists(module):
                stale.append(
                    StaleImport(
                        file=str(md_path.relative_to(REPO_ROOT)),
                        line=fence_start_line + node.lineno - 1,
                        module=module,
                        reason="module not found",
                    )
                )
                continue
            # Module exists — verify each imported name actually lives on it.
            spec = importlib.util.find_spec(module)
            if spec is None or spec.origin is None or spec.origin == "built-in":
                continue
            try:
                source = Path(spec.origin).read_text()
                module_ast = ast.parse(source)
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            exported = _collect_exports(module_ast)
            for alias in node.names:
                if alias.name == "*":
                    continue
                if alias.name not in exported:
                    stale.append(
                        StaleImport(
                            file=str(md_path.relative_to(REPO_ROOT)),
                            line=fence_start_line + node.lineno - 1,
                            module=module,
                            name=alias.name,
                            reason=f"name not defined in {module}",
                        )
                    )
    return stale, errors


def _collect_exports(tree: ast.Module) -> set[str]:
    """Return the set of top-level names defined in a module.

    Includes class/function defs, top-level assignments, and re-exports via
    ``from X import Y`` / ``import X as Y``. Misses dynamic re-exports
    (``__getattr__``, lazy proxies); those produce false positives we accept
    as the cost of static analysis.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


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

    Pre-commit passes every changed markdown file, including ones we want
    to skip (the python.instructions.md style guide, CHANGELOG, …). Without
    this filter the hook would block any commit touching those files.
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


def find_stale_imports(files: list[Path] | None = None) -> tuple[list[StaleImport], list[FenceError]]:
    files = files if files is not None else _iter_markdown_files()
    all_stale: list[StaleImport] = []
    all_errors: list[FenceError] = []
    for path in files:
        try:
            text = path.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for start, body in _iter_python_fences(text):
            stale, errors = _analyze_fence(body, start, path)
            all_stale += stale
            all_errors += errors
    return all_stale, all_errors


def run(files: list[str] | None = None) -> CheckResult:
    target_files = _resolve_hook_files(files) if files else None
    stale, errors = find_stale_imports(target_files)

    # Syntax errors in markdown fences are reported as warnings (in details)
    # but don't fail the check — the whole point is that fences are often
    # snippets, not complete programs.
    if not stale:
        scope = f"{len(target_files)} file(s)" if target_files is not None else "all docs"
        msg = f"No stale project-local imports in {scope}."
        details = [f"Note: {len(errors)} non-runnable fence(s) skipped."] if errors else []
        return CheckResult(passed=True, message=msg, details=details)

    details: list[str] = [f"Found {len(stale)} stale project-local import(s):"]
    last_file: str | None = None
    for ref in sorted(stale, key=lambda r: (r.file, r.line, r.module)):
        if ref.file != last_file:
            details.append(f"  {ref.file}:")
            last_file = ref.file
        what = f"{ref.module}.{ref.name}" if ref.name else ref.module
        details.append(f"    line {ref.line}: `{what}` — {ref.reason}")
    return CheckResult(
        passed=False,
        message=f"{len(stale)} stale project-local import(s) in documentation.",
        details=details,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Detect stale project-local Python imports in markdown.")
    parser.add_argument("files", nargs="*", help="Restrict scan to these files (default: configured globs)")
    args = parser.parse_args()
    result = run(files=args.files or None)
    status = "PASS ✅" if result.passed else "FAIL ❌"
    print(f"[Py Import Audit] {status}: {result.message}")
    for line in result.details:
        print(f"  {line}")
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
