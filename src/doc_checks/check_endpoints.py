"""Endpoint Registry Check — detects stale endpoint docs against code.

Parses framework router decorators (``@router.get("/path")``,
``@app.post(...)``, …) from each configured service's source directory and
compares the routes against the endpoints documented in that service's README
files.

Policy:
    * Code (the OpenAPI surface) is the source of truth.
    * READMEs are high-level and may intentionally omit internal or new
      endpoints — *undocumented* code routes are reported as information only.
    * The check **fails** on *stale* documented endpoints: a README references
      a route that no longer exists in code.
    * Internal/framework endpoints (``/health``, ``/docs``, …) are skipped.

The check is config-driven via the ``endpoints.services`` map and passes
trivially when no services are configured, so the hook is safe to enable
anywhere.

Usage:
    python -m doc_checks.check_endpoints
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

from pydantic import BaseModel

from doc_checks import CheckResult, get_config, repo_root

REPO_ROOT = repo_root()

_cfg = get_config().get("endpoints", {})
INTERNAL_ENDPOINTS: set[str] = set(_cfg.get("internal", ["/status", "/health", "/docs", "/openapi.json", "/redoc", "/"]))
# Decorator receiver names treated as routers, e.g. ``@router.get`` / ``@app.post``.
ROUTER_NAMES: set[str] = set(_cfg.get("router_names", ["router", "app"]))
# HTTP methods recognized on a router decorator.
HTTP_METHODS: set[str] = {m.lower() for m in _cfg.get("methods", ["get", "post", "put", "delete", "patch", "head", "options"])}
# Regex prefixes stripped from paths before comparison (e.g. versioned API roots).
STRIP_PATH_PREFIXES: list[str] = _cfg.get("strip_path_prefixes", [r"^/api/v\d+"])

_SERVICES_CFG: dict[str, dict] = _cfg.get("services", {})
SERVICES: dict[str, dict] = {
    name: {
        "endpoints_dir": REPO_ROOT / svc["endpoints_dir"],
        "readme_files": [REPO_ROOT / p for p in svc.get("readme_files", [])],
    }
    for name, svc in _SERVICES_CFG.items()
}


class CodeEndpoint(BaseModel):
    """An endpoint extracted from a router decorator in source code."""

    method: str
    path: str
    function: str
    file: str


class DocEndpoint(BaseModel):
    """An endpoint reference extracted from a README file."""

    path: str
    method: str | None = None


def normalize_path(path: str) -> str:
    """Normalize an endpoint path for comparison.

    Strips configured prefixes (e.g. ``/api/vN``) and trailing slashes, and
    replaces named params with ``{}`` so ``{id}`` and ``{user_id}`` compare equal.
    """
    normalized = path
    for pattern in STRIP_PATH_PREFIXES:
        normalized = re.sub(pattern, "", normalized)
    normalized = normalized.rstrip("/")
    normalized = re.sub(r"\{[^}]+\}", "{}", normalized)
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def is_internal(path: str) -> bool:
    """Check whether a path is an internal/framework endpoint."""
    return normalize_path(path) in INTERNAL_ENDPOINTS or path in INTERNAL_ENDPOINTS


def _router_prefix(tree: ast.Module) -> str:
    """Best-effort extraction of ``APIRouter(prefix=...)`` from module assignments."""
    prefix = ""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id in ROUTER_NAMES for t in node.targets):
            continue
        if isinstance(node.value, ast.Call):
            for kw in node.value.keywords:
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                    prefix = str(kw.value.value)
    return prefix


def extract_endpoints_from_code(endpoints_dir: Path) -> tuple[list[CodeEndpoint], list[str]]:
    """Extract endpoint definitions from router decorators via AST.

    Returns ``(endpoints, parse_warnings)``.
    """
    endpoints: list[CodeEndpoint] = []
    parse_warnings: list[str] = []

    for py_file in sorted(endpoints_dir.glob("*.py")):
        if py_file.name.startswith("_"):
            continue
        try:
            tree = ast.parse(py_file.read_text())
        except SyntaxError as exc:
            parse_warnings.append(f"⚠ {py_file.name}: SyntaxError — {exc.msg} (line {exc.lineno})")
            continue

        prefix = _router_prefix(tree)

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)):
                    continue
                func = decorator.func
                if not (isinstance(func.value, ast.Name) and func.value.id in ROUTER_NAMES):
                    continue
                if func.attr.lower() not in HTTP_METHODS:
                    continue
                method = func.attr.upper()
                path = ""
                if decorator.args and isinstance(decorator.args[0], ast.Constant):
                    path = str(decorator.args[0].value)
                for kw in decorator.keywords:
                    if kw.arg == "path" and isinstance(kw.value, ast.Constant):
                        path = str(kw.value.value)
                endpoints.append(
                    CodeEndpoint(method=method, path=f"{prefix}{path}", function=node.name, file=py_file.name),
                )

    return endpoints, parse_warnings


def _extract_service_sections(text: str, service_name: str) -> str:
    """Return markdown sections whose heading mentions ``service_name``."""
    lines = text.splitlines()
    sections: list[str] = []
    current: list[str] = []
    in_section = False
    section_level = 0

    for line in lines:
        heading = re.match(r"^(#{1,4})\s+(.*)", line)
        if heading:
            level = len(heading.group(1))
            title = heading.group(2)
            if re.search(rf"\b{re.escape(service_name)}\b", title, re.IGNORECASE):
                if current:
                    sections.append("\n".join(current))
                current = []
                in_section = True
                section_level = level
                continue
            if in_section and level <= section_level:
                if current:
                    sections.append("\n".join(current))
                    current = []
                in_section = False
                continue
        if in_section:
            current.append(line)

    if current:
        sections.append("\n".join(current))
    return "\n".join(sections)


def extract_endpoints_from_readme(
    readme_path: Path,
    service_name: str,
    *,
    is_service_readme: bool = False,
) -> list[DocEndpoint]:
    """Extract endpoint references from a README file.

    ``is_service_readme`` scans the whole file (already scoped to one service);
    otherwise only sections of the root README that name the service are scanned.
    """
    if not readme_path.exists():
        return []

    text = readme_path.read_text()
    if is_service_readme:
        combined = text
    else:
        combined = _extract_service_sections(text, service_name)
        if not combined:
            return []

    found: list[DocEndpoint] = []

    # Strategy 1: explicit full API paths like ``/api/v1/something``.
    for match in re.finditer(r"(/api/v\d+/[\w{}/_-]+)", combined):
        path = match.group(1).rstrip("/")
        if path:
            found.append(DocEndpoint(path=path))

    # Strategy 2: ``**Endpoints**: /path1, /path2`` lines.
    for match in re.finditer(r"\*\*Endpoints\*\*:\s*(.*)", combined, re.IGNORECASE):
        for path_match in re.finditer(r"(/[\w{}/_-]+)", match.group(1)):
            path = path_match.group(1).rstrip("/")
            if path and len(path) > 1:
                found.append(DocEndpoint(path=path))

    # Strategy 3: method-prefixed paths like ``GET /path``.
    for match in re.finditer(r"(GET|POST|PUT|DELETE|PATCH)\s+(/[\w{}/_-]+)", combined):
        path = match.group(2).rstrip("/")
        if path:
            found.append(DocEndpoint(method=match.group(1), path=path))

    # Deduplicate by normalized path, preferring entries that carry a method.
    seen: dict[str, DocEndpoint] = {}
    for entry in found:
        norm = normalize_path(entry.path)
        if norm not in seen or (entry.method and not seen[norm].method):
            seen[norm] = entry
    return list(seen.values())


def _match_paths(code_norm: str, doc_norm: str) -> bool:
    """Whether a normalized code path matches a normalized doc path.

    Matches on exact equality, doc-as-prefix (``/conversations`` vs
    ``/conversations/{}/messages``), or doc-as-suffix (``/messages``).
    """
    if code_norm == doc_norm:
        return True
    if code_norm.startswith(doc_norm + "/"):
        return True
    code_segs = [s for s in code_norm.strip("/").split("/") if s]
    doc_segs = [s for s in doc_norm.strip("/").split("/") if s]
    if doc_segs and len(doc_segs) <= len(code_segs) and code_segs[-len(doc_segs) :] == doc_segs:
        return True
    return False


def run(files: list[str] | None = None) -> CheckResult:  # noqa: ARG001 (whole-repo audit)
    """Run the endpoint registry check across all configured services."""
    if not SERVICES:
        return CheckResult(passed=True, message="No services configured — nothing to check.")

    issues: list[str] = []
    details: list[str] = []

    for service_name, config in SERVICES.items():
        endpoints_dir = config["endpoints_dir"]
        if not endpoints_dir.exists():
            issues.append(f"{service_name}: endpoints directory not found at {endpoints_dir}")
            continue

        code_endpoints, parse_warnings = extract_endpoints_from_code(endpoints_dir)
        for warn in parse_warnings:
            details.append(f"    {warn}")

        if not code_endpoints:
            details.append(f"  {service_name}: No endpoints found in code (possibly dynamic)")
            continue

        public_endpoints = [ep for ep in code_endpoints if not is_internal(ep.path)]
        internal_count = len(code_endpoints) - len(public_endpoints)

        details.append(f"\n  [{service_name}] Endpoints found in code:")
        for ep in code_endpoints:
            marker = " (internal)" if is_internal(ep.path) else ""
            details.append(f"    {ep.method:6s} {ep.path:40s}  ({ep.file}::{ep.function}){marker}")

        service_stale_count = 0
        code_norms = {normalize_path(ep.path) for ep in public_endpoints}
        documented_anywhere: set[str] = set()

        for readme_path in config["readme_files"]:
            if not readme_path.exists():
                continue
            is_svc_readme = readme_path.parent.name == service_name
            readme_endpoints = extract_endpoints_from_readme(readme_path, service_name, is_service_readme=is_svc_readme)
            readme_rel = readme_path.relative_to(REPO_ROOT)

            if not readme_endpoints:
                details.append(f"    ⚠ No endpoint paths found in {readme_rel} for {service_name}")
                continue

            # Undocumented code endpoints — informational, per README.
            for cp_norm in sorted(code_norms):
                if any(_match_paths(cp_norm, normalize_path(de.path)) for de in readme_endpoints):
                    documented_anywhere.add(cp_norm)
                else:
                    details.append(
                        f"    ℹ {service_name}: {cp_norm} exists in code, not in {readme_rel} (allowed by policy)",
                    )

            # Stale documented endpoints — hard errors.
            for de in readme_endpoints:
                doc_norm = normalize_path(de.path)
                if not any(_match_paths(cp_norm, doc_norm) for cp_norm in code_norms):
                    service_stale_count += 1
                    method_str = f"{de.method} " if de.method else ""
                    issues.append(
                        f"{service_name}: {method_str}{de.path} documented in {readme_rel} but NOT found in code",
                    )

        undocumented_unique = code_norms - documented_anywhere
        details.append(
            f"    📊 {service_name}: {len(public_endpoints)} public endpoints in code, "
            f"{internal_count} internal (skipped), "
            f"{len(undocumented_unique)} undocumented (info), "
            f"{service_stale_count} stale (error)",
        )

    passed = not issues
    msg = "No stale endpoint docs found." if passed else f"Found {len(issues)} stale endpoint documentation mismatch(es)."
    return CheckResult(
        passed=passed,
        message=msg,
        details=[*details, "", *[f"  ❌ {i}" for i in issues]] if issues else details,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit documented endpoints against router decorators in code.")
    parser.add_argument("files", nargs="*", help="(ignored — the audit always scans configured services)")
    parser.parse_args()

    result = run()
    status = "PASS ✅" if result.passed else "FAIL ❌"
    print(f"[Endpoint Check] {status}: {result.message}")
    for line in result.details:
        print(line)
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
