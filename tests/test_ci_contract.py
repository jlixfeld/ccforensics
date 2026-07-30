from pathlib import Path


def test_ci_formats_source_without_rewriting_markdown() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    lint_job = workflow[workflow.index("lint:") : workflow.index("uv-checks:")]

    assert "uv tool install ruff" in lint_job
    assert "uv tool install ruff==" not in lint_job
    assert "ruff format --check --exclude '*.md' ." in lint_job
