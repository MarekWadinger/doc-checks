"""Tests for the code ref audit (``doc_checks.check_code_refs``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_checks import check_code_refs as cr
from doc_checks import check_links as cl

PY = '''"""Module."""

RETENTION_DAYS = 30


def process(x):
    return x


class Validator:
    limit: int = 3

    @property
    def name(self):
        return "v"

    async def run(self):
        pass
'''

GO = """package lb

type Server struct{}

func (s *Server) Pick() int {
	return 1
}

func Run() {}
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    for rel, text in {"src/app/core.py": PY, "lb/server.go": GO, "docs/.keep": ""}.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    cl.build_index.cache_clear()
    cr._python_symbols.cache_clear()
    cr._lines.cache_clear()
    return tmp_path


def run(repo: Path, text: str, doc: str = "docs/page.md") -> tuple[str, list[cr.CodeRefIssue]]:
    return cr.process_text(text, repo / doc, repo)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # stale anchor moves to the definition
        ("[core.py::process](../src/app/core.py#L1)", "[core.py::process](../src/app/core.py#L6)"),
        # missing anchor is added; backticked label, nested method, decorated method
        ("[`core.py::Validator.run`](../src/app/core.py)", "[`core.py::Validator.run`](../src/app/core.py#L17)"),
        ("[core.py::Validator.name](../src/app/core.py#L99)", "[core.py::Validator.name](../src/app/core.py#L14)"),
        # module and class constants; trailing () in the label
        ("[core.py::RETENTION_DAYS](../src/app/core.py#L9)", "[core.py::RETENTION_DAYS](../src/app/core.py#L3)"),
        ("[core.py::Validator.limit](../src/app/core.py#L1)", "[core.py::Validator.limit](../src/app/core.py#L11)"),
        ("[core.py::process()](../src/app/core.py#L2)", "[core.py::process()](../src/app/core.py#L6)"),
        # a range keeps its length
        ("[core.py::process](../src/app/core.py#L1-L2)", "[core.py::process](../src/app/core.py#L6-L7)"),
        # non-Python: generic definition search, Go method on a type
        ("[server.go::Run](../lb/server.go#L1)", "[server.go::Run](../lb/server.go#L9)"),
        ("[server.go::Server.Pick](../lb/server.go)", "[server.go::Server.Pick](../lb/server.go#L5)"),
        # an unlinked span becomes a link, path resolved like doc-check-links does
        ("See `core.py::process`.", "See [`core.py::process`](../src/app/core.py#L6)."),
    ],
)
def test_symbol_links_point_at_the_definition(repo: Path, text: str, expected: str) -> None:
    out, issues = run(repo, text + "\n")
    assert issues == []
    assert out == expected + "\n"


def test_correct_anchor_is_left_alone(repo: Path) -> None:
    text = "[core.py::process](../src/app/core.py#L6)\n"
    assert run(repo, text) == (text, [])


@pytest.mark.parametrize(
    "text",
    [
        "Plain [link](../src/app/core.py#L3) without a symbol.\n",  # line-only links are doc-check-links' job
        "```\n[core.py::process](../src/app/core.py#L1)\n```\n",  # code fence
        "[core.py::process](../src/app/core.py#L1) <!-- doc-checks: ignore-code-refs -->\n",
        "[gone.py::f](../src/app/gone.py#L1)\n",  # missing file: reported by doc-check-links
    ],
)
def test_untouched(repo: Path, text: str) -> None:
    assert run(repo, text) == (text, [])


def test_missing_symbol_fails_and_keeps_the_text(repo: Path) -> None:
    text = "[core.py::renamed](../src/app/core.py#L6)\n"
    out, issues = run(repo, text)
    assert out == text
    assert [i.problem for i in issues] == ["symbol not found in core.py"]


def test_symbol_found_twice_in_non_python_fails(repo: Path) -> None:
    (repo / "lb/other.go").write_text("func Run() {}\nfunc Run() {}\n")
    out, issues = run(repo, "[other.go::Run](../lb/other.go#L1)\n")
    assert issues and "more than once" in issues[0].problem


def test_run_rewrites_and_fails_like_a_formatter(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    doc = repo / "docs/page.md"
    doc.write_text("[core.py::process](../src/app/core.py#L1)\n")
    monkeypatch.setattr(cl, "REPO_ROOT", repo)
    result = cr.run(files=[str(doc)])
    assert not result.passed and "rewritten" in result.message
    assert doc.read_text() == "[core.py::process](../src/app/core.py#L6)\n"
    assert cr.run(files=[str(doc)]).passed
