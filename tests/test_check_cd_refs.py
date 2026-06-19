"""Tests for the ``cd`` reference audit (``doc_checks.check_cd_refs``)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from doc_checks import check_cd_refs as cd


# --------------------------------------------------------------------------- #
# _clone_dirs
# --------------------------------------------------------------------------- #
def test_clone_dirs_url_basename() -> None:
    assert cd._clone_dirs("git clone https://x/y.git") == ({"y"}, False)


def test_clone_dirs_explicit_target() -> None:
    assert cd._clone_dirs("git clone https://x/foo.git bar") == ({"bar"}, False)


def test_clone_dirs_placeholder_url_unresolved() -> None:  # issue #7
    assert cd._clone_dirs("git clone <repository-url>") == (set(), True)


def test_clone_dirs_quoted_var_target_unresolved() -> None:  # issue #5
    assert cd._clone_dirs('git clone <url> "$PROJECT"') == (set(), True)


# --------------------------------------------------------------------------- #
# find_stale_cd_refs
# --------------------------------------------------------------------------- #
@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(cd, "REPO_ROOT", tmp_path)
    return tmp_path


def _write(repo: Path, body: str) -> Path:
    p = repo / "README.md"
    p.write_text(body)
    return p


def test_quoted_dynamic_cd_skipped(repo: Path) -> None:  # issue #5
    md = textwrap.dedent(
        """\
        ```bash
        PROJECT=my-new-project
        git clone <url> "$PROJECT"
        cd "$PROJECT"
        ```
        """
    )
    assert cd.find_stale_cd_refs([_write(repo, md)]) == []


def test_placeholder_clone_then_cd_skipped(repo: Path) -> None:  # issue #7
    md = textwrap.dedent(
        """\
        ```bash
        git clone <repository-url>
        cd llm-loadbalancer
        ```
        """
    )
    assert cd.find_stale_cd_refs([_write(repo, md)]) == []


def test_genuinely_stale_cd_still_flagged(repo: Path) -> None:
    p = _write(repo, "```bash\ncd does-not-exist-xyz\n```\n")
    stale = cd.find_stale_cd_refs([p])
    assert len(stale) == 1
    assert stale[0].path == "does-not-exist-xyz"


def test_real_dir_passes(repo: Path) -> None:
    (repo / "src").mkdir()
    assert cd.find_stale_cd_refs([_write(repo, "```bash\ncd src\n```\n")]) == []
