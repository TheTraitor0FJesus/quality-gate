"""Feature-preserving test partition."""

from __future__ import annotations

from ci_feature_support72 import (
	DEPENDABOT as DEPENDABOT,
)
from ci_feature_support72 import (
	EXPECTED_CI_CHECK_INVOCATIONS as EXPECTED_CI_CHECK_INVOCATIONS,
)
from ci_feature_support72 import (
	FULL_SHA as FULL_SHA,
)
from ci_feature_support72 import (
	LEGACY_TEST_RELEASE as LEGACY_TEST_RELEASE,
)
from ci_feature_support72 import (
	PARITY_SCRIPT as PARITY_SCRIPT,
)
from ci_feature_support72 import (
	PARITY_WORKFLOW as PARITY_WORKFLOW,
)
from ci_feature_support72 import (
	POLICY_RELEASE as POLICY_RELEASE,
)
from ci_feature_support72 import (
	REPOSITORY as REPOSITORY,
)
from ci_feature_support72 import (
	RESULT_LINE as RESULT_LINE,
)
from ci_feature_support72 import (
	SCANNER_RELEASE as SCANNER_RELEASE,
)
from ci_feature_support72 import (
	WORKFLOW as WORKFLOW,
)
from ci_feature_support72 import (
	DistributionError as DistributionError,
)
from ci_feature_support72 import (
	Path as Path,
)
from ci_feature_support72 import (
	PolicyCache as PolicyCache,
)
from ci_feature_support72 import (
	_assert_shallow_history_is_unchecked as _assert_shallow_history_is_unchecked,
)
from ci_feature_support72 import (
	_build_wheel as _build_wheel,
)
from ci_feature_support72 import (
	_cached_policy_root as _cached_policy_root,
)
from ci_feature_support72 import (
	_copy_legacy_no_python_fixture as _copy_legacy_no_python_fixture,
)
from ci_feature_support72 import (
	_git as _git,
)
from ci_feature_support72 import (
	_install_wheel as _install_wheel,
)
from ci_feature_support72 import (
	_installed_scanner as _installed_scanner,
)
from ci_feature_support72 import (
	_make_writable as _make_writable,
)
from ci_feature_support72 import (
	_policy_root as _policy_root,
)
from ci_feature_support72 import (
	_release_gate as _release_gate,
)
from ci_feature_support72 import (
	_result_surface as _result_surface,
)
from ci_feature_support72 import (
	_run_cli as _run_cli,
)
from ci_feature_support72 import (
	_valid_parity_result as _valid_parity_result,
)
from ci_feature_support72 import (
	_write_parity_pair as _write_parity_pair,
)
from ci_feature_support72 import (
	_write_release as _write_release,
)
from ci_feature_support72 import (
	ci_parity as ci_parity,
)
from ci_feature_support72 import (
	compare_results as compare_results,
)
from ci_feature_support72 import (
	hashlib as hashlib,
)
from ci_feature_support72 import (
	json as json,
)
from ci_feature_support72 import (
	os as os,
)
from ci_feature_support72 import (
	pytest as pytest,
)
from ci_feature_support72 import (
	re as re,
)
from ci_feature_support72 import (
	runner as runner,
)
from ci_feature_support72 import (
	shutil as shutil,
)
from ci_feature_support72 import (
	stat as stat,
)
from ci_feature_support72 import (
	subprocess as subprocess,
)
from ci_feature_support72 import (
	sys as sys,
)
from ci_feature_support72 import (
	tomllib as tomllib,
)




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


