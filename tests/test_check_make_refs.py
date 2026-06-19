"""Tests for the recursive Makefile discovery in ``doc_checks.check_make_refs``."""

from __future__ import annotations

from pathlib import Path

import pytest

from doc_checks import check_make_refs as mr


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(mr, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(mr, "MAKEFILE_PATHS", ["Makefile"])
    monkeypatch.setattr(mr, "EXCLUDE_GLOBS", [])
    monkeypatch.setattr(mr, "IGNORE_TARGETS", set())
    return tmp_path


def _mk(repo: Path, rel: str, target: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{target}:\n\techo {target}\n")


def _doc(repo: Path, rel: str, target: str) -> Path:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"```bash\nmake {target}\n```\n")
    return path


def test_subproject_target_recognized(repo: Path) -> None:  # feature
    _mk(repo, "Makefile", "install")
    _mk(repo, "server/Makefile", "serve")
    doc = _doc(repo, "server/README.md", "serve")
    stale, _ = mr.find_stale_refs([doc])
    assert stale == []


def test_subsubdir_target_recognized(repo: Path) -> None:  # feature
    _mk(repo, "Makefile", "install")
    _mk(repo, "server/api/Makefile", "run")
    doc = _doc(repo, "server/README.md", "run")
    stale, _ = mr.find_stale_refs([doc])
    assert stale == []


def test_sibling_target_out_of_scope_flagged(repo: Path) -> None:
    _mk(repo, "Makefile", "install")
    _mk(repo, "a/Makefile", "atarget")
    doc = _doc(repo, "b/README.md", "atarget")
    stale, _ = mr.find_stale_refs([doc])
    assert len(stale) == 1
    assert stale[0].target == "atarget"


def test_root_doc_sees_subproject_targets(repo: Path) -> None:
    _mk(repo, "Makefile", "install")
    _mk(repo, "server/Makefile", "serve")
    doc = _doc(repo, "README.md", "serve")
    stale, _ = mr.find_stale_refs([doc])
    assert stale == []


def test_root_target_available_in_subproject(repo: Path) -> None:
    _mk(repo, "Makefile", "install")
    _mk(repo, "server/Makefile", "serve")
    doc = _doc(repo, "server/README.md", "install")
    stale, _ = mr.find_stale_refs([doc])
    assert stale == []
