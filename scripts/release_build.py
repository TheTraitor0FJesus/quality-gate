"""Run the existing exact-platform build, installation, audit, and full-test verification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from quality_gate.publication_artifacts import load_json_record
from quality_gate.temp_workspace import temporary_workspace
from scripts.release_metadata import load_release_timeout


def _write_json(path: Path, value: object) -> None:
	temporary = path.with_suffix(path.suffix + ".tmp")
	temporary.write_text(json.dumps(value) + "\n", encoding="utf-8")
	temporary.replace(path)


def _run_release_build() -> None:

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
	subprocess_timeout = load_release_timeout(root, "subprocess_seconds")
	output = (
		Path(os.environ["RUNNER_TEMP"]) / "quality-gate-release" / os.environ["RELEASE_PLATFORM"]
	)
	output.mkdir(parents=True, exist_ok=True)
	subprocess.run(
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
		check=True,
		timeout=subprocess_timeout,
	)
	subprocess.run(
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
		check=True,
		timeout=subprocess_timeout,
	)
	subprocess.run(
		[sys.executable, "-m", "pip", "install", "--no-build-isolation", "--no-deps", "-e", "."],
		check=True,
		timeout=subprocess_timeout,
	)
	with tempfile.TemporaryFile() as build_output:
		subprocess.run(
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
			check=True,
			stdout=build_output,
			timeout=subprocess_timeout,
		)
		build_output.seek(0)
		identity = load_json_record(build_output)
	subprocess.run(
		[
			sys.executable,
			"scripts/release_adapter.py",
			"runtime-check",
			"--source-sha",
			os.environ["SOURCE_SHA"],
			"--artifact",
			identity["path"],
		],
		check=True,
		timeout=subprocess_timeout,
	)
	cache = Path(os.environ["RUNNER_TEMP"]) / "quality-gate-cache" / "quality-gate"
	subprocess.run(
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
		check=True,
		timeout=subprocess_timeout,
	)
	subprocess.run(
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
		check=True,
		timeout=subprocess_timeout,
	)
	test_environment = os.environ.copy()
	# Tests create disposable repositories whose history is unrelated to the release PR.
	for variable in ("GITHUB_EVENT_PATH", "GITHUB_BASE_REF", "GITHUB_SHA"):
		test_environment.pop(variable, None)
	cache_variable = "LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME"
	test_environment[cache_variable] = str(cache.parent)
	test_environment["QUALITY_GATE_POLICY_ROOT"] = str(
		cache / "releases" / f"v{identity['version']}"
	)
	subprocess.run(
		[
			sys.executable,
			"-m",
			"pytest",
			"tests",
			"-p",
			"no:cacheprovider",
			"-q",
			"--tb=long",
			"-ra",
		],
		check=True,
		env=test_environment,
		timeout=subprocess_timeout,
	)
	package = Path(identity["path"])
	published_package = output / package.name
	if package.resolve() != published_package.resolve():
		import shutil

		shutil.copy2(package, published_package)
	identity["path"] = str(published_package)
	_write_json(output / "artifact.json", identity)

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


def main() -> None:
	with temporary_workspace(Path.cwd()):
		_run_release_build()


if __name__ == "__main__":
	main()
