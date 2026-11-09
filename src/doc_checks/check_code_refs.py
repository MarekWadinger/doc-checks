"""Code Ref Audit — links to a symbol keep pointing at it when the code moves.

A link whose label names a symbol after ``::`` carries a line anchor that the
check keeps in sync with where the symbol is defined now::

    [validate.py::process_validation](../src/validator/validate.py#L33)
    [`client.py::Validator.run`](src/client.py#L120-L140)

* The symbol is found by definition: Python files are parsed (``ast``), so
  ``Class.method`` and module or class constants resolve exactly; other files
  are searched for a ``def``/``class``/``func``/``function``/``fn``/``type``/
  ``struct``/``interface``/``const``/``let``/``var`` line naming the last part
  (``Type.Method`` also matches a Go method on ``Type``).
* A ``#L`` anchor that no longer points at the definition is moved there (a
  range keeps its length); a link with no anchor gets one.
* An unlinked code span `` `path/to/file.py::symbol` `` becomes such a link,
  its path resolved the way ``doc-check-links`` resolves file references.
* A symbol that is gone, or defined more than once, fails with a list.

Line-only references (`` `file.py:42` ``) stay ``doc-check-links``' business:
they cannot heal, because a bare line number says nothing about what it meant.
Missing link targets are reported by ``doc-check-links`` too, not here.

Autofix is on by default, as in ``doc-check-links``; the hook then fails with
"files were modified" so the result can be reviewed and staged. Scans the same
markdown files as ``doc-check-links`` (``links.scan_globs`` /
``links.exclude_globs``). Opt-out per line: ``<!-- doc-checks: ignore-code-refs -->``.

Usage:
    python -m doc_checks.check_code_refs [--check] [files...]
"""

from __future__ import annotations

import argparse
import ast
import posixpath
import re
import sys
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote

from pydantic import BaseModel

from doc_checks import CheckResult, get_config
from doc_checks import check_links as cl

_cfg = get_config().get("code_refs", {})
AUTOFIX: bool = bool(_cfg.get("autofix", True))

_IGNORE_LINE = re.compile(r"<!--\s*doc-checks:\s*ignore-code-refs\s*-->")
_SYMBOL = r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*"
# [label containing path::Symbol](target#fragment)
_LINK = re.compile(
    r"\[(?P<label>[^\[\]]*?::\s*(?P<sym>" + _SYMBOL + r")(?:\(\))?`?)\]"
    r"\((?P<target>[^()\s#]+)(?:#(?P<frag>[^()\s]*))?\)"
)
# `path/to/file.ext::Symbol` not already inside a link
_SPAN = re.compile(r"`(?P<path>[^`\s:]+\.\w+)::(?P<sym>" + _SYMBOL + r")(?:\(\))?`")
_LINE_FRAG = re.compile(r"^L(?P<a>\d+)(?:-L?(?P<b>\d+))?$")
_DEF_WORDS = r"(?:export\s+)?(?:default\s+)?(?:pub(?:\(\w+\))?\s+)?(?:async\s+)?(?:def|class|func|function|fn|type|struct|interface|const|let|var)"


class CodeRefIssue(BaseModel):
    file: str
    line: int
    ref: str
    problem: str


