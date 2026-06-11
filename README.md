# doc-checks

Self-healing documentation checks wired into git via [pre-commit](https://pre-commit.com).
Keeps markdown docs honest by failing the commit when they drift from reality:

| Hook | What it catches |
|------|-----------------|
| `doc-check-make-refs` | `make <target>` references in markdown that no longer exist in the Makefile |
| `doc-check-cd-refs` | `cd <path>` references pointing at directories that no longer exist |
| `doc-check-py-imports` | Project-local imports in markdown code fences that no longer resolve |
| `doc-check-mermaid` | Broken Mermaid diagram syntax (via `mmdc`, skipped if not installed) |
| `lychee` | Broken internal file links (offline mode — external URLs are skipped) |

## Setup

Requires [uv](https://docs.astral.sh/uv/). Optional: [lychee](https://lychee.cli.rs)
(`brew install lychee`) and `mmdc` (`npm install -g @mermaid-js/mermaid-cli`).

```bash
make install   # uv sync + pre-commit install
```

## Usage

Hooks run automatically on `git commit`. To run manually:

```bash
make doc-check             # run all checks via the runner
make doc-check-make-refs   # run a single check
make pre-commit            # run every hook against all files
```

## Configuration

Project-specific values (scan globs, excluded paths, ignored targets) live in
[`scripts/doc_checks/config.yaml`](scripts/doc_checks/config.yaml). Link-check
behaviour is configured in [`.lychee.toml`](.lychee.toml).

## Adding a check

Drop a `check_<name>.py` module into [`scripts/doc_checks/`](scripts/doc_checks/)
exposing `run(files: list[str] | None = None) -> CheckResult`. The runner
auto-discovers it; add a hook entry in
[`.pre-commit-config.yaml`](.pre-commit-config.yaml) to enforce it on commit.
