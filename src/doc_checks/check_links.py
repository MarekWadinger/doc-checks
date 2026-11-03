"""Link Audit — every file reference in markdown is a link, and every link resolves.

Scans the configured markdown files (outside code fences and HTML comments) for:

* **Relative links** ``[text](path#anchor)`` and reference definitions ``[id]: path``.
  The target must exist (relative to the document). Anchors are checked too:
  ``#L42`` / ``#L42-L50`` against the target's line count, ``#some-heading``
  against the headings of a markdown target (GitHub slugs).
* **Code spans that name a file or directory**: `` `src/app.py` ``,
  `` `config.yaml` ``, `` `docs/` ``, `` `app.py:42` ``. A span counts as a
  path when it has no whitespace or placeholder characters and either ends in
  ``/`` or has a known extension (``links.extensions``) or a known file name
  (``links.names``).
* **Bare paths** in prose such as ``docs/setup.md`` (at least one ``/`` and a
  known extension).

A reference resolves against the document's directory, the repo root, the
configured ``search_dirs``, and finally by unique basename (or unique path
suffix) among the repo's files. Existence is judged against the files git
knows (tracked plus untracked-but-not-ignored), so a reference to a gitignored
build output fails the same way on every machine; list such outputs in
``ignore_refs``.

Autofix (on by default, like a formatter): a code span or bare path that
resolves to exactly one file is rewritten into a link to it, and a broken link
whose file name now exists exactly once elsewhere is re-pointed there. The
hook then fails with "files were modified" so the result can be reviewed and
staged. Missing, ambiguous and unfixable references fail with a list.

The hook scans every configured markdown file on each run (not only the
changed ones): deleting or moving a code file must fail the docs that point at
it, and those docs are usually not part of the change.

Opt-outs: ``<!-- doc-checks: ignore-links -->`` on a line skips that line;
``exclude_globs`` skips whole files (frozen records, changelogs).

Usage:
    python -m doc_checks.check_links [--check] [files...]
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import posixpath
import re
import subprocess
import sys
from functools import lru_cache
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

from pydantic import BaseModel

from doc_checks import CheckResult, get_config, repo_root

REPO_ROOT = repo_root()

_cfg = get_config().get("links", {})
SCAN_GLOBS: list[str] = _cfg.get("scan_globs", ["**/*.md"])
EXCLUDE_GLOBS: list[str] = _cfg.get("exclude_globs", [])
EXTENSIONS: set[str] = {e.lower().lstrip(".") for e in _cfg.get("extensions", [])}
NAMES: set[str] = set(_cfg.get("names", []))
SEARCH_DIRS: list[str] = [d.strip("/") for d in _cfg.get("search_dirs", [])]
IGNORE_REFS: list[str] = _cfg.get("ignore_refs", [])
AUTOFIX: bool = bool(_cfg.get("autofix", True))
CHECK_ANCHORS: bool = bool(_cfg.get("check_anchors", True))
PRUNE_DIRS: set[str] = set(_cfg.get("prune_dirs", [".git", "node_modules", ".venv", "site-packages", "__pycache__"]))

_HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_IGNORE_LINE = re.compile(r"<!--\s*doc-checks:\s*ignore-links\s*-->")
_SCHEME = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]*://|(?:mailto|tel|data|javascript|file):)", re.IGNORECASE)
_PLACEHOLDER = set("<>*?{}$[]|\\\"'=,;!~^")
_LINE_SUFFIX = re.compile(r"^(?P<path>.+?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)$")
_LINE_ANCHOR = re.compile(r"^L(?P<a>\d+)(?:-L?(?P<b>\d+))?$")
_REF_DEF = re.compile(r"^(?P<lead>\s{0,3}\[[^\]]+\]:\s*)(?P<target><[^>]*>|\S+)")


def _exts_alt() -> str:
    exts = sorted(EXTENSIONS, key=len, reverse=True) or ["md"]
    return "|".join(re.escape(e) for e in exts)


# One pass per line: a link, else a code span, else a bare path (leftmost match wins).
_TOKEN = re.compile(
    r"(?P<link>!?\[(?P<text>(?:[^\[\]]|\[[^\]]*\])*)\]\((?P<target><[^>]*>|[^()\s]+(?:\([^()\s]*\)[^()\s]*)*)"
    r"(?P<title>\s+\"[^\"]*\")?\))"
    r"|(?P<code>(?P<ticks>`+)(?P<span>.+?)(?P=ticks))"
    r"|(?<![\w/.\-\[\]`(<:@#])(?P<bare>(?:\.\.?/)*[\w@+\-]+(?:\.[\w@+\-]+)*(?:/[\w@+\-.]+)+\.(?:"
    + _exts_alt()
    + r"))(?![\w/\-])",
    re.IGNORECASE,
)


class LinkIssue(BaseModel):
    file: str
    line: int
    ref: str
    problem: str


# --------------------------------------------------------------------------- #
# Repository index
# --------------------------------------------------------------------------- #
class _Index(BaseModel):
    files: frozenset[str]
    dirs: frozenset[str]
    by_name: dict[str, list[str]]


def _git_files(root: Path) -> list[str] | None:
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=root,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return [p for p in out.stdout.decode().split("\0") if p]


def _walk_files(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNE_DIRS]
        rel = Path(dirpath).relative_to(root)
        found += [(rel / f).as_posix() for f in filenames]
    return found


@lru_cache(maxsize=4)
def build_index(root: Path | None = None) -> _Index:
    """Files git knows about (that still exist on disk), their directories, and a basename map."""
    root = root or REPO_ROOT
    listed = _git_files(root)
    paths = [p for p in (listed if listed is not None else _walk_files(root)) if (root / p).is_file()]
    files = frozenset(paths)
    dirs: set[str] = set()
    by_name: dict[str, list[str]] = {}
    for p in paths:
        by_name.setdefault(posixpath.basename(p), []).append(p)
        parent = posixpath.dirname(p)
        while parent and parent not in dirs:
            dirs.add(parent)
            by_name.setdefault(posixpath.basename(parent) + "/", []).append(parent)
            parent = posixpath.dirname(parent)
    return _Index(files=files, dirs=frozenset(dirs), by_name=by_name)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _norm(path: str) -> str | None:
    """Repo-relative POSIX path, or None if it leaves the repository."""
    p = posixpath.normpath(path)
    if p in (".", ""):
        return ""
    return None if p == ".." or p.startswith("../") else p


def _exists(rel: str, idx: _Index, want_dir: bool | None) -> bool:
    if want_dir is True:
        return rel in idx.dirs or rel == ""
    if want_dir is False:
        return rel in idx.files
    return rel in idx.files or rel in idx.dirs or rel == ""


def _ignored(ref: str, rel: str | None = None) -> bool:
    cands = [ref, ref.rstrip("/")] + ([rel] if rel else [])
    return any(fnmatch.fnmatch(c, pat) for c in cands for pat in IGNORE_REFS)


def resolve_ref(ref: str, doc_dir: str, idx: _Index) -> tuple[str | None, str]:
    """Resolve a written file/dir reference. Returns (repo path, status: ok|missing|ambiguous|outside)."""
    want_dir = True if ref.endswith("/") else None
    clean = ref.rstrip("/")
    if clean.startswith("/"):
        bases, clean = [""], clean.lstrip("/")
    else:
        bases = [doc_dir, "", *SEARCH_DIRS]
    outside = False
    for base in bases:
        rel = _norm(posixpath.join(base, clean))
        if rel is None:
            outside = True
            continue
        if _exists(rel, idx, want_dir):
            return rel, "ok"
    if "/" not in clean:
        hits = idx.by_name.get(clean + "/", []) if want_dir else idx.by_name.get(clean, [])
        if want_dir is None and not hits:
            hits = idx.by_name.get(clean + "/", [])
    else:
        tail = "/" + clean.lstrip("./")
        pool = idx.dirs if want_dir else (idx.files | idx.dirs if want_dir is None else idx.files)
        hits = sorted(p for p in pool if ("/" + p).endswith(tail))
    if len(hits) == 1:
        return hits[0], "ok"
    if len(hits) > 1:
        return None, "ambiguous"
    return None, "outside" if outside else "missing"


def _rel_link(target: str, doc_dir: str, is_dir: bool) -> str:
    rel = posixpath.relpath(target or ".", doc_dir or ".")
    return rel + "/" if is_dir and not rel.endswith("/") else rel


# --------------------------------------------------------------------------- #
# Anchors
# --------------------------------------------------------------------------- #
def _slug(heading: str) -> str:
    """GitHub-style heading anchor."""
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", heading)  # links -> their text
    text = re.sub(r"<[^>]+>", "", text).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


@lru_cache(maxsize=512)
def _anchors(path: Path) -> tuple[frozenset[str], int]:
    """(heading slugs, line count) of a file."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return frozenset(), 0
    lines = text.splitlines()
    slugs: set[str] = set()
    seen: dict[str, int] = {}
    fence = None
    for line in lines:
        s = line.lstrip()
        if s.startswith(("```", "~~~")):
            fence = None if fence and s.startswith(fence) else (fence or s[:3])
            continue
        if fence:
            continue
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if m:
            base = _slug(m.group(1))
            n = seen.get(base, 0)
            seen[base] = n + 1
            slugs.add(base if n == 0 else f"{base}-{n}")
        for a in re.findall(r"<a\s+(?:name|id)=\"([^\"]+)\"", line):
            slugs.add(a)
    return frozenset(slugs), len(lines)


