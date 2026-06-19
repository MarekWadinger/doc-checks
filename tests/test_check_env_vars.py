"""Tests for the env var audit (``doc_checks.check_env_vars``)."""

from __future__ import annotations

import ast
import textwrap

import pytest

from doc_checks import check_env_vars as ev


# --------------------------------------------------------------------------- #
# _field_has_default (via parsed AnnAssign nodes)
# --------------------------------------------------------------------------- #
def _ann_assign(src: str) -> ast.AnnAssign:
    node = ast.parse(src).body[0]
    assert isinstance(node, ast.AnnAssign)
    return node


@pytest.mark.parametrize(
    "src",
    [
        "x: str",  # bare annotation
        "x: str = ...",  # ellipsis literal
        "x: str = Field(...)",  # Field with ellipsis positional
        "x: str = Field(..., description='d')",  # ellipsis first, kwargs after
        "x: str = Field(default=...)",  # explicit ellipsis default
        "x: str = Field(description='d')",  # Field with no default at all
    ],
)
def test_field_required(src: str) -> None:
    assert ev._field_has_default(_ann_assign(src)) is False


@pytest.mark.parametrize(
    "src",
    [
        "x: str = 'hello'",  # plain default
        "x: int = 5",
        "x: str = Field(default='hello')",  # explicit default kwarg
        "x: str = Field('hello')",  # default as positional
        "x: list = Field(default_factory=list)",  # factory => optional
    ],
)
def test_field_optional(src: str) -> None:
    assert ev._field_has_default(_ann_assign(src)) is True


# --------------------------------------------------------------------------- #
# extract_settings_fields
# --------------------------------------------------------------------------- #
def test_extract_fields_with_prefix_and_required_flag(tmp_path) -> None:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        textwrap.dedent(
            """\
            class Settings(BaseSettings):
                model_config = SettingsConfigDict(env_prefix="APP_")
                api_key: str  # required
                timeout: int = 30  # optional
            """
        ),
        encoding="utf-8",
    )
    classes = ev.extract_settings_fields(cfg)
    assert set(classes) == {"Settings"}
    fields = {f.name: f.has_default for f in classes["Settings"]}
    assert fields == {"APP_API_KEY": False, "APP_TIMEOUT": True}


def test_extract_skips_ignored_fields_and_non_settings(tmp_path) -> None:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        textwrap.dedent(
            """\
            class NotSettings:
                secret: str

            class Settings(BaseSettings):
                token: str
            """
        ),
        encoding="utf-8",
    )
    classes = ev.extract_settings_fields(cfg)
    assert set(classes) == {"Settings"}
    assert [f.name for f in classes["Settings"]] == ["TOKEN"]


def test_extract_follows_transitive_settings_base(tmp_path) -> None:
    cfg = tmp_path / "config.py"
    cfg.write_text(
        textwrap.dedent(
            """\
            class Base(BaseSettings):
                shared: str

            class Sub(Base):
                extra: str = "x"
            """
        ),
        encoding="utf-8",
    )
    classes = ev.extract_settings_fields(cfg)
    assert set(classes) == {"Base", "Sub"}


# --------------------------------------------------------------------------- #
# extract_env_example_vars / extract_readme_env_vars
# --------------------------------------------------------------------------- #
def test_extract_env_example_vars(tmp_path) -> None:
    env = tmp_path / ".env.example"
    env.write_text("# comment\nAPP_API_KEY=secret\nAPP_TIMEOUT=\n\nnot_a_var\n", encoding="utf-8")
    assert ev.extract_env_example_vars(env) == {"APP_API_KEY", "APP_TIMEOUT"}


def test_extract_readme_env_vars_scopes_to_section(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        textwrap.dedent(
            """\
            # Project

            Uses an API and SSE somewhere — these acronyms must NOT match.

            ## Environment Variables

            - `APP_API_KEY` — the key
            - `APP_TIMEOUT` — seconds

            ## Other

            `OTHER_VAR` is outside the section.
            """
        ),
        encoding="utf-8",
    )
    found = ev.extract_readme_env_vars(readme)
    assert found == {"APP_API_KEY", "APP_TIMEOUT"}


# --------------------------------------------------------------------------- #
# run (CheckResult integration)
# --------------------------------------------------------------------------- #
def _setup_repo(tmp_path, monkeypatch, *, config_src: str, env_src: str) -> None:
    cfg = tmp_path / "config.py"
    cfg.write_text(config_src, encoding="utf-8")
    env = tmp_path / ".env.example"
    env.write_text(env_src, encoding="utf-8")
    monkeypatch.setattr(ev, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ev, "ENV_EXAMPLE", env)
    monkeypatch.setattr(ev, "README_FILE", tmp_path / "README.md")
    monkeypatch.setattr(ev, "_iter_config_files", lambda: [cfg])


def test_run_passes_when_required_present(tmp_path, monkeypatch) -> None:
    _setup_repo(
        tmp_path,
        monkeypatch,
        config_src="class Settings(BaseSettings):\n    api_key: str\n    timeout: int = 30\n",
        env_src="API_KEY=x\n",  # required present; optional TIMEOUT may be absent
    )
    result = ev.run()
    assert result.passed is True
    assert "All required config fields" in result.message


def test_run_fails_on_missing_required(tmp_path, monkeypatch) -> None:
    _setup_repo(
        tmp_path,
        monkeypatch,
        config_src="class Settings(BaseSettings):\n    api_key: str\n    db_url: str\n",
        env_src="API_KEY=x\n",  # DB_URL (required) missing
    )
    result = ev.run()
    assert result.passed is False
    assert "1 required env var" in result.message
    assert any("DB_URL" in line for line in result.details)


def test_run_passes_when_no_settings_classes(tmp_path, monkeypatch) -> None:
    _setup_repo(
        tmp_path,
        monkeypatch,
        config_src="class Plain:\n    x = 1\n",
        env_src="",
    )
    result = ev.run()
    assert result.passed is True
    assert "No Pydantic Settings classes" in result.message
