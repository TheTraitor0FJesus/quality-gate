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
from quality_gate.distribution import PolicyCache

REPOSITORY = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY / ".github" / "workflows" / "quality.yml"
DEPENDABOT = REPOSITORY / ".github" / "dependabot.yml"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
EXPECTED_CI_CHECK_INVOCATIONS = 2
RESULT_LINE = re.compile(
	r"^(?P<check_id>[a-z0-9_.]+): (?P<status>passed|failed|unchecked|not_applicable|waived)$",
	re.MULTILINE,
)


def _git(root: Path, *arguments: str) -> None:
	result = subprocess.run(
		["git", *arguments],
		cwd=root,
		env={**os.environ, "GIT_CONFIG_GLOBAL": str(root / "missing-global-config")},
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stderr


def _run_cli(
	root: Path, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
	return subprocess.run(
		[sys.executable, "-m", "quality_gate", "--root", str(root), "check", *arguments],
		cwd=root,
		env=environment,
		capture_output=True,
		text=True,
		check=False,
	)


def _result_surface(output: str) -> dict[str, str]:
	return {
		match.group("check_id"): match.group("status")
		for match in RESULT_LINE.finditer(output)
		if match.group("check_id") != "secrets.history"
	}


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
	assert "release wheel checksum mismatch" in workflow
	assert "quality-gate setup" in workflow
	assert "quality-gate check" in workflow
	assert '--base "$QUALITY_GATE_BASE"' in workflow
	assert '--head "$QUALITY_GATE_HEAD"' in workflow
	assert "QUALITY_GATE_RELEASE_URL" in workflow
	assert "QUALITY_GATE_RELEASE_DIR" in workflow
	assert "QUALITY_GATE_ARCHIVE" in workflow
	assert "--max-time 60" in workflow
	assert "--max-filesize 104857600" in workflow
	assert "release archive exceeds the size or entry limit" in workflow
	assert "manifest_python.outputs.versions" in workflow
	assert manifest["quality"]["policy_release"] == "v2.0.0"


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
	"""Verify local and CI-shaped CLI runs expose the same release-backed result surface."""

	scanner = shutil.which("gitleaks")
	if scanner is None:
		pytest.skip("Gitleaks is required for the release-backed parity fixture")
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

	cache_root = root / ".localappdata" / "quality-gate"
	release = root / ".release"
	release.mkdir()
	(release / "quality_gate-2.0.0-py3-none-any.whl").write_bytes(b"wheel")
	shutil.copy2(scanner, release / "gitleaks.exe")
	digests = {
		name: hashlib.sha256((release / name).read_bytes()).hexdigest()
		for name in ("quality_gate-2.0.0-py3-none-any.whl", "gitleaks.exe")
	}
	(release / "release.toml").write_text(
		f'''[release]
version = "v2.0.0"

[[release.files]]
path = "quality_gate-2.0.0-py3-none-any.whl"
sha256 = "{digests["quality_gate-2.0.0-py3-none-any.whl"]}"

[[release.tools]]
name = "gitleaks"
version = "8.30.1"
path = "gitleaks.exe"
sha256 = "{digests["gitleaks.exe"]}"
''',
		encoding="utf-8",
	)

	PolicyCache(cache_root).sync(release)
	environment = {
		**os.environ,
		"LOCALAPPDATA": str(root / ".localappdata"),
		"PYTHONPATH": str(REPOSITORY),
	}
	local = _run_cli(root, environment)
	ci = _run_cli(root, environment, "--base", "HEAD", "--head", "HEAD")

	assert local.returncode == ci.returncode == 0
	assert _result_surface(local.stdout) == _result_surface(ci.stdout)
