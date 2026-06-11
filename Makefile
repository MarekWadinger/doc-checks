.PHONY: install doc-check doc-check-make-refs doc-check-cd-refs doc-check-py-imports doc-check-mermaid pre-commit

install:
	uv sync --all-groups
	uv run pre-commit install

doc-check:
	uv run python -m scripts.doc_checks.runner

doc-check-make-refs:
	uv run python -m scripts.doc_checks.check_make_refs

doc-check-cd-refs:
	uv run python -m scripts.doc_checks.check_cd_refs

doc-check-py-imports:
	uv run python -m scripts.doc_checks.check_py_imports

doc-check-mermaid:
	uv run python -m scripts.doc_checks.check_mermaid

pre-commit:
	uv run pre-commit run --all-files