def _anchor_problem(rel: str, fragment: str, root: Path) -> str | None:
    if not CHECK_ANCHORS or not fragment:
        return None
    _, n_lines = _anchors(root / rel)
    m = _LINE_ANCHOR.match(fragment)
    if m:
        last = int(m.group("b") or m.group("a"))
        return None if last <= n_lines else f"line anchor #{fragment} past the end ({n_lines} lines)"
    if rel.lower().endswith((".md", ".markdown")):
        slugs, _ = _anchors(root / rel)
        return None if unquote(fragment).lower() in slugs else f"no heading for anchor #{fragment}"
    return None


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def _is_path_span(span: str) -> bool:
    s = span.strip()
    if not s or any(ch.isspace() for ch in s) or _SCHEME.match(s) or s.startswith(("-", "~", "@", "/")):
        return False
    m = _LINE_SUFFIX.match(s)
    core = m.group("path") if m else s
    if any(ch in _PLACEHOLDER for ch in core) or core in (".", "..", "/") or "..." in core or "…" in core:
        return False
    if not re.search(r"[A-Za-z]", core):
        return False
    if core.endswith("/"):
        return True
    base = posixpath.basename(core)
    if base in NAMES:
        return True
    ext = base.rsplit(".", 1)[-1].lower() if "." in base.strip(".") else ""
    return ext in EXTENSIONS