# --------------------------------------------------------------------------- #
# Finding a symbol's definition
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=256)
def _python_symbols(path: Path) -> dict[str, list[int]] | None:
    """Qualified name -> definition lines, for functions, classes and module/class-level assignments."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, SyntaxError):
        return None
    found: dict[str, list[int]] = {}

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            names: list[str] = []
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [node.name]
            elif isinstance(node, ast.Assign):
                names = [t.id for t in node.targets if isinstance(t, ast.Name)]
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names = [node.target.id]
            for name in names:
                found.setdefault(prefix + name, []).append(node.lineno)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(node.body, f"{prefix}{node.name}.")

    visit(tree.body, "")
    return found


@lru_cache(maxsize=256)
def _lines(path: Path) -> tuple[str, ...]:
    try:
        return tuple(path.read_text(encoding="utf-8").splitlines())
    except (OSError, UnicodeDecodeError):
        return ()


def find_symbol(path: Path, symbol: str) -> tuple[int | None, str]:
    """(definition line, status: ok|missing|ambiguous)."""
    if path.suffix in (".py", ".pyi"):
        symbols = _python_symbols(path)
        if symbols is not None:
            hits = symbols.get(symbol, [])
            # A property setter or an overload repeats the name: the first definition is the one to show.
            return (hits[0], "ok") if hits else (None, "missing")
    *parents, name = symbol.split(".")
    pattern = re.compile(
        r"^\s*(?:" + _DEF_WORDS + r"\s+(?:\([^)]*\)\s*)?" + re.escape(name) + r"\b"
        r"|" + re.escape(name) + r"\s*[:=]\s*(?:async\s+)?(?:function\b|\())"
    )
    hits = [i for i, text in enumerate(_lines(path), start=1) if pattern.search(text)]
    if parents and len(hits) > 1:
        hits = [i for i in hits if parents[-1] in _lines(path)[i - 1]] or hits
    if not hits:
        return None, "missing"
    return (hits[0], "ok") if len(hits) == 1 else (None, "ambiguous")


def _moved_fragment(frag: str | None, line: int) -> str:
    m = _LINE_FRAG.match(frag or "")
    if m and m.group("b"):
        return f"L{line}-L{line + int(m.group('b')) - int(m.group('a'))}"
    return f"L{line}"


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def process_text(text: str, doc_path: Path, root: Path | None = None) -> tuple[str, list[CodeRefIssue]]:
    """Return (fixed text, problems) for one markdown document at ``doc_path``."""
    root = root or cl.REPO_ROOT
    try:
        doc_rel = doc_path.relative_to(root).as_posix()
    except ValueError:
        doc_rel = doc_path.name
    doc_dir = posixpath.dirname(doc_rel)
    issues: list[CodeRefIssue] = []
    out: list[str] = []
    fence: str | None = None

    def locate(target: Path, sym: str, lineno: int, ref: str) -> int | None:
        line, status = find_symbol(target, sym)
        if status != "ok":
            problem = "symbol not found" if status == "missing" else "symbol defined more than once, qualify it"
            issues.append(CodeRefIssue(file=doc_rel, line=lineno, ref=ref, problem=f"{problem} in {target.name}"))
        return line

    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        stripped = line.lstrip()
        if fence is not None:
            fence = None if stripped.startswith(fence) else fence
            out.append(line)
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            out.append(line)
            continue
        if _IGNORE_LINE.search(line) or "::" not in line:
            out.append(line)
            continue

        def fix_link(m: re.Match[str]) -> str:
            raw = unquote(m.group("target"))
            if cl._SCHEME.match(raw):
                return m.group(0)
            target = (doc_path.parent / raw).resolve()
            if not target.is_file():
                return m.group(0)  # doc-check-links reports missing targets
            found = locate(target, m.group("sym"), lineno, m.group(0))
            if found is None:
                return m.group(0)
            frag = _moved_fragment(m.group("frag"), found)
            return f"[{m.group('label')}]({m.group('target')}#{frag})"

        line = _LINK.sub(fix_link, line)
        masked = _LINK.sub(lambda m: "\0" * len(m.group(0)), line)

        pieces: list[str] = []
        pos = 0
        for m in _SPAN.finditer(masked):
            pieces.append(line[pos : m.start()])
            pos = m.end()
            chunk = line[m.start() : m.end()]
            rel, status = cl.resolve_ref(m.group("path"), doc_dir, cl.build_index(root))
            if status != "ok" or rel is None:
                pieces.append(chunk)  # doc-check-links reports unresolvable paths
                continue
            found = locate(root / rel, m.group("sym"), lineno, chunk)
            if found is None:
                pieces.append(chunk)
                continue
            pieces.append(f"[{chunk}]({cl._rel_link(rel, doc_dir, False)}#L{found})")
        pieces.append(line[pos:])
        out.append("".join(pieces))
    return "".join(out), issues


# --------------------------------------------------------------------------- #
# Hook plumbing
# --------------------------------------------------------------------------- #
def run(files: list[str] | None = None, fix: bool | None = None) -> CheckResult:
    fix = AUTOFIX if fix is None else fix
    targets = cl._resolve_files(files) if files else cl._iter_markdown_files()
    issues: list[CodeRefIssue] = []
    changed: list[str] = []
    for path in targets:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        new, found = process_text(text, path)
        issues += found
        if fix and new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(path.relative_to(cl.REPO_ROOT).as_posix())

    if not issues and not changed:
        return CheckResult(passed=True, message=f"All symbol links point at their definitions in {len(targets)} file(s).")
    details: list[str] = []
    if changed:
        details.append(f"Moved line anchors to the current definitions in {len(changed)} file(s) (review and stage):")
        details += [f"  {c}" for c in changed]
    if issues:
        details.append(f"Found {len(issues)} symbol reference(s) that cannot be resolved:")
        for i in sorted(issues, key=lambda x: (x.file, x.line)):
            details.append(f"  {i.file}:{i.line}: {i.ref} — {i.problem}")
        details += ["", "Fix: rename the reference to the symbol's current name, or remove it if the code is gone."]
    msg = ", ".join(
        s
        for s in (
            f"{len(issues)} unresolved symbol reference(s)" if issues else "",
            f"{len(changed)} file(s) rewritten" if changed else "",
        )
        if s
    )
    return CheckResult(passed=False, message=msg + ".", details=details)


def main() -> None:
    parser = argparse.ArgumentParser(description="Keep line anchors of symbol links on the symbol's definition.")
    parser.add_argument("files", nargs="*", help="Restrict to these files (default: every configured markdown file)")
    parser.add_argument("--check", action="store_true", help="Report only; do not rewrite files")
    args = parser.parse_args()
    result = run(files=args.files or None, fix=False if args.check else None)
    status = "PASS ✅" if result.passed else "FAIL ❌"
    print(f"[Code Ref Audit] {status}: {result.message}")
    for line in result.details:
        print(f"  {line}")
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
