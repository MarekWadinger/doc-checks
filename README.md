# doc-checks

Self-healing documentation checks, distributed as [pre-commit](https://pre-commit.com)
hooks. They fail the commit when markdown docs drift from reality:

| Hook | What it catches |
|------|-----------------|
| `doc-check-make-refs` | `make <target>` references in markdown that no longer exist in the Makefile |
| `doc-check-cd-refs` | `cd <path>` references pointing at directories that no longer exist |
| `doc-check-py-imports` | Project-local imports in markdown code fences that no longer resolve |
| `doc-check-mermaid` | Broken Mermaid diagram syntax (via `mmdc`, skipped if not installed) |

## Use in your project

Add the hooks to your repo's `.pre-commit-config.yaml` — pre-commit installs
this package into an isolated environment, no manual setup needed
(hook definitions: [`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml)):

```yaml
repos:
  - repo: <git url of this repo>
    rev: v0.1.0
    hooks:
      - id: doc-check-make-refs   # requires a Makefile at your repo root
      - id: doc-check-cd-refs
      - id: doc-check-py-imports
      - id: doc-check-mermaid
```

Pick only the hooks that fit: `doc-check-make-refs` fails when it finds no
Makefile targets to validate against, so skip it in repos without a Makefile.

For broken-link checking, pair these with the official
[lychee](https://lychee.cli.rs) hook or a local one (see this repo's
[`.pre-commit-config.yaml`](.pre-commit-config.yaml) and
[`.lychee.toml`](.lychee.toml) for a working offline-mode setup).

### Per-repo configuration

Defaults live in the packaged [`src/doc_checks/config.yaml`](src/doc_checks/config.yaml).
To override, drop a `.doc-checks.yaml` at your repo root — sections are merged
key-by-key over the defaults, so you only state what differs:

```yaml
make_refs:
  ignore_targets: [deploy]   # documented but defined in another repo's Makefile
py_imports:
  project_packages: [myapp]  # which top-level imports count as project-local
mermaid:
  require_mmdc: true         # fail instead of skip when mmdc is missing
```

This repo's own [`.doc-checks.yaml`](.doc-checks.yaml) is a working example.

## Developing this repo

Requires [uv](https://docs.astral.sh/uv/). Optional: [lychee](https://lychee.cli.rs)
(`brew install lychee`) and `mmdc` (`npm install -g @mermaid-js/mermaid-cli`).

```bash
make install   # uv sync + pre-commit install
```

Hooks run automatically on `git commit`. To run manually:

```bash
make doc-check             # run all checks via the runner
make doc-check-make-refs   # run a single check
make pre-commit            # run every hook against all files
```

### Adding a check

Add a `check_<name>.py` module in [`src/doc_checks/`](src/doc_checks/)
exposing a `run` function — the runner auto-discovers it:

```python
from doc_checks import CheckResult

def run(files: list[str] | None = None) -> CheckResult:
    return CheckResult(passed=True, message="...")
```

Then register a console script in [`pyproject.toml`](pyproject.toml) and a
hook entry in [`.pre-commit-hooks.yaml`](.pre-commit-hooks.yaml) to make it
available to consumers.
