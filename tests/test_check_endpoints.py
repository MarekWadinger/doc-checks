"""Tests for the endpoint registry check (``doc_checks.check_endpoints``)."""

from __future__ import annotations

import textwrap

import pytest

from doc_checks import check_endpoints as ep


# --------------------------------------------------------------------------- #
# normalize_path / is_internal / _match_paths
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/api/v1/conversations/{conversation_id}/messages", "/conversations/{}/messages"),
        ("/api/v2/users", "/users"),
        ("/chat/stream/", "/chat/stream"),
        ("users", "/users"),
    ],
)
def test_normalize_path(raw: str, expected: str) -> None:
    assert ep.normalize_path(raw) == expected


def test_is_internal() -> None:
    assert ep.is_internal("/health") is True
    assert ep.is_internal("/api/v1/health") is True  # prefix stripped, then matched
    assert ep.is_internal("/conversations") is False


@pytest.mark.parametrize(
    ("code", "doc", "match"),
    [
        ("/conversations/{}/messages", "/conversations/{}/messages", True),  # exact
        ("/conversations/{}/messages", "/conversations", True),  # doc prefix
        ("/conversations/{}/messages", "/messages", True),  # doc suffix
        ("/conversations", "/users", False),  # unrelated
    ],
)
def test_match_paths(code: str, doc: str, match: bool) -> None:
    assert ep._match_paths(code, doc) is match


# --------------------------------------------------------------------------- #
# extract_endpoints_from_code
# --------------------------------------------------------------------------- #
def test_extract_endpoints_with_prefix(tmp_path) -> None:
    d = tmp_path / "endpoints"
    d.mkdir()
    (d / "convos.py").write_text(
        textwrap.dedent(
            """\
            router = APIRouter(prefix="/api/v1/conversations")

            @router.get("/")
            async def list_convos():
                ...

            @router.post("/{conversation_id}/messages")
            async def add_message():
                ...
            """
        ),
        encoding="utf-8",
    )
    endpoints, warnings = ep.extract_endpoints_from_code(d)
    assert warnings == []
    paths = {(e.method, e.path) for e in endpoints}
    assert paths == {
        ("GET", "/api/v1/conversations/"),
        ("POST", "/api/v1/conversations/{conversation_id}/messages"),
    }


def test_extract_skips_underscore_files_and_non_router_decorators(tmp_path) -> None:
    d = tmp_path / "endpoints"
    d.mkdir()
    (d / "_private.py").write_text("@router.get('/x')\ndef f(): ...\n", encoding="utf-8")
    (d / "pub.py").write_text(
        "router = APIRouter()\n\n@staticmethod\ndef helper(): ...\n\n@router.get('/ok')\ndef ok(): ...\n",
        encoding="utf-8",
    )
    endpoints, _ = ep.extract_endpoints_from_code(d)
    assert {(e.method, e.path) for e in endpoints} == {("GET", "/ok")}


def test_extract_reports_syntax_error(tmp_path) -> None:
    d = tmp_path / "endpoints"
    d.mkdir()
    (d / "broken.py").write_text("def f(:\n", encoding="utf-8")
    endpoints, warnings = ep.extract_endpoints_from_code(d)
    assert endpoints == []
    assert len(warnings) == 1 and "broken.py" in warnings[0]


# --------------------------------------------------------------------------- #
# extract_endpoints_from_readme
# --------------------------------------------------------------------------- #
def test_extract_from_readme_strategies(tmp_path) -> None:
    readme = tmp_path / "README.md"
    readme.write_text(
        textwrap.dedent(
            """\
            ## api

            Call `/api/v1/users` to list users.

            **Endpoints**: /conversations, /chat/stream

            `GET /api/v1/health`
            """
        ),
        encoding="utf-8",
    )
    found = {ep.normalize_path(d.path) for d in ep.extract_endpoints_from_readme(readme, "api", is_service_readme=True)}
    assert "/users" in found
    assert "/conversations" in found
    assert "/chat/stream" in found


# --------------------------------------------------------------------------- #
# run (CheckResult integration)
# --------------------------------------------------------------------------- #
def _setup(tmp_path, monkeypatch, *, code: str, readme: str) -> None:
    d = tmp_path / "endpoints"
    d.mkdir()
    (d / "api.py").write_text(code, encoding="utf-8")
    (tmp_path / "README.md").write_text(readme, encoding="utf-8")
    monkeypatch.setattr(ep, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        ep,
        "SERVICES",
        {"api": {"endpoints_dir": d, "readme_files": [tmp_path / "README.md"]}},
    )


def test_run_passes_when_no_services(monkeypatch) -> None:
    monkeypatch.setattr(ep, "SERVICES", {})
    result = ep.run()
    assert result.passed is True
    assert "No services configured" in result.message


def test_run_passes_when_docs_match_code(tmp_path, monkeypatch) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        code='router = APIRouter(prefix="/api/v1")\n\n@router.get("/users")\ndef u(): ...\n',
        readme="## api\n\n`GET /api/v1/users`\n",
    )
    result = ep.run()
    assert result.passed is True
    assert "No stale endpoint docs" in result.message


def test_run_fails_on_stale_documented_endpoint(tmp_path, monkeypatch) -> None:
    _setup(
        tmp_path,
        monkeypatch,
        code='router = APIRouter(prefix="/api/v1")\n\n@router.get("/users")\ndef u(): ...\n',
        readme="## api\n\nCall `/api/v1/ghost` for ghosts.\n",
    )
    result = ep.run()
    assert result.passed is False
    assert "1 stale endpoint" in result.message
    assert any("ghost" in line for line in result.details)
