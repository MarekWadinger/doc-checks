"""Tests for the link audit (``doc_checks.check_links``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_checks import check_links as cl


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small repo: docs, code, a nested note and two files sharing a name."""
    files = {
        "README.md": "# Project\n\n## Setup steps\n",
        "src/pkg/core.py": "\n".join(f"x{i} = {i}" for i in range(50)) + "\n",
        "src/pkg/__init__.py": "",
        "tests/__init__.py": "",
        "docs/guide.md": "# Guide\n\n## Install it\n\n## Install it\n",
        "docs/notes/deep.md": "# Deep\n",
        "Makefile": "all:\n",
        "data/table.csv": "a,b\n",
    }
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    cl.build_index.cache_clear()
    cl._anchors.cache_clear()
    return tmp_path


def run(repo: Path, text: str, doc: str = "docs/page.md") -> tuple[str, list[cl.LinkIssue]]:
    return cl.process_text(text, doc, cl.build_index(repo), repo)


# --------------------------------------------------------------------------- #
# Code spans and bare paths become links
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("See `src/pkg/core.py`.", "See [`src/pkg/core.py`](../src/pkg/core.py)."),
        ("See `core.py`.", "See [`core.py`](../src/pkg/core.py)."),  # unique basename
        ("See `pkg/core.py`.", "See [`pkg/core.py`](../src/pkg/core.py)."),  # unique path suffix
        ("See `guide.md`.", "See [`guide.md`](guide.md)."),  # document's own directory first
        ("See `notes/`.", "See [`notes/`](notes/)."),  # directory
        ("Run `Makefile`.", "Run [`Makefile`](../Makefile)."),  # known file name
        ("Line `core.py:42`.", "Line [`core.py:42`](../src/pkg/core.py#L42)."),  # line suffix -> anchor
        ("Bare docs/guide.md here.", "Bare [docs/guide.md](guide.md) here."),
    ],
)
def test_references_are_linked(repo: Path, text: str, expected: str) -> None:
    out, issues = run(repo, text + "\n")
    assert issues == []
    assert out == expected + "\n"


@pytest.mark.parametrize(
    "text",
    [
        "`make test`",  # whitespace
        "`pkg.core.run`",  # dotted name, not a file
        "`*.py`",  # glob
        "`results/<run>/summary.csv`",  # placeholder
        "`--flag`",  # option
        "`3.13`",  # number
        "https://example.org/a/b.md",  # URL
        "[already](../README.md)",  # link to an existing file
        '<img src="../data/table.csv" width="1">',  # HTML attribute, not prose
        "`/home/`",  # absolute system path
    ],
)
def test_non_references_are_left_alone(repo: Path, text: str) -> None:
    out, issues = run(repo, text + "\n")
    assert issues == []
    assert out == text + "\n"


def test_code_fences_and_comments_are_skipped(repo: Path) -> None:
    text = "```\n`gone.py`\n```\n<!-- `gone.py` -->\n<!--\n`gone.py`\n-->\n`gone.py` <!-- doc-checks: ignore-links -->\n"
    out, issues = run(repo, text)
    assert issues == []
    assert out == text


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #
def test_missing_reference_fails(repo: Path) -> None:
    _, issues = run(repo, "See `experiments/gone.py`.\n")
    assert [(i.ref, i.problem) for i in issues] == [("experiments/gone.py", "not found")]


def test_ambiguous_reference_fails(repo: Path) -> None:
    _, issues = run(repo, "See `__init__.py`.\n")
    assert issues and issues[0].problem.startswith("ambiguous")


def test_broken_link_fails(repo: Path) -> None:
    _, issues = run(repo, "[x](../experiments/gone.py)\n")
    assert issues[0].problem == "link target not found"


def test_moved_file_link_is_repointed(repo: Path) -> None:
    out, issues = run(repo, "[core](../lib/core.py#L3)\n")
    assert issues == []
    assert out == "[core](../src/pkg/core.py#L3)\n"


def test_reference_definition_is_checked(repo: Path) -> None:
    _, issues = run(repo, "[ref]: ../nowhere.md\n")
    assert issues[0].ref == "../nowhere.md"


# --------------------------------------------------------------------------- #
# Anchors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("[a](../src/pkg/core.py#L50)", True),
        ("[a](../src/pkg/core.py#L10-L20)", True),
        ("[a](../src/pkg/core.py#L51)", False),
        ("[a](../README.md#setup-steps)", True),
        ("[a](guide.md#install-it-1)", True),  # duplicate heading gets a suffix
        ("[a](../README.md#nope)", False),
    ],
)
def test_anchors(repo: Path, text: str, ok: bool) -> None:
    _, issues = run(repo, text + "\n")
    assert (issues == []) is ok


def test_same_document_anchor(repo: Path) -> None:
    _, issues = run(repo, "[a](#install-it)\n[b](#missing)\n", doc="docs/guide.md")
    assert [i.ref for i in issues] == ["#missing"]


def test_ignore_refs(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cl, "IGNORE_REFS", ["build/**"])
    _, issues = run(repo, "Output `build/out.pdf`.\n")
    assert issues == []


def test_check_mode_does_not_rewrite(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cl, "AUTOFIX", False)
    out, issues = run(repo, "See `core.py`.\n")
    assert issues == []
    assert out == "See `core.py`.\n"


def test_placeholder_span_next_to_html_is_left_alone(repo: Path) -> None:
    text = "<b>Runs</b> live in `results/<exp>/<run>/result.json`.\n"
    out, issues = run(repo, text)
    assert issues == []
    assert out == text
