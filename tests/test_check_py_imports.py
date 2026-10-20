"""Tests for the Python import audit (``doc_checks.check_py_imports``)."""

from __future__ import annotations

import importlib
import textwrap
from pathlib import Path

import pytest

from doc_checks import check_py_imports as cpi


@pytest.fixture
def src_layout(tmp_path: Path) -> Path:
    """A src-layout repo: ``src/dcfixturepkg/core.py`` defining ``helper``."""
    pkg = tmp_path / "src" / "dcfixturepkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "core.py").write_text("def helper():\n    return 1\n")
    return tmp_path


# --------------------------------------------------------------------------- #
# _source_paths / _detect_src_packages
# --------------------------------------------------------------------------- #
def test_source_paths_keeps_existing_roots_in_order(src_layout: Path) -> None:
    assert cpi._source_paths(src_layout, [".", "src", "missing"]) == [
        src_layout.resolve(),
        (src_layout / "src").resolve(),
    ]


def test_detect_src_packages_finds_regular_packages(src_layout: Path) -> None:
    (src_layout / "src" / "not_a_package").mkdir()
    assert cpi._detect_src_packages(src_layout) == {"dcfixturepkg"}


def test_detect_src_packages_without_src_dir(tmp_path: Path) -> None:
    assert cpi._detect_src_packages(tmp_path) == set()


# --------------------------------------------------------------------------- #
# find_stale_imports on a src layout
# --------------------------------------------------------------------------- #
def test_src_layout_imports_are_validated(src_layout: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cpi, "REPO_ROOT", src_layout)
    monkeypatch.setattr(cpi, "PROJECT_PACKAGES", cpi._detect_src_packages(src_layout))
    for path in reversed(cpi._source_paths(src_layout, [".", "src"])):
        monkeypatch.syspath_prepend(str(path))
    importlib.invalidate_caches()

    md = src_layout / "README.md"
    md.write_text(
        textwrap.dedent(
            """\
            ```python
            from dcfixturepkg.core import helper
            from dcfixturepkg.core import gone
            from dcfixturepkg.missing import thing
            ```
            """
        )
    )

    stale, errors = cpi.find_stale_imports([md])

    assert errors == []
    assert {(s.module, s.name, s.reason) for s in stale} == {
        ("dcfixturepkg.core", "gone", "name not defined in dcfixturepkg.core"),
        ("dcfixturepkg.missing", None, "module not found"),
    }
