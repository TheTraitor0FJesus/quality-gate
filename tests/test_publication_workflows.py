"""Caller wiring for the shared preparation/build/publication contract."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _assert_job_permission(workflow: str, job: str, permission: str) -> None:
	diagnostic = f"jobs.{job}.permissions must grant {permission}"
	jobs = re.search(r"^jobs:\n(.*?)(?=^\S|\Z)", workflow, re.MULTILINE | re.DOTALL)
	assert jobs is not None, diagnostic
	job_block = re.search(
		rf"^  {re.escape(job)}:\n(.*?)(?=^  \S|\Z)",
		jobs.group(1),
		re.MULTILINE | re.DOTALL,
	)
	assert job_block is not None, diagnostic
	permissions = re.search(
		r"^    permissions:\n(.*?)(?=^    \S|\Z)",
		job_block.group(1),
		re.MULTILINE | re.DOTALL,
	)
	assert permissions is not None, diagnostic
	assert f"      {permission}" in permissions.group(1).splitlines(), diagnostic


def test_open_pr_validation_is_read_only_and_includes_body_edits() -> None:
	workflow = (ROOT / ".github/workflows/release-validation.yml").read_text(encoding="utf-8")
	assert "types: [opened, synchronize, reopened, edited]" in workflow
	assert "validation-only: true" in workflow
	assert "./.github/workflows/release-prepare.yml" in workflow
	assert "RELEASE_TOKEN" not in workflow
	assert "packages: read" not in workflow


def test_reusable_workflows_checkout_their_executed_helper_revision() -> None:
	workflows = [
		ROOT / ".github/workflows/release-prepare.yml",
		ROOT / ".github/workflows/release-publish.yml",
		ROOT / ".github/workflows/image-writer.yml",
	]
	for path in workflows:
		workflow = path.read_text(encoding="utf-8")
		assert "workflow_call:" in workflow
		assert "RELEASE_TOKEN" not in workflow
		assert "repository: ${{ job.workflow_repository }}" in workflow
		assert "ref: ${{ job.workflow_sha }}" in workflow
		assert "PUBLISHER_SHA: ${{ job.workflow_sha }}" in workflow
		assert "persist-credentials: false" in workflow
		assert "pip install" not in workflow
		assert "scripts/source_release.py" in workflow
		assert "registry-auth" not in workflow
		assert "PUBLISHER_REGISTRY_AUTH" not in workflow
		if path.name == "release-publish.yml":
			assert "packages: read" in workflow
		else:
			assert "packages: read" not in workflow
		assert "PUBLISHER_GHCR_TOKEN" not in workflow
		assert "PUBLISHER_GHCR_USERNAME" not in workflow
		if path.name != "release-prepare.yml":
			assert "GH_TOKEN: ${{ github.token }}" in workflow


def test_product_only_builds_required_candidates_and_uses_job_scoped_github_token() -> None:
	workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
	assert "types: [closed]" in workflow
	assert "github.event.pull_request.merged == true" in workflow
	assert "workflow_dispatch:" in workflow
	assert "pr_number:" in workflow
	assert "needs.prepare.outputs.status == 'build-required'" in workflow
	assert "ref: ${{ needs.prepare.outputs.source-sha }}" in workflow
	assert "needs: [prepare, build-and-verify]" in workflow
	assert "./.github/workflows/release-publish.yml" in workflow
	assert "RELEASE_TOKEN" not in workflow
	_assert_job_permission(workflow, "publish", "contents: write")
	assert "packages: read" not in workflow.split("jobs:")[0]
	_assert_job_permission(workflow, "publish", "packages: read")
	publisher = (ROOT / ".github/workflows/release-publish.yml").read_text(encoding="utf-8")
	assert "timeout-minutes: 30" in publisher
	assert "Impact:" not in workflow
	assert "cancel-in-progress: false" in (
		ROOT / ".github/workflows/release-publish.yml"
	).read_text(encoding="utf-8")


def test_shared_image_writer_uses_a_job_scoped_package_write_token() -> None:
	workflow = (ROOT / ".github/workflows/image-writer.yml").read_text(encoding="utf-8")
	flow = (ROOT / "quality_gate/publication_flow.py").read_text(encoding="utf-8")
	_assert_job_permission(workflow, "push-images", "packages: write")
	assert "      - name: Write images\n" in workflow
	assert 'checks = ["Write images"]' in flow
	assert "pr-number:" in workflow
	assert "PR_NUMBER: ${{ inputs.pr-number }}" in workflow
	assert 'write-images --pr "$PR_NUMBER"' in workflow
	assert "packages: read" not in workflow
	assert "GH_TOKEN: ${{ github.token }}" in workflow
	assert "PUBLISHER_GHCR_TOKEN" not in workflow
	assert "PUBLISHER_GHCR_USERNAME" not in workflow
	assert "password: ${{ github.token }}" not in workflow
	assert "secrets." not in workflow


@pytest.mark.parametrize(
	("filename", "job", "permission"),
	[
		pytest.param("release.yml", "publish", "contents: write", id="publish-contents-write"),
		pytest.param("release.yml", "publish", "packages: read", id="publish-packages-read"),
		pytest.param(
			"image-writer.yml", "push-images", "packages: write", id="push-images-packages-write"
		),
	],
)
def test_job_permission_guard_rejects_later_job_decoy(
	filename: str, job: str, permission: str
) -> None:
	workflow = (ROOT / ".github/workflows" / filename).read_text(encoding="utf-8")
	_assert_job_permission(workflow, job, permission)
	permission_line = f"      {permission}\n"
	assert workflow.count(permission_line) == 1
	decoy = workflow.replace(permission_line, "", 1)
	decoy += f"\n  later-job:\n    permissions:\n{permission_line}    runs-on: ubuntu-latest\n"
	assert permission in decoy.split(f"  {job}:", 1)[1]
	with pytest.raises(
		AssertionError, match=re.escape(f"jobs.{job}.permissions must grant {permission}")
	):
		_assert_job_permission(decoy, job, permission)
