.PHONY: install test doc-check doc-check-make-refs doc-check-cd-refs doc-check-py-imports doc-check-trees doc-check-mermaid pre-commit build

install:
	uv sync --all-groups
	uv run pre-commit install

doc-check:
	uv run doc-check

doc-check-make-refs:
	uv run doc-check-make-refs

doc-check-cd-refs:
	uv run doc-check-cd-refs

doc-check-py-imports:
	uv run doc-check-py-imports

doc-check-trees:
	uv run doc-check-trees

doc-check-mermaid:
	uv run doc-check-mermaid

test:
	uv run pytest

pre-commit:
	uv run pre-commit run --all-files

build:
	uv build
