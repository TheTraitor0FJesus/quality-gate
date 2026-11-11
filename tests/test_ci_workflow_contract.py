"""Static contracts for the reusable GitHub CI workflow."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY / ".github" / "workflows" / "quality.yml"
DEPENDABOT = REPOSITORY / ".github" / "dependabot.yml"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_CI_CHECK_INVOCATIONS = 2


def test_reusable_workflow_runs_the_pinned_release_and_complete_cli_contract() -> None:
	"""Verify CI bootstraps the manifest release and delegates to the public gate CLI."""

	workflow = WORKFLOW.read_text(encoding="utf-8")
	references = re.findall(r"^ +uses: +([^ ]+)", workflow, re.MULTILINE)
	manifest = tomllib.loads((REPOSITORY / "quality-gate.toml").read_text(encoding="utf-8"))
	template = tomllib.loads(
		(REPOSITORY / "templates" / "quality-gate.toml").read_text(encoding="utf-8")
	)
	fixture = tomllib.loads(
		(REPOSITORY / "tests" / "fixtures" / "release-parity" / "quality-gate.toml").read_text(
			encoding="utf-8"
		)
	)
	publisher = tomllib.loads(
		(REPOSITORY / ".release" / "publisher.toml").read_text(encoding="utf-8")
	)

	assert references
	assert all(
		"@" in reference and FULL_SHA.fullmatch(reference.rsplit("@", 1)[1])
		for reference in references
	)
	assert "@main" not in workflow
	assert "fetch-depth: 0" in workflow
	assert "quality-gate sync --source" in workflow
	assert "release-tools.toml" in workflow
	assert "GITHUB_REPOSITORY" in workflow
	assert '["bootstrap"]["policy_release"]' in workflow
	assert "scripts/release_adapter.py bootstrap-setup" in workflow
	assert "scripts/release_adapter.py bootstrap-check" in workflow
	assert "quality_gate/release_contract.py" in workflow
	assert "curl --fail --location" in workflow
	assert "github.workflow_sha" not in workflow
	assert "repository: ${{ job.workflow_repository }}" in workflow
	assert "ref: ${{ job.workflow_sha }}" in workflow
	assert "quality_gate/ci_release.py" in workflow
	assert "quality-gate setup" in workflow
	assert "quality-gate check" in workflow
	assert '--base "$QUALITY_GATE_BASE"' in workflow
	assert '--head "$QUALITY_GATE_HEAD"' in workflow
	assert "QUALITY_GATE_RELEASE_URL" in workflow
	assert "QUALITY_GATE_RELEASE_DIR" in workflow
	assert "QUALITY_GATE_ARCHIVE" in workflow
	assert "--max-time 60" in workflow
	assert "--max-filesize 104857600" in workflow
	assert (
		'QUALITY_GATE_ASSET_NAME="quality-gate-${QUALITY_GATE_RELEASE}-${RUNNER_OS}.zip"'
		in workflow
	)
	assert '"${QUALITY_GATE_RELEASE_URL}/${QUALITY_GATE_ASSET_NAME}"' in workflow
	assert 'QUALITY_GATE_WHEEL="$(python .quality-gate-ci/quality_gate/ci_release.py' in workflow
	assert "manifest_python.outputs.versions" in workflow
	assert 'default: "3.12"' not in workflow
	assert manifest["quality"]["policy_release"] == "v4.8.0"
	version = tomllib.loads((REPOSITORY / ".release" / "version.toml").read_text(encoding="utf-8"))
	assert template["quality"]["policy_release"] == f"v{version['version']}"
	assert fixture["quality"]["policy_release"] == manifest["quality"]["policy_release"]
	assert all(item["path"] != "quality-gate.toml" for item in publisher["projections"])
	assert any(item["path"] == "templates/quality-gate.toml" for item in publisher["projections"])


def test_workflow_bootstraps_before_reading_a_multicomponent_manifest(tmp_path: Path) -> None:
	"""Verify the portable bootstrap and multiline setup-python contract."""

	manifest_path = tmp_path / "quality-gate.toml"
	manifest_path.write_text(
		"""[quality]
policy_release = "v2.0.0"

[[python]]
name = "first"
python_version = "3.11"

[[python]]
name = "second"
python_version = "3.12"
""",
		encoding="utf-8",
	)
	manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
	versions = sorted({item["python_version"] for item in manifest["python"]})
	workflow = WORKFLOW.read_text(encoding="utf-8")
	bootstrap = workflow.index('python-version: "3.12"')
	read_manifest = workflow.index("Read declared Python versions")
	prepare_declared = workflow.index("manifest_python.outputs.versions")

	assert versions == ["3.11", "3.12"]
	assert bootstrap < read_manifest
	assert read_manifest < prepare_declared
	assert "shell: python" in workflow
	assert "python -c" in workflow
	assert "versions<<QUALITY_GATE_VERSIONS" in workflow
	assert "\\n" in workflow
	assert "inputs['python-version'] || steps.manifest_python.outputs.versions" in workflow


def test_reusable_workflow_has_one_stable_job_and_detects_private_push_bypass() -> None:
	"""Verify CI triggers, bounds, and reports the private default-branch limitation."""

	workflow = WORKFLOW.read_text(encoding="utf-8")

	assert "workflow_call:" in workflow
	assert "pull_request:" in workflow
	assert "push:" in workflow
	assert "branches:" in workflow
	assert "- main" in workflow
	assert "name: Quality Gate" in workflow
	assert "github.event.repository.default_branch" in workflow
	assert "timeout-minutes: 50" in workflow
	assert (
		"group: quality-${{ github.workflow }}-${{ github.ref }}-"
		"${{ inputs['runner-os'] || 'ubuntu-latest' }}" in workflow
	)
	assert "cancel-in-progress: true" in workflow
	assert "Default-branch pushes are detection-only" in workflow
	assert "private GitHub Free repositories" in workflow


def test_provider_verification_budgets_fit_the_50_minute_ci_job() -> None:
	"""Keep nested Python and Gate deadlines below the workflow's outer timeout."""
	manifest = tomllib.loads((REPOSITORY / "quality-gate.toml").read_text(encoding="utf-8"))
	defaults = manifest["repository"]["defaults"]

	assert defaults["test_timeout_seconds"] == 30 * 60
	assert defaults["gate_timeout_seconds"] == 40 * 60
	assert defaults["test_timeout_seconds"] < defaults["gate_timeout_seconds"] < 50 * 60
	assert "timeout-minutes: 50" in WORKFLOW.read_text(encoding="utf-8")


def test_dependabot_updates_pinned_actions_weekly_without_auto_merge() -> None:
	"""Verify Dependabot proposes weekly GitHub Actions updates without merging them."""

	dependabot = DEPENDABOT.read_text(encoding="utf-8")

	assert "version: 2" in dependabot
	assert "package-ecosystem: github-actions" in dependabot
	assert 'directory: "/"' in dependabot
	assert "interval: weekly" in dependabot
	assert "auto-merge" not in dependabot.casefold()


def test_ci_and_local_parity_uses_one_cli_verdict_contract() -> None:
	"""Verify both CI event paths invoke the same complete CLI contract."""

	workflow = WORKFLOW.read_text(encoding="utf-8")

	assert workflow.count("quality-gate check") == EXPECTED_CI_CHECK_INVOCATIONS
	assert "quality-gate audit" not in workflow
	assert '--base "$QUALITY_GATE_BASE" --head "$QUALITY_GATE_HEAD"' in workflow
	assert 'quality-gate check --head "$GITHUB_SHA"' in workflow
	assert "quality-gate check --verbose" not in workflow