def _check_link(target: str, doc_dir: str, idx: _Index, root: Path) -> tuple[str | None, str | None]:
    """(new target or None, problem or None) for a link target."""
    raw = target[1:-1] if target.startswith("<") and target.endswith(">") else target
    if not raw or _SCHEME.match(raw) or raw.startswith("//"):
        return None, None
    path, _, fragment = raw.partition("#")
    if not path:  # same-document anchor: checked by the caller with the doc itself
        return None, None
    path = unquote(path)
    rel = _norm(path.lstrip("/") if path.startswith("/") else posixpath.join(doc_dir, path))
    if rel is not None and _exists(rel, idx, True if path.endswith("/") else None):
        return None, _anchor_problem(rel, fragment, root) if rel in idx.files else None
    if _ignored(path, rel):
        return None, None
    # Self-heal: the same file name exists exactly once elsewhere.
    name = posixpath.basename(path.rstrip("/"))
    hits = idx.by_name.get(name + ("/" if path.endswith("/") else ""), [])
    if len(hits) == 1:
        new = _rel_link(hits[0], doc_dir, path.endswith("/"))
        return new + (f"#{fragment}" if fragment else ""), None
    where = "outside the repository" if rel is None else "not found"
    return None, f"link target {where}" + (f" ({len(hits)} files share its name)" if len(hits) > 1 else "")


