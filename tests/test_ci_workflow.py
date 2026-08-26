"""Public contract tests for the reusable GitHub CI workflow."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tomllib
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from quality_gate import runner
from quality_gate.contracts import CheckResult, Status
from quality_gate.distribution import DistributionError, PolicyCache

REPOSITORY = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY / ".github" / "workflows" / "quality.yml"
DEPENDABOT = REPOSITORY / ".github" / "dependabot.yml"
PARITY_WORKFLOW = REPOSITORY / ".github" / "workflows" / "parity.yml"
PARITY_SCRIPT = REPOSITORY / "quality_gate" / "ci_parity.py"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_CI_CHECK_INVOCATIONS = 2
RESULT_LINE = re.compile(
	r"^(?P<check_id>[a-z0-9_.]+): "
	r"(?P<status>passed|failed|unchecked|not_applicable|waived)(?: - |$)",
	re.MULTILINE,
)


def _git(root: Path, *arguments: str) -> str:
	result = subprocess.run(
		["git", *arguments],
		cwd=root,
		env={**os.environ, "GIT_CONFIG_GLOBAL": str(root / "missing-global-config")},
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr
	return result.stdout.strip()


def _run_cli(
	executable: Path, root: Path, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
	isolated_environment = environment.copy()
	isolated_environment.pop("PYTHONPATH", None)
	isolated_environment.pop("VIRTUAL_ENV", None)
	return subprocess.run(
		[str(executable), "--root", str(root), "check", *arguments],
		cwd=root,
		env=isolated_environment,
		capture_output=True,
		text=True,
		check=False,
	)


def _result_surface(output: str) -> dict[str, str]:
	return {
		match.group("check_id"): match.group("status") for match in RESULT_LINE.finditer(output)
	}


def _installed_scanner() -> Path | None:
	"""Return the installed scanner from PATH or the active policy release."""

	discovered = shutil.which("gitleaks")
	if discovered is not None:
		return Path(discovered)
	try:
		policy_root = PolicyCache().select("v2.0.0")
		release = tomllib.loads((policy_root / "release.toml").read_text(encoding="utf-8"))[
			"release"
		]
	except (DistributionError, FileNotFoundError, OSError, ValueError):
		return None
	tool = next((item for item in release["tools"] if item["name"] == "gitleaks"), None)
	tool_path = None if tool is None else policy_root / tool["path"]
	return tool_path if tool_path is not None and tool_path.is_file() else None


def _build_wheel(tmp_path: Path) -> Path:
	source = tmp_path / "quality-gate-source"
	shutil.copytree(
		REPOSITORY,
		source,
		ignore=shutil.ignore_patterns(
			".git", ".venv", ".quality-gate-tmp", "build", "*.egg-info", "__pycache__"
		),
	)
	destination = tmp_path / "wheel"
	destination.mkdir()
	result = subprocess.run(
		[
			sys.executable,
			"-m",
			"pip",
			"wheel",
			"--no-deps",
			"--no-build-isolation",
			str(source),
			"--wheel-dir",
			str(destination),
		],
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr
	return next(destination.glob("quality_gate-*.whl"))


def _install_wheel(tmp_path: Path, wheel: Path) -> Path:
	venv = tmp_path / "gate-venv"
	result = subprocess.run(
		[sys.executable, "-m", "venv", str(venv)],
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr
	python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
	result = subprocess.run(
		[str(python), "-m", "pip", "install", "--no-deps", str(wheel)],
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr
	return venv / ("Scripts/quality-gate.exe" if os.name == "nt" else "bin/quality-gate")


def _write_release(release: Path, scanner: Path | bytes, wheel: Path) -> None:
	"""Write a release fixture with the wheel that the test installs."""

	release.mkdir()
	shutil.copy2(wheel, release / wheel.name)
	if isinstance(scanner, Path):
		shutil.copy2(scanner, release / "gitleaks.exe")
	else:
		(release / "gitleaks.exe").write_bytes(scanner)
	digests = {
		name: hashlib.sha256((release / name).read_bytes()).hexdigest()
		for name in (wheel.name, "gitleaks.exe")
	}
	(release / "release.toml").write_text(
		f'''[release]
version = "v2.0.0"

[[release.files]]
path = "{wheel.name}"
sha256 = "{digests[wheel.name]}"

[[release.tools]]
name = "gitleaks"
version = "8.30.1"
path = "gitleaks.exe"
sha256 = "{digests["gitleaks.exe"]}"
''',
		encoding="utf-8",
	)


def _assert_shallow_history_is_unchecked(
	root: Path,
	temporary_root: Path,
	executable: Path,
	environment: dict[str, str],
	base: str,
	credential: str,
) -> None:
	"""Verify an unavailable base in a shallow CI clone fails closed."""

	shallow = temporary_root / "shallow"
	clone = subprocess.run(
		["git", "clone", "--depth", "1", root.resolve().as_uri(), str(shallow)],
		capture_output=True,
		text=True,
		check=False,
	)
	assert clone.returncode == 0, clone.stderr
	result = _run_cli(executable, shallow, environment, "--base", base, "--head", "HEAD")

	assert result.returncode == runner.EXIT_UNCHECKED
	assert _result_surface(result.stdout)["secrets.history"] == "unchecked"
	assert credential not in result.stdout + result.stderr


def test_reusable_workflow_runs_the_pinned_release_and_complete_cli_contract() -> None:
	"""Verify CI bootstraps the manifest release and delegates to the public gate CLI."""

	workflow = WORKFLOW.read_text(encoding="utf-8")
	references = re.findall(r"^ +uses: +([^ ]+)", workflow, re.MULTILINE)
	manifest = tomllib.loads((REPOSITORY / "quality-gate.toml").read_text(encoding="utf-8"))

	assert references
	assert all(
		"@" in reference and FULL_SHA.fullmatch(reference.rsplit("@", 1)[1])
		for reference in references
	)
	assert "@main" not in workflow
	assert "fetch-depth: 0" in workflow
	assert "quality-gate sync --source" in workflow
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
	assert manifest["quality"]["policy_release"] == "v2.0.0"


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
	assert "timeout-minutes: 10" in workflow
	assert "cancel-in-progress: true" in workflow
	assert "Default-branch pushes are detection-only" in workflow
	assert "private GitHub Free repositories" in workflow


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


def test_ci_and_local_check_return_the_same_public_result_contract(
	monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	"""Verify CI and local invocations share check IDs and statuses at the CLI seam."""

	root = REPOSITORY / "tests" / "fixtures" / "no-python"
	monkeypatch.setattr(
		runner,
		"candidate_snapshot",
		lambda _root: nullcontext(SimpleNamespace(root=root)),
	)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *_args, **_kwargs: SimpleNamespace(
			policy_root=REPOSITORY, release_manifest=None, runtimes=()
		),
	)
	monkeypatch.setattr(
		runner,
		"secret_candidate_result",
		lambda *_args, **_kwargs: CheckResult(
			"secrets.candidate", Status.PASSED, "no credentials detected"
		),
	)
	monkeypatch.setattr(
		runner,
		"secret_history_result",
		lambda *_args, **_kwargs: CheckResult(
			"secrets.history",
			Status.NOT_APPLICABLE,
			"history range not requested",
			recovery_action="provide a verified CI base reference when range scanning applies",
		),
	)

	local = runner.check(root)
	ci = runner.check(root, base="HEAD", head="HEAD")
	capsys.readouterr()

	assert [(item.check_id, item.status) for item in local.results] == [
		(item.check_id, item.status) for item in ci.results
	]


def test_ci_and_local_cli_runs_match_on_a_release_backed_repository(tmp_path: Path) -> None:
	"""Verify local and CI runs expose one release, tool, result, and redaction surface."""

	scanner = _installed_scanner()
	assert scanner is not None, "the active policy release must provide Gitleaks"
	wheel = _build_wheel(tmp_path)
	gate = _install_wheel(tmp_path, wheel)
	root = tmp_path / "repository"
	shutil.copytree(REPOSITORY / "tests" / "fixtures" / "no-python", root)
	workflow = root / ".github" / "workflows" / "quality.yml"
	workflow.parent.mkdir(parents=True)
	shutil.copy2(WORKFLOW, workflow)
	_git(root, "init")
	_git(root, "add", ".")
	_git(
		root,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"base",
	)
	base = _git(root, "rev-parse", "HEAD")
	credential = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"[:36]
	credential_path = root / "credentials.txt"
	credential_path.write_text(f"access={credential}\n", encoding="utf-8")
	_git(root, "add", "credentials.txt")
	_git(
		root,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"add credential fixture",
	)
	credential_path.unlink()
	_git(root, "add", "credentials.txt")
	_git(
		root,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"remove credential fixture",
	)

	release = root / ".release"
	_write_release(release, scanner, wheel)

	cache_bases = [root / ".local-cache", root / ".ci-cache"]
	selected_releases = []
	for cache_base in cache_bases:
		cache = PolicyCache(cache_base / "quality-gate")
		cache.sync(release)
		selected = cache.select("v2.0.0")
		selected_releases.append(
			tomllib.loads((selected / "release.toml").read_text(encoding="utf-8"))["release"]
		)
	output_marker = "PARITY_OUTPUT_MARKER_7c83e5"
	environments = [
		{
			**os.environ,
			"LOCALAPPDATA": str(cache_base),
			"XDG_CACHE_HOME": str(cache_base),
			"QUALITY_GATE_PARITY_MARKER": output_marker,
		}
		for cache_base in cache_bases
	]
	local = _run_cli(gate, root, environments[0], "--base", base, "--head", "HEAD")
	ci = _run_cli(gate, root, environments[1], "--base", base, "--head", "HEAD")

	assert local.returncode == ci.returncode == 1
	assert _result_surface(local.stdout) == _result_surface(ci.stdout)
	assert _result_surface(local.stdout)
	assert _result_surface(local.stdout)["secrets.history"] == "failed"
	assert [release["version"] for release in selected_releases] == ["v2.0.0", "v2.0.0"]
	assert [
		[(tool["name"], tool["version"]) for tool in release["tools"]]
		for release in selected_releases
	] == [[("gitleaks", "8.30.1")], [("gitleaks", "8.30.1")]]
	assert "credentials.txt" in local.stdout
	assert "credentials.txt" in ci.stdout
	assert credential not in local.stdout + local.stderr
	assert credential not in ci.stdout + ci.stderr
	assert credential[4:20] not in local.stdout + local.stderr
	assert credential[4:20] not in ci.stdout + ci.stderr
	assert output_marker not in local.stdout
	assert output_marker not in ci.stdout

	_assert_shallow_history_is_unchecked(root, tmp_path, gate, environments[1], base, credential)


def test_ci_reports_an_unavailable_release_scanner_as_unchecked(tmp_path: Path) -> None:
	"""Verify an unusable release tool cannot produce a clean CI verdict."""

	root = tmp_path / "repository"
	shutil.copytree(REPOSITORY / "tests" / "fixtures" / "no-python", root)
	_git(root, "init")
	_git(root, "add", ".")
	_git(
		root,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"base",
	)
	release = root / ".release"
	wheel = _build_wheel(tmp_path)
	gate = _install_wheel(tmp_path, wheel)
	_write_release(release, b"not an executable scanner", wheel)
	cache_base = root / ".ci-cache"
	PolicyCache(cache_base / "quality-gate").sync(release)
	environment = {
		**os.environ,
		"LOCALAPPDATA": str(cache_base),
		"XDG_CACHE_HOME": str(cache_base),
	}

	result = _run_cli(gate, root, environment, "--base", "HEAD", "--head", "HEAD")

	assert result.returncode == runner.EXIT_UNCHECKED
	assert _result_surface(result.stdout)["secrets.candidate"] == "unchecked"
	assert _result_surface(result.stdout)["secrets.history"] == "unchecked"
