"""Caller wiring for the shared preparation/build/publication contract."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_open_pr_validation_is_read_only_and_includes_body_edits() -> None:
	workflow = (ROOT / ".github/workflows/release-validation.yml").read_text(encoding="utf-8")
	assert "types: [opened, synchronize, reopened, edited]" in workflow
	assert "validation-only: true" in workflow
	assert "./.github/workflows/release-prepare.yml" in workflow
	assert "RELEASE_TOKEN" not in workflow


def test_reusable_workflows_checkout_their_executed_helper_revision() -> None:
	for name in ("prepare", "publish"):
		workflow = (ROOT / f".github/workflows/release-{name}.yml").read_text(encoding="utf-8")
		assert "workflow_call:" in workflow
		assert "repository: ${{ job.workflow_repository }}" in workflow
		assert "ref: ${{ job.workflow_sha }}" in workflow
		assert "PUBLISHER_SHA: ${{ job.workflow_sha }}" in workflow
		assert "persist-credentials: false" in workflow
		assert "pip install" not in workflow
		assert "scripts/source_release.py" in workflow


def test_product_only_builds_required_candidates_and_isolates_publication_credentials() -> None:
	workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
	assert "types: [closed]" in workflow
	assert "github.event.pull_request.merged == true" in workflow
	assert "workflow_dispatch:" in workflow
	assert "pr_number:" in workflow
	assert "needs.prepare.outputs.status == 'build-required'" in workflow
	assert "ref: ${{ needs.prepare.outputs.source-sha }}" in workflow
	assert "needs: [prepare, build-and-verify]" in workflow
	assert "./.github/workflows/release-publish.yml" in workflow
	assert workflow.count("secrets.RELEASE_TOKEN") == 1
	assert "secrets.RELEASE_TOKEN" not in workflow.split("  publish:")[0]
	assert "Impact:" not in workflow
	assert "cancel-in-progress: false" in (
		ROOT / ".github/workflows/release-publish.yml"
	).read_text(encoding="utf-8")