def process_text(text: str, doc_rel: str, idx: _Index, root: Path | None = None) -> tuple[str, list[LinkIssue]]:
    """Return (fixed text, problems) for one markdown document."""
    root = root or REPO_ROOT
    doc_dir = posixpath.dirname(doc_rel)
    issues: list[LinkIssue] = []
    out: list[str] = []
    fence: str | None = None
    in_comment = False

    def issue(lineno: int, ref: str, problem: str) -> None:
        issues.append(LinkIssue(file=doc_rel, line=lineno, ref=ref, problem=problem))

    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        stripped = line.lstrip()
        if fence is not None:
            if stripped.startswith(fence):
                fence = None
            out.append(line)
            continue
        if stripped.startswith(("```", "~~~")):
            fence = stripped[:3]
            out.append(line)
            continue
        if in_comment:
            if "-->" in line:
                in_comment = False
            out.append(line)
            continue
        if _IGNORE_LINE.search(line):
            out.append(line)
            continue
        if "<!--" in line and "-->" not in line.split("<!--", 1)[1]:
            in_comment = True
            line_body, tail = line.split("<!--", 1)
            tail = "<!--" + tail
        else:
            line_body, tail = line, ""
        line_body = re.sub(r"<!--.*?-->", lambda m: "\0" * len(m.group(0)), line_body)
        # HTML tags are not prose: an attribute such as src="a.png" is neither a reference nor rewritten.
        line_body = _HTML_TAG.sub(lambda m: "\0" * len(m.group(0)), line_body)
        original = line if not tail else line[: len(line) - len(tail)]

        m_def = _REF_DEF.match(line_body)
        if m_def:
            new_target, problem = _check_link(m_def.group("target"), doc_dir, idx, root)
            if problem:
                issue(lineno, m_def.group("target"), problem)
            elif new_target and AUTOFIX:
                s, e = m_def.span("target")
                original = original[:s] + new_target + original[e:]
            out.append(original + tail)
            continue

        pieces: list[str] = []
        pos = 0
        for m in _TOKEN.finditer(line_body):

            def grp(name: str, m: re.Match[str] = m) -> str:
                return original[m.start(name) : m.end(name)]

            pieces.append(original[pos : m.start()])
            chunk = original[m.start() : m.end()]
            if m.group("link"):
                target = grp("target")
                if target.startswith("#"):
                    slugs, _ = _anchors(root / doc_rel) if (root / doc_rel).exists() else (frozenset(), 0)
                    frag = target[1:]
                    if CHECK_ANCHORS and frag and not _LINE_ANCHOR.match(frag) and unquote(frag).lower() not in slugs:
                        issue(lineno, target, f"no heading for anchor {target} in this document")
                else:
                    new_target, problem = _check_link(target, doc_dir, idx, root)
                    if problem:
                        issue(lineno, target, problem)
                    elif new_target and AUTOFIX:
                        s, e = m.start("target") - m.start(), m.end("target") - m.start()
                        chunk = chunk[:s] + new_target + chunk[e:]
            else:
                ref = grp("span") if m.group("code") else grp("bare")
                if m.group("code") and not _is_path_span(ref):
                    pieces.append(chunk)
                    pos = m.end()
                    continue
                ref = ref.strip()
                ls = _LINE_SUFFIX.match(ref)
                core = ls.group("path") if ls and m.group("code") else ref
                anchor = ""
                if ls and m.group("code"):
                    anchor = f"#L{ls.group('a')}" + (f"-L{ls.group('b')}" if ls.group("b") else "")
                if _ignored(core):
                    pieces.append(chunk)
                    pos = m.end()
                    continue
                target, status = resolve_ref(core, doc_dir, idx)
                if _ignored(core, target):
                    pass
                elif status == "ok" and target is not None:
                    if AUTOFIX:
                        is_dir = core.endswith("/") or (target in idx.dirs and target not in idx.files)
                        chunk = f"[{chunk}]({_rel_link(target, doc_dir, is_dir)}{anchor})"
                else:
                    issue(lineno, ref, {"missing": "not found", "ambiguous": "ambiguous: several files match, write the path", "outside": "outside the repository"}[status])
            pieces.append(chunk)
            pos = m.end()
        pieces.append(original[pos:])
        out.append("".join(pieces) + tail)
    return "".join(out), issues


