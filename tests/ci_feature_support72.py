"""Shared fixtures and helpers for feature-preserving test partitions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from quality_gate import ci_parity, runner
from quality_gate.ci_parity import compare_results
from quality_gate.distribution import DistributionError, PolicyCache

REPOSITORY = Path(__file__).resolve().parents[1]

WORKFLOW = REPOSITORY / ".github" / "workflows" / "quality.yml"

DEPENDABOT = REPOSITORY / ".github" / "dependabot.yml"

PARITY_WORKFLOW = REPOSITORY / ".github" / "workflows" / "parity.yml"

PARITY_SCRIPT = REPOSITORY / "quality_gate" / "ci_parity.py"

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")

EXPECTED_CI_CHECK_INVOCATIONS = 2

POLICY_RELEASE = "v2.0.6"

SCANNER_RELEASE = "v2.0.5"

LEGACY_TEST_RELEASE = "v2.0.0"

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


def _make_writable(root: Path) -> None:
	for path in (root, *root.rglob("*")):
		if not path.is_symlink():
			path.chmod(path.stat().st_mode | stat.S_IWUSR)


def _policy_root() -> Path | None:
	configured = os.environ.get("QUALITY_GATE_POLICY_ROOT")
	if configured:
		root = Path(configured)
		if (root / "release.toml").is_file():
			return root
	return None


def _cached_policy_root() -> Path | None:
	try:
		return PolicyCache().select(SCANNER_RELEASE)
	except (DistributionError, FileNotFoundError, OSError, ValueError):
		return None


def _installed_scanner() -> Path | None:
	"""Return the installed scanner from PATH or the active policy release."""

	discovered = shutil.which("gitleaks")
	if discovered is not None:
		return Path(discovered)
	policy_root = _policy_root() or _cached_policy_root()
	if policy_root is None:
		return None
	try:
		release = tomllib.loads((policy_root / "release.toml").read_text(encoding="utf-8"))[
			"release"
		]
	except (DistributionError, FileNotFoundError, OSError, ValueError):
		return None
	tool = next((item for item in release["tools"] if item["name"] == "gitleaks"), None)
	tool_path = None if tool is None else policy_root / tool["path"]
	return tool_path if tool_path is not None and tool_path.is_file() else None


def _build_wheel(tmp_path: Path) -> Path:
	destination = tmp_path / "wheel"
	destination.mkdir()
	policy_root = _policy_root()
	if policy_root is not None:
		release = tomllib.loads((policy_root / "release.toml").read_text(encoding="utf-8"))[
			"release"
		]
		wheel_entry = next(
			item
			for item in release["files"]
			if item.get("kind", "artifact") == "artifact" and item["path"].endswith(".whl")
		)
		return Path(shutil.copy2(policy_root / wheel_entry["path"], destination))
	source = tmp_path / "quality-gate-source"
	shutil.copytree(
		REPOSITORY,
		source,
		ignore=shutil.ignore_patterns(
			".git", ".venv", ".quality-gate-tmp", "temp", "build", "*.egg-info", "__pycache__"
		),
	)
	_make_writable(source)
	build_python = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
	result = subprocess.run(
		[
			str(build_python),
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


@pytest.fixture(scope="module")
def _release_gate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
	"""Build and install one immutable release CLI for the module's contract tests."""
	root = tmp_path_factory.mktemp("release-gate")
	wheel = _build_wheel(root)
	return _install_wheel(root, wheel), wheel


def _write_release(release: Path, scanner: Path | bytes, wheel: Path) -> None:
	"""Write a historical release fixture for generic CI contract tests."""

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
version = "{LEGACY_TEST_RELEASE}"

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


def _copy_legacy_no_python_fixture(root: Path) -> None:
	shutil.copytree(REPOSITORY / "tests" / "fixtures" / "no-python", root)
	_make_writable(root)
	manifest = root / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8").replace(POLICY_RELEASE, LEGACY_TEST_RELEASE),
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


def _valid_parity_result() -> dict[str, object]:
	return {
		"policy_release": "v2.0.0",
		"tool_names_versions": [["gitleaks", "8.30.1"]],
		"supplemental_tests": [],
		"check_surface": {"secrets.candidate": "passed", "secrets.history": "failed"},
		"pr_history_verdict": "failed",
		"redaction": {
			"full_secret_absent": True,
			"meaningful_substring_absent": True,
		},
		"shallow_history": {"history_verdict": "unchecked"},
		"unavailable_scanner": {
			"exit_code": runner.EXIT_UNCHECKED,
			"candidate": "unchecked",
			"history": "unchecked",
		},
	}


def _write_parity_pair(
	tmp_path: Path, left: dict[str, object], right: dict[str, object]
) -> tuple[Path, Path]:
	left_path = tmp_path / "left.json"
	right_path = tmp_path / "right.json"
	left_path.write_text(json.dumps(left), encoding="utf-8")
	right_path.write_text(json.dumps(right), encoding="utf-8")
	return left_path, right_path


__all__ = [
	"DEPENDABOT",
	"DistributionError",
	"EXPECTED_CI_CHECK_INVOCATIONS",
	"FULL_SHA",
	"LEGACY_TEST_RELEASE",
	"PARITY_SCRIPT",
	"PARITY_WORKFLOW",
	"POLICY_RELEASE",
	"Path",
	"PolicyCache",
	"REPOSITORY",
	"RESULT_LINE",
	"SCANNER_RELEASE",
	"WORKFLOW",
	"_assert_shallow_history_is_unchecked",
	"_build_wheel",
	"_cached_policy_root",
	"_copy_legacy_no_python_fixture",
	"_git",
	"_install_wheel",
	"_installed_scanner",
	"_make_writable",
	"_policy_root",
	"_release_gate",
	"_result_surface",
	"_run_cli",
	"_valid_parity_result",
	"_write_parity_pair",
	"_write_release",
	"ci_parity",
	"compare_results",
	"hashlib",
	"json",
	"os",
	"pytest",
	"re",
	"runner",
	"shutil",
	"stat",
	"subprocess",
	"sys",
	"tomllib",
]
