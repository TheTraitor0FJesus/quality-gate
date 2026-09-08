"""Run and compare the release-backed cross-platform CI fixture."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from .contracts import load_manifest
from .distribution import PolicyCache, ReleaseManifest, load_release_manifest

RESULT_LINE = re.compile(
	r"^(?P<check_id>[a-z0-9_.]+): "
	r"(?P<status>passed|failed|unchecked|not_applicable|waived)(?: - |$)",
	re.MULTILINE,
)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PARITY_STATUSES = {
	"lessons.learning": "passed",
	"manifest.documents": "passed",
	"python.component_1.coverage": "not_applicable",
	"python.component_1.deptry": "passed",
	"python.component_1.format": "passed",
	"python.component_1.mypy": "passed",
	"python.component_1.pytest": "passed",
	"python.component_1.ruff": "passed",
	"repository.documentation.components": "passed",
	"repository.documentation.links": "passed",
	"repository.git.case_collisions": "passed",
	"repository.git.conflict_markers": "passed",
	"repository.git.large_blobs": "passed",
	"repository.git.tracked_junk": "passed",
	"repository.git.unsafe_symlinks": "passed",
	"repository.workflow": "passed",
	"secrets.candidate": "passed",
	"secrets.history": "failed",
	"web.component_1.biome_format": "passed",
	"web.component_1.biome_lint": "passed",
	"web.component_1.css_budget": "passed",
	"web.component_1.javascript_budget": "passed",
}


def _git(root: Path, *arguments: str) -> str:
	result = subprocess.run(
		["git", *arguments],
		cwd=root,
		env={**os.environ, "GIT_CONFIG_GLOBAL": str(root / "missing-global-config")},
		capture_output=True,
		text=True,
		check=False,
	)
	if result.returncode:
		raise RuntimeError(result.stderr.strip() or "git command failed")
	return result.stdout.strip()


def _gate(
	environment: dict[str, str], root: Path, *arguments: str
) -> subprocess.CompletedProcess[str]:
	executable = shutil.which("quality-gate", path=environment.get("PATH"))
	if executable is None:
		raise RuntimeError("the installed quality-gate executable is unavailable")
	return subprocess.run(
		[executable, "--root", str(root), "check", *arguments],
		cwd=root,
		env=environment,
		capture_output=True,
		text=True,
		check=False,
	)


def _setup(environment: dict[str, str], root: Path) -> None:
	executable = shutil.which("quality-gate", path=environment.get("PATH"))
	if executable is None:
		raise RuntimeError("the installed quality-gate executable is unavailable")
	result = subprocess.run(
		[executable, "--root", str(root), "setup"],
		cwd=root,
		env=environment,
		capture_output=True,
		text=True,
		check=False,
	)
	if result.returncode:
		raise RuntimeError(result.stdout + result.stderr)


def _surface(output: str) -> dict[str, str]:
	return {
		match.group("check_id"): match.group("status") for match in RESULT_LINE.finditer(output)
	}


def _environment(cache_base: Path) -> dict[str, str]:
	environment = os.environ.copy()
	environment.pop("PYTHONPATH", None)
	environment["PYTHONDONTWRITEBYTECODE"] = "1"
	environment["PYTHONPYCACHEPREFIX"] = str(cache_base / "pycache")
	environment["LOCALAPPDATA"] = str(cache_base)
	environment["XDG_CACHE_HOME"] = str(cache_base)
	return environment


def _fixture(root: Path) -> tuple[Path, str, str]:
	fixture = root / "fixture"
	shutil.copytree(
		PROJECT_ROOT / "tests" / "fixtures" / "release-parity",
		fixture,
		ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
	)
	workflow = fixture / ".github" / "workflows" / "quality.yml"
	workflow.parent.mkdir(parents=True)
	shutil.copy2(PROJECT_ROOT / ".github" / "workflows" / "quality.yml", workflow)
	_git(fixture, "init", "-b", "main")
	_git(fixture, "add", ".")
	_git(
		fixture,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"base",
	)
	base = _git(fixture, "rev-parse", "HEAD")
	credential = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"[:36]
	(fixture / "credentials.txt").write_text(f"access={credential}\n", encoding="utf-8")
	_git(fixture, "add", "credentials.txt")
	_git(
		fixture,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"add credential fixture",
	)
	(fixture / "credentials.txt").unlink()
	_git(fixture, "add", "-A")
	_git(
		fixture,
		"-c",
		"user.name=Quality Gate Test",
		"-c",
		"user.email=quality-gate@example.test",
		"commit",
		"-m",
		"remove credential fixture",
	)
	return fixture, base, credential


def _shallow_result(
	root: Path, temporary_root: Path, environment: dict[str, str], base: str
) -> dict[str, Any]:
	shallow = temporary_root / "shallow"
	result = subprocess.run(
		["git", "clone", "--depth", "1", root.resolve().as_uri(), str(shallow)],
		capture_output=True,
		text=True,
		check=False,
	)
	if result.returncode:
		raise RuntimeError(result.stderr.strip() or "shallow clone failed")
	verdict = _gate(environment, shallow, "--base", base, "--head", "HEAD")
	return {
		"exit_code": verdict.returncode,
		"history_verdict": _surface(verdict.stdout).get("secrets.history"),
	}


def _unavailable_cache(selected: Path, manifest: ReleaseManifest, temporary_root: Path) -> Path:
	broken_release = temporary_root / "broken-release"
	shutil.copytree(selected, broken_release)
	tool = next(tool for tool in manifest.tools if tool.name == "gitleaks")
	tool_path = broken_release / tool.path
	tool_path.write_bytes(b"unavailable scanner")
	release_path = broken_release / "release.toml"
	release_text = release_path.read_text(encoding="utf-8").replace(
		tool.sha256, hashlib.sha256(tool_path.read_bytes()).hexdigest()
	)
	release_path.write_text(release_text, encoding="utf-8")
	cache_root = temporary_root / "unavailable-cache" / "quality-gate"
	PolicyCache(cache_root).sync(broken_release, version=manifest.version)
	return cache_root.parent


def build_result() -> dict[str, Any]:
	"""Build one machine-readable result from the release-backed parity fixture."""

	with tempfile.TemporaryDirectory(prefix="quality-gate-parity-") as temporary:
		temporary_root = Path(temporary)
		manifest = tomllib.loads((PROJECT_ROOT / "quality-gate.toml").read_text(encoding="utf-8"))
		version = manifest["quality"]["policy_release"]
		selected = PolicyCache().select(version)
		release = load_release_manifest(selected)
		fixture, base, credential = _fixture(temporary_root)
		fixture_manifest = load_manifest(fixture)
		environment = _environment(PolicyCache().root.parent)
		_setup(environment, fixture)
		pr = _gate(environment, fixture, "--base", base, "--head", "HEAD")
		combined_output = pr.stdout + pr.stderr
		pr_surface = _surface(pr.stdout)
		missing_checks = EXPECTED_PARITY_STATUSES.keys() - pr_surface.keys()
		extra_checks = pr_surface.keys() - EXPECTED_PARITY_STATUSES.keys()
		if missing_checks or extra_checks:
			raise RuntimeError(
				"parity fixture check surface mismatch: "
				f"missing={sorted(missing_checks)}, extra={sorted(extra_checks)}"
			)
		unexpected_statuses = {
			check_id: status
			for check_id, status in pr_surface.items()
			if EXPECTED_PARITY_STATUSES[check_id] != status
		}
		if unexpected_statuses:
			raise RuntimeError(f"parity fixture has unexpected verdicts: {unexpected_statuses}")
		if pr_surface.get("secrets.history") != "failed":
			raise RuntimeError("release-backed fixture did not run the history scanner")
		shallow = _shallow_result(fixture, temporary_root, environment, base)
		broken_cache = _unavailable_cache(selected, release, temporary_root)
		broken_environment = _environment(broken_cache)
		_setup(broken_environment, fixture)
		unavailable = _gate(broken_environment, fixture, "--base", "HEAD", "--head", "HEAD")
		return {
			"policy_release": release.version,
			"tool_names_versions": [[tool.name, tool.version] for tool in release.tools],
			"supplemental_tests": [
				{
					"name": test.name,
					"runner": test.runner,
					"targets": list(test.targets),
				}
				for test in fixture_manifest.supplemental_tests
			],
			"check_surface": pr_surface,
			"pr_history_verdict": pr_surface["secrets.history"],
			"redaction": {
				"full_secret_absent": credential not in combined_output,
				"meaningful_substring_absent": credential[4:20] not in combined_output,
			},
			"shallow_history": shallow,
			"unavailable_scanner": {
				"exit_code": unavailable.returncode,
				"candidate": _surface(unavailable.stdout).get("secrets.candidate"),
				"history": _surface(unavailable.stdout).get("secrets.history"),
			},
		}


def compare_results(left_path: Path, right_path: Path) -> None:
	"""Compare two platform results and reject any contract difference."""

	left = json.loads(left_path.read_text(encoding="utf-8"))
	right = json.loads(right_path.read_text(encoding="utf-8"))
	keys = (
		"policy_release",
		"tool_names_versions",
		"supplemental_tests",
		"check_surface",
		"pr_history_verdict",
		"redaction",
		"shallow_history",
		"unavailable_scanner",
	)
	for key in keys:
		if left.get(key) != right.get(key):
			raise RuntimeError(f"parity mismatch in {key}")
	if left["redaction"] != {
		"full_secret_absent": True,
		"meaningful_substring_absent": True,
	}:
		raise RuntimeError("parity redaction contract failed")
	if left["shallow_history"]["history_verdict"] != "unchecked":
		raise RuntimeError("shallow history must be unchecked")
	if left["unavailable_scanner"]["candidate"] != "unchecked":
		raise RuntimeError("unavailable scanner must be unchecked")


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--output", type=Path)
	parser.add_argument("--compare", nargs=2, type=Path)
	return parser


def main(arguments: list[str] | None = None) -> int:
	"""Run the parity fixture or compare two previously written results."""

	options = _parser().parse_args(arguments)
	try:
		if options.compare:
			compare_results(*options.compare)
			return 0
		result = build_result()
		output = options.output or Path("parity-result.json")
		output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n", encoding="utf-8")
		return 0
	except (OSError, RuntimeError, ValueError, KeyError) as error:
		sys.stderr.write(f"quality-gate parity: unchecked - {error}\n")
		return 2


if __name__ == "__main__":
	raise SystemExit(main())