# --------------------------------------------------------------------------- #
# Hook plumbing
# --------------------------------------------------------------------------- #
def _iter_markdown_files() -> list[Path]:
    """Configured markdown files that git knows about (gitignored downloads and vendored docs are skipped)."""
    out: list[Path] = []
    for rel in sorted(build_index(REPO_ROOT).files):
        pure = PurePosixPath(rel)
        if not any(pure.full_match(g) for g in SCAN_GLOBS):
            continue
        if any(part in PRUNE_DIRS for part in pure.parts) or any(pure.full_match(g) for g in EXCLUDE_GLOBS):
            continue
        out.append(REPO_ROOT / rel)
    return out


def _resolve_files(arg_files: list[str]) -> list[Path]:
    out: list[Path] = []
    for raw in arg_files:
        path = (REPO_ROOT / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        if not path.is_file() or path.suffix.lower() not in {".md", ".markdown"}:
            continue
        try:
            rel = path.relative_to(REPO_ROOT)
        except ValueError:
            continue
        if any(rel.full_match(pat) for pat in EXCLUDE_GLOBS):
            continue
        out.append(path)
    return out


def run(files: list[str] | None = None, fix: bool | None = None) -> CheckResult:
    fix = AUTOFIX if fix is None else fix
    targets = _resolve_files(files) if files else _iter_markdown_files()
    idx = build_index(REPO_ROOT)
    issues: list[LinkIssue] = []
    changed: list[str] = []
    for path in targets:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(REPO_ROOT).as_posix()
        new, found = process_text(text, rel, idx)
        issues += found
        if fix and new != text:
            path.write_text(new, encoding="utf-8")
            changed.append(rel)

    if not issues and not changed:
        return CheckResult(passed=True, message=f"All file references are links that resolve in {len(targets)} file(s).")
    details: list[str] = []
    if changed:
        details.append(f"Linked or re-pointed references in {len(changed)} file(s) (review and stage):")
        details += [f"  {c}" for c in changed]
    if issues:
        details.append(f"Found {len(issues)} broken or unresolvable reference(s):")
        last = None
        for i in sorted(issues, key=lambda x: (x.file, x.line, x.ref)):
            if i.file != last:
                details.append(f"  {i.file}:")
                last = i.file
            details.append(f"    line {i.line}: `{i.ref}` — {i.problem}")
        details += [
            "",
            "Fix: point the reference at the file's current path, remove it if the file is gone,",
            "or list deliberate non-files (build outputs, external paths) in links.ignore_refs.",
        ]
    msg = ", ".join(
        s
        for s in (
            f"{len(issues)} broken reference(s)" if issues else "",
            f"{len(changed)} file(s) rewritten" if changed else "",
        )
        if s
    )
    return CheckResult(passed=False, message=msg + ".", details=details)


def main() -> None:
    parser = argparse.ArgumentParser(description="Link every file reference in markdown and check that links resolve.")
    parser.add_argument("files", nargs="*", help="Restrict to these files (default: every configured markdown file)")
    parser.add_argument("--check", action="store_true", help="Report only; do not rewrite files")
    args = parser.parse_args()
    result = run(files=args.files or None, fix=False if args.check else None)
    status = "PASS ✅" if result.passed else "FAIL ❌"
    print(f"[Link Audit] {status}: {result.message}")
    for line in result.details:
        print(f"  {line}")
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
