"""Tests for the ASCII project-tree audit (``doc_checks.check_trees``)."""

from __future__ import annotations

import textwrap

import pytest

from doc_checks import check_trees as ct


# --------------------------------------------------------------------------- #
# _clean_name
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("api.ts", "api.ts"),
        ("`config.py`", "config.py"),  # backticks stripped
        ("src/", "src"),  # trailing slash stripped
        ("run.sh*", "run.sh"),  # ls -F executable marker
        ("link@", "link"),  # ls -F symlink marker
        ("socket=", "socket"),  # ls -F socket marker
        ("pipe|", "pipe"),  # ls -F fifo marker
        ("lib/api.ts", "lib/api.ts"),  # nested path kept intact
    ],
)
def test_clean_name_normalizes(raw: str, expected: str) -> None:
    assert ct._clean_name(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "...",  # placeholder
        "…",  # unicode ellipsis placeholder
        "etc.",  # placeholder
        "etc",  # placeholder
        "../sibling",  # parent reference
        "*.py",  # wildcard
        "test_*.py",  # wildcard
        "file[0].txt",  # glob char
        "---",  # pure decoration
        "",  # empty
    ],
)
def test_clean_name_rejects_non_paths(raw: str) -> None:
    assert ct._clean_name(raw) is None


# --------------------------------------------------------------------------- #
# extract_tree_entries
# --------------------------------------------------------------------------- #
def test_extracts_box_drawing_tree() -> None:
    md = textwrap.dedent(
        """\
        Layout:

        ```
        myproject/
        ├── src/
        │   ├── app.py
        │   └── utils.py
        └── README.md
        ```
        """
    )
    names = [name for _, name in ct.extract_tree_entries(md)]
    assert names == ["src", "app.py", "utils.py", "README.md"]


def test_extracts_ascii_tree() -> None:
    md = textwrap.dedent(
        """\
        ```text
        root
        |-- a.py
        |   `-- nested.py
        +-- b.py
        \\-- c.py
        ```
        """
    )
    names = [name for _, name in ct.extract_tree_entries(md)]
    assert names == ["a.py", "nested.py", "b.py", "c.py"]


def test_strips_trailing_comments() -> None:
    md = textwrap.dedent(
        """\
        ```
        repo/
        ├── main.py      # entry point
        └── conf.yaml    # configuration
        ```
        """
    )
    names = [name for _, name in ct.extract_tree_entries(md)]
    assert names == ["main.py", "conf.yaml"]


def test_ignores_non_tree_fences() -> None:
    md = textwrap.dedent(
        """\
        ```python
        def f():
            return 1
        ```

        ```
        x = [1, 2, 3]
        ```
        """
    )
    assert ct.extract_tree_entries(md) == []


def test_supports_tilde_fences() -> None:
    md = textwrap.dedent(
        """\
        ~~~
        proj/
        └── only.py
        ~~~
        """
    )
    names = [name for _, name in ct.extract_tree_entries(md)]
    assert names == ["only.py"]


def test_unterminated_fence_still_scanned() -> None:
    md = "```\nproj/\n└── dangling.py\n"
    names = [name for _, name in ct.extract_tree_entries(md)]
    assert names == ["dangling.py"]


def test_reports_correct_line_numbers() -> None:
    md = textwrap.dedent(
        """\
        intro

        ```
        proj/
        ├── first.py
        └── second.py
        ```
        """
    )
    entries = ct.extract_tree_entries(md)
    assert entries == [(5, "first.py"), (6, "second.py")]


def test_info_string_opt_out() -> None:  # issue #9
    # A ``no-trees`` info-string tag skips the block entirely.
    md = textwrap.dedent(
        """\
        ```text no-trees
        Do you have a database?
        ├─ NO  → migrate
        └─ YES → current
        ```
        """
    )
    assert ct.extract_tree_entries(md) == []


def test_comment_opt_out() -> None:  # issue #9
    md = textwrap.dedent(
        """\
        <!-- doc-checks: ignore-trees -->
        ```text
        RunFingerprint
        ├── comparability_key   SHA-256 of inputs
        └── prompt_content_hash SHA-256 of prompt
        ```
        """
    )
    assert ct.extract_tree_entries(md) == []


def test_comment_opt_out_only_applies_to_next_fence() -> None:  # issue #9
    # The comment is consumed by the first following fence; an unrelated real
    # tree afterwards is still validated.
    md = textwrap.dedent(
        """\
        <!-- doc-checks: ignore-trees -->
        ```text
        ├─ NO  → x
        ```

        ```
        proj/
        └── real.py
        ```
        """
    )
    names = [name for _, name in ct.extract_tree_entries(md)]
    assert names == ["real.py"]


def test_root_line_not_treated_as_entry() -> None:
    # The unindented root (``proj/``) has no branch connector, so it is not
    # validated — only the branch lines are.
    md = "```\nnonexistent-root/\n└── real.py\n```\n"
    names = [name for _, name in ct.extract_tree_entries(md)]
    assert names == ["real.py"]


# --------------------------------------------------------------------------- #
# find_stale_trees (with injected names + files)
# --------------------------------------------------------------------------- #
def _write(tmp_path, body: str):
    p = tmp_path / "doc.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_find_stale_flags_missing_basename(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ct, "REPO_ROOT", tmp_path)
    md = _write(
        tmp_path,
        "```\nproj/\n├── present.py\n└── gone.py\n```\n",
    )
    stale = ct.find_stale_trees(files=[md], names={"present.py"})
    assert len(stale) == 1
    assert stale[0].name == "gone.py"
    assert stale[0].file == "doc.md"
    assert stale[0].line == 4


def test_find_stale_matches_leaf_of_nested_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ct, "REPO_ROOT", tmp_path)
    md = _write(tmp_path, "```\nproj/\n└── src/api.ts\n```\n")
    # Only the leaf basename exists in the repo — that's enough to pass.
    assert ct.find_stale_trees(files=[md], names={"api.ts"}) == []


def test_find_stale_empty_when_all_present(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ct, "REPO_ROOT", tmp_path)
    md = _write(tmp_path, "```\nproj/\n├── a.py\n└── b.py\n```\n")
    assert ct.find_stale_trees(files=[md], names={"a.py", "b.py"}) == []


# --------------------------------------------------------------------------- #
# existing_basenames
# --------------------------------------------------------------------------- #
def test_existing_basenames_prunes_dirs(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("", encoding="utf-8")

    names = ct.existing_basenames(tmp_path)
    assert "real.py" in names
    assert "src" in names
    # .git is pruned: neither the dir nor its contents are collected.
    assert "config" not in names
    assert ".git" not in names


# --------------------------------------------------------------------------- #
# run (CheckResult integration)
# --------------------------------------------------------------------------- #
def test_run_passes_on_clean_repo(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ct, "REPO_ROOT", tmp_path)
    (tmp_path / "a.py").write_text("", encoding="utf-8")
    md = _write(tmp_path, "```\nproj/\n└── a.py\n```\n")
    monkeypatch.setattr(ct, "_iter_markdown_files", lambda: [md])

    result = ct.run()
    assert result.passed is True
    assert "existing paths" in result.message


def test_run_fails_and_reports_details(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(ct, "REPO_ROOT", tmp_path)
    md = _write(tmp_path, "```\nproj/\n└── ghost.py\n```\n")
    monkeypatch.setattr(ct, "_iter_markdown_files", lambda: [md])

    result = ct.run()
    assert result.passed is False
    assert "1 stale tree entry" in result.message
    assert any("ghost.py" in line for line in result.details)
