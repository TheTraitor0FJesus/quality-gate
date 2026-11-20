"""Run the existing exact-platform build, installation, audit, and full-test verification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import BinaryIO

sys.path.insert(0, str(Path.cwd()))
from quality_gate.contracts import load_manifest
from quality_gate.publication_artifacts import load_json_record
from quality_gate.temp_workspace import temporary_workspace
from scripts.release_metadata import ArtifactIdentity, load_release_timeout


def _write_json(path: Path, value: object) -> None:
	temporary = path.with_suffix(path.suffix + ".tmp")
	temporary.write_text(json.dumps(value) + "\n", encoding="utf-8")
	temporary.replace(path)


def _require_clean_checkout(root: Path, timeout: float, expected_sha: str) -> None:
	status = subprocess.run(
		["git", "status", "--porcelain=v1", "--untracked-files=all"],
		cwd=root,
		capture_output=True,
		text=True,
		check=True,
		timeout=timeout,
	)
	head = subprocess.run(
		["git", "rev-parse", "HEAD"],
		cwd=root,
		capture_output=True,
		text=True,
		check=True,
		timeout=timeout,
	)
	if status.stdout.strip() or head.stdout.strip() != expected_sha:
		raise SystemExit("product verification requires a clean exact-head checkout")


@contextmanager
def _stage(name: str) -> Iterator[None]:
	started = time.perf_counter()
	status = "failed"
	try:
		yield
		status = "completed"
	finally:
		print(
			"release-timing: "
			+ json.dumps(
				{
					"stage": name,
					"elapsed_seconds": round(time.perf_counter() - started, 6),
					"status": status,
				}
			),
			flush=True,
		)


def _run_stage(
	name: str,
	command: list[str],
	*,
	timeout: float,
	stdout: BinaryIO | None = None,
	env: dict[str, str] | None = None,
) -> None:
	with _stage(name):
		subprocess.run(command, check=True, timeout=timeout, stdout=stdout, env=env)



def _run_release_build(*, pull_request: bool = False) -> None:

	candidate: dict[str, object] = {}
	candidate_bytes = b""
	if not pull_request:
		candidate_path = Path(os.environ["CANDIDATE_PATH"])
		with candidate_path.open("rb") as candidate_file:
			candidate = load_json_record(candidate_file)
		candidate_bytes = (
			json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
		).encode("utf-8")
		if (
			hashlib.sha256(candidate_bytes).hexdigest() != os.environ["CANDIDATE_DIGEST"]
			or candidate["source_sha"] != os.environ["SOURCE_SHA"]
		):
			raise SystemExit("release: original candidate transfer or source mismatch")

	root = Path.cwd()
	with _stage("suite-contract"):
		manifest = load_manifest(root)
		if "python" not in manifest.repository.domains or not any(
			component.tests_applicable and "tests" in component.test_paths
			for component in manifest.python
		):
			raise SystemExit("release: installed-artifact Gate must cover the complete tests directory")
	subprocess_timeout = load_release_timeout(root, "subprocess_seconds")
	if pull_request:
		_require_clean_checkout(root, subprocess_timeout, os.environ["SOURCE_SHA"])
	output = (
		Path(os.environ["RUNNER_TEMP"]) / "quality-gate-release" / os.environ["RELEASE_PLATFORM"]
	)
	output.mkdir(parents=True, exist_ok=True)
	_run_stage(
		"build-backend",
		[
			sys.executable,
			"-m",
			"pip",
			"install",
			"--require-hashes",
			"--only-binary=:all:",
			"-r",
			".release/build-requirements.txt",
		],
		timeout=subprocess_timeout,
	)
	_run_stage(
		"release-tools",
		[
			sys.executable,
			"-m",
			"pip",
			"install",
			"--require-hashes",
			"--only-binary=:all:",
			"-r",
			".release/release-requirements.txt",
		],
		timeout=subprocess_timeout,
	)
	_run_stage(
		"source-install",
		[sys.executable, "-m", "pip", "install", "--no-build-isolation", "--no-deps", "-e", "."],
		timeout=subprocess_timeout,
	)
	with tempfile.TemporaryFile() as build_output:
		_run_stage(
			"artifact-build",
			[
				sys.executable,
				"scripts/release_adapter.py",
				"build",
				"--source-sha",
				os.environ["SOURCE_SHA"],
				"--platform",
				os.environ["RELEASE_PLATFORM"],
				"--output",
				str(output),
			],
			stdout=build_output,
			timeout=subprocess_timeout,
		)
		build_output.seek(0)
		identity = load_json_record(build_output)
	if pull_request:
		_require_clean_checkout(root, subprocess_timeout, os.environ["SOURCE_SHA"])
		artifact_identity = ArtifactIdentity.from_mapping(identity)
		if (
			artifact_identity.source_sha != os.environ["SOURCE_SHA"]
			or artifact_identity.platform != os.environ["RELEASE_PLATFORM"]
		):
			raise SystemExit("product verification artifact source or platform mismatch")
	verify_command = [
		sys.executable,
		"scripts/release_adapter.py",
		"verify-pr" if pull_request else "runtime-check",
		"--source-sha",
		os.environ["SOURCE_SHA"],
		"--artifact",
		identity["path"],
	]
	if pull_request:
		verify_command.extend(["--sha256", identity["sha256"]])
	_run_stage(
		"artifact-verification",
		verify_command,
		timeout=subprocess_timeout,
	)
	cache = Path(os.environ["RUNNER_TEMP"]) / "quality-gate-cache" / "quality-gate"
	_run_stage(
		"candidate-policy-sync",
		[
			sys.executable,
			"-m",
			"quality_gate",
			"--root",
			str(root),
			"sync",
			"--source",
			identity["path"],
			"--version",
			f"v{identity['version']}",
			"--cache-dir",
			str(cache),
		],
		timeout=subprocess_timeout,
	)
	pinned_release = manifest.policy_release
	candidate_release = f"v{identity['version']}"
	if pinned_release != candidate_release:
		repository = os.environ["GITHUB_REPOSITORY"]
		platform = os.environ["RELEASE_PLATFORM"].title()
		asset_name = f"quality-gate-{pinned_release}-{platform}.zip"
		asset_url = (
			f"https://github.com/{repository}/releases/download/{pinned_release}/{asset_name}"
		)
		_run_stage(
			"pinned-policy-sync",
			[
				sys.executable,
				"-m",
				"quality_gate",
				"--root",
				str(root),
				"sync",
				"--url",
				asset_url,
				"--version",
				pinned_release,
				"--cache-dir",
				str(cache),
			],
			timeout=subprocess_timeout,
		)
	_run_stage(
		"pinned-policy-setup",
		[
			sys.executable,
			"-m",
			"quality_gate",
			"--root",
			str(root),
			"setup",
			"--cache-dir",
			str(cache),
		],
		timeout=subprocess_timeout,
	)
	test_environment = os.environ.copy()
	# Readiness uses the same isolated cache, without release-PR history metadata.
	for variable in ("GITHUB_EVENT_PATH", "GITHUB_BASE_REF", "GITHUB_SHA"):
		test_environment.pop(variable, None)
	cache_variable = "LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME"
	test_environment[cache_variable] = str(cache.parent)
	test_environment["QUALITY_GATE_POLICY_ROOT"] = str(
		cache / "releases" / f"v{identity['version']}"
	)
	_run_stage(
		"pinned-policy-readiness",
		[
			sys.executable,
			"-m",
			"quality_gate",
			"--root",
			str(root),
			"doctor",
			"--cache-dir",
			str(cache),
		],
		env=test_environment,
		timeout=subprocess_timeout,
	)
	if pull_request:
		_require_clean_checkout(root, subprocess_timeout, os.environ["SOURCE_SHA"])
	with _stage("package-finalization"):
		package = Path(identity["path"])
		published_package = output / package.name
		if package.resolve() != published_package.resolve():
			import shutil

			shutil.copy2(package, published_package)
		identity["path"] = str(published_package)
		_write_json(output / "artifact.json", identity)
		if pull_request:
			_write_json(
				output / "verification.json",
				{
					"schema": 1,
					"repository": os.environ["GITHUB_REPOSITORY"],
					"base_sha": os.environ["BASE_SHA"],
					"head_sha": identity["source_sha"],
					"producer": "package",
					"platform": os.environ["RELEASE_PLATFORM"],
					"kind": "artifact",
					"status": "passed",
					"applicability": "applicable",
					"artifact": {
						"name": identity["name"],
						"sha256": identity["sha256"],
						"source_sha": identity["source_sha"],
					},
					"evidence": f"run/{os.environ['GITHUB_RUN_ID']}/attempt/{os.environ['GITHUB_RUN_ATTEMPT']}",
				},
			)
			return

		evidence = {
			"schema": 1,
			"repository": os.environ["GITHUB_REPOSITORY"],
			"pr": candidate["pr"],
			"source_sha": identity["source_sha"],
			"version": identity["version"],
			"provider_sha": os.environ["PUBLISHER_SHA"],
			"run_id": int(os.environ["GITHUB_RUN_ID"]),
			"attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
			"candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
			"deliverables": [
				{"kind": "archive", "name": identity["name"], "sha256": identity["sha256"]}
			],
		}
		_write_json(output / "evidence.json", evidence)



def main(*, pull_request: bool = False) -> None:
	with ExitStack() as workspace:
		with _stage("workspace-setup"):
			workspace.enter_context(temporary_workspace(Path.cwd()))
		try:
			_run_release_build(pull_request=pull_request)
		finally:
			with _stage("workspace-cleanup"):
				workspace.close()


if __name__ == "__main__":
	if sys.argv[1:] not in ([], ["--pull-request"]):
		raise SystemExit("usage: release_build.py [--pull-request]")
	main(pull_request=sys.argv[1:] == ["--pull-request"])