def test_parity_requests_verbose_status_lines_for_the_complete_check_map(
	monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
	"""Verify parity requests the full status listing that its parser consumes."""

	invocations: list[list[str]] = []

	def locate_executable(_name: str, *, path: str | None) -> str:
		assert path == "isolated-path"
		return "quality-gate"

	def run_gate(
		command: list[str],
		**_kwargs: object,
	) -> subprocess.CompletedProcess[str]:
		invocations.append(command)
		return subprocess.CompletedProcess(
			command,
			0,
			stdout=(
				"first.check: passed - complete\nsecond.check: unchecked - runtime unavailable\n"
			),
			stderr="",
		)

	monkeypatch.setattr(ci_parity.shutil, "which", locate_executable)
	monkeypatch.setattr(ci_parity.subprocess, "run", run_gate)

	result = ci_parity._gate(
		{"PATH": "isolated-path"},
		tmp_path,
		"--base",
		"BASE",
		"--head",
		"HEAD",
	)

	assert result.returncode == 0
	assert invocations[0][3:5] == ["check", "--verbose"]
	assert ci_parity._surface(result.stdout) == {
		"first.check": "passed",
		"second.check": "unchecked",
	}


def test_platform_parity_workflow_executes_the_gate_on_windows_and_linux() -> None:
	"""Verify repository CI executes the reusable parity contract on both platforms."""

	workflow = PARITY_WORKFLOW.read_text(encoding="utf-8")
	quality_workflow = WORKFLOW.read_text(encoding="utf-8")
	script = PARITY_SCRIPT.read_text(encoding="utf-8")

	assert "workflow_dispatch:" in workflow
	assert "schedule:" in workflow
	assert "pull_request:" not in workflow
	assert "runner-os: [ubuntu-latest, windows-latest]" in workflow
	assert "uses: ./.github/workflows/quality.yml" in workflow
	assert "runner-os: ${{ matrix.runner-os }}" in workflow
	assert "name: Parity (${{ matrix.runner-os }})" in workflow
	assert "parity: true" in workflow
	assert "actions/upload-artifact@" in quality_workflow
	assert "actions/download-artifact@" in workflow
	assert "compare" in workflow
	assert "quality_gate.ci_parity" in workflow
	assert "policy_release" in script
	assert "tool_names_versions" in script
	assert "secrets.history" in script
	assert "redaction" in script
	assert "unchecked" in script
	assert all(
		reference.startswith("./")
		or ("@" in reference and FULL_SHA.fullmatch(reference.rsplit("@", 1)[1]))
		for reference in re.findall(r"^ +uses: +([^ ]+)", workflow, re.MULTILINE)
	)


def test_consumer_templates_pin_the_caller_and_schedule_updates() -> None:
	"""Verify consumer templates preserve immutable workflow and update boundaries."""

	workflow = (REPOSITORY / "templates" / "quality.yml").read_text(encoding="utf-8")
	dependabot = (REPOSITORY / "templates" / "dependabot.yml").read_text(encoding="utf-8")

	assert "uses: TheTraitor0FJesus/quality-gate/.github/workflows/quality.yml@" in workflow
	assert "<40-character-commit-sha>" in workflow
	assert "interval: weekly" in dependabot
	assert "auto-merge" not in dependabot.casefold()


def test_compare_results_accepts_valid_equal_pair(tmp_path: Path) -> None:
	"""Accept equal platform results that satisfy every parity probe."""

	left_path, right_path = _write_parity_pair(
		tmp_path, _valid_parity_result(), _valid_parity_result()
	)

	compare_results(left_path, right_path)


def test_compare_results_rejects_unequal_contract_field(tmp_path: Path) -> None:
	"""Identify a differing release value while all other fields remain valid."""

	left = _valid_parity_result()
	right = _valid_parity_result()
	right["policy_release"] = "v2.0.1"
	left_path, right_path = _write_parity_pair(tmp_path, left, right)

	diagnostic = re.escape("parity mismatch in policy_release")
	with pytest.raises(RuntimeError, match=f"^{diagnostic}$"):
		compare_results(left_path, right_path)


@pytest.mark.parametrize(
	("field", "invalid_value", "diagnostic"),
	[
		(
			"redaction",
			{"full_secret_absent": False, "meaningful_substring_absent": True},
			"parity redaction contract failed",
		),
		(
			"redaction",
			{"full_secret_absent": True, "meaningful_substring_absent": False},
			"parity redaction contract failed",
		),
		(
			"shallow_history",
			{"history_verdict": "failed"},
			"shallow history must be unchecked",
		),
		(
			"unavailable_scanner",
			{"exit_code": runner.EXIT_UNCHECKED, "candidate": "failed", "history": "unchecked"},
			"unavailable scanner must be unchecked",
		),
	],
)
def test_compare_results_rejects_equal_invalid_probe(
	tmp_path: Path,
	field: str,
	invalid_value: object,
	diagnostic: str,
) -> None:
	"""Reject matching invalid probe results at their specific contract guard."""

	left = _valid_parity_result()
	right = _valid_parity_result()
	left[field] = invalid_value
	right[field] = invalid_value
	left_path, right_path = _write_parity_pair(tmp_path, left, right)

	with pytest.raises(RuntimeError, match=f"^{re.escape(diagnostic)}$"):
		compare_results(left_path, right_path)
