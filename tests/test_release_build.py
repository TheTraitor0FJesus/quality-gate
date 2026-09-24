"""Release build process boundaries and evidence after full-suite verification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from quality_gate.contracts import load_manifest
from scripts import release_build


@pytest.mark.parametrize("test_exit_code", [0, 1])
def test_release_build_isolates_test_history_and_preserves_evidence(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, test_exit_code: int
) -> None:
	source_sha = "a" * 40
	provider_sha = "b" * 40
	candidate = {"source_sha": source_sha, "pr": 27}
	candidate_bytes = (json.dumps(candidate, sort_keys=True, separators=(",", ":")) + "\n").encode()
	candidate_path = tmp_path / "candidate.json"
	candidate_path.write_bytes(candidate_bytes)
	event_path = tmp_path / "event.json"
	event_path.write_text(
		json.dumps({"pull_request": {"base": {"sha": "c" * 40}, "head": {"sha": "d" * 40}}}),
		encoding="utf-8",
	)
	context = {
		"CANDIDATE_PATH": str(candidate_path),
		"CANDIDATE_DIGEST": hashlib.sha256(candidate_bytes).hexdigest(),
		"SOURCE_SHA": source_sha,
		"PUBLISHER_SHA": provider_sha,
		"RUNNER_TEMP": str(tmp_path),
		"RELEASE_PLATFORM": "windows" if os.name == "nt" else "linux",
		"GITHUB_REPOSITORY": "owner/quality-gate",
		"GITHUB_RUN_ID": "123",
		"GITHUB_RUN_ATTEMPT": "2",
		"GITHUB_EVENT_PATH": str(event_path),
		"GITHUB_BASE_REF": "main",
		"GITHUB_SHA": "d" * 40,
	}
	for key, value in context.items():
		monkeypatch.setenv(key, value)
	package = tmp_path / "quality-gate-v2.2.0.zip"
	package.write_bytes(b"verified archive from the product adapter")
	identity = {
		"path": str(package),
		"name": package.name,
		"version": "2.2.0",
		"source_sha": source_sha,
		"sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
	}
	test_environments: list[dict[str, str]] = []
	commands: list[list[str]] = []

	# External build/install/test processes are controlled; the orchestration and files are real.
	def run(command: list[str], **options: object) -> subprocess.CompletedProcess[str]:
		commands.append(list(command))
		if "build" in command:
			cast(BinaryIO, options["stdout"]).write(json.dumps(identity).encode())
		if "pytest" in command:
			test_environments.append(dict(cast(dict[str, str], options["env"])))
			if test_exit_code:
				raise subprocess.CalledProcessError(test_exit_code, command)
		return subprocess.CompletedProcess(command, 0)

	monkeypatch.setattr(subprocess, "run", run)
	if test_exit_code:
		with pytest.raises(subprocess.CalledProcessError):
			release_build.main()
	else:
		release_build.main()

	pinned_release = load_manifest(Path.cwd()).policy_release
	candidate_syncs = [
		command for command in commands if "sync" in command and "--source" in command
	]
	pinned_syncs = [command for command in commands if "sync" in command and "--url" in command]
	assert len(candidate_syncs) == 1
	candidate_sync = candidate_syncs[0]
	candidate_release = f"v{identity['version']}"
	assert candidate_sync[candidate_sync.index("--source") + 1] == identity["path"]
	assert candidate_sync[candidate_sync.index("--version") + 1] == candidate_release
	assert candidate_sync[candidate_sync.index("--cache-dir") + 1] == str(
		tmp_path / "quality-gate-cache" / "quality-gate"
	)
	setup_index = next(index for index, command in enumerate(commands) if "setup" in command)
	assert commands.index(candidate_sync) < setup_index
	if pinned_release == candidate_release:
		assert not pinned_syncs
	else:
		assert len(pinned_syncs) == 1
		pinned_sync = pinned_syncs[0]
		platform = context["RELEASE_PLATFORM"].title()
		asset_name = f"quality-gate-{pinned_release}-{platform}.zip"
		asset_url = (
			f"https://github.com/{context['GITHUB_REPOSITORY']}/releases/download/"
			f"{pinned_release}/{asset_name}"
		)
		assert pinned_sync[pinned_sync.index("--url") + 1] == asset_url
		assert pinned_sync[pinned_sync.index("--version") + 1] == pinned_release
		assert pinned_sync[pinned_sync.index("--cache-dir") + 1] == str(
			tmp_path / "quality-gate-cache" / "quality-gate"
		)
		assert commands.index(candidate_sync) < commands.index(pinned_sync) < setup_index

	assert len(test_environments) == 1
	test_environment = test_environments[0]
	for variable in ("GITHUB_EVENT_PATH", "GITHUB_BASE_REF", "GITHUB_SHA"):
		assert variable not in test_environment
		assert os.environ[variable] == context[variable]
	cache = tmp_path / "quality-gate-cache"
	cache_variable = "LOCALAPPDATA" if os.name == "nt" else "XDG_CACHE_HOME"
	assert Path(test_environment[cache_variable]) == cache
	assert (
		Path(test_environment["QUALITY_GATE_POLICY_ROOT"]) == cache / "quality-gate/releases/v2.2.0"
	)
	output = tmp_path / "quality-gate-release" / context["RELEASE_PLATFORM"]
	if test_exit_code:
		assert not (output / "evidence.json").exists()
		assert not (output / "artifact.json").exists()
	else:
		evidence = json.loads((output / "evidence.json").read_text(encoding="utf-8"))
		assert evidence["repository"] == "owner/quality-gate"
		assert evidence["source_sha"] == source_sha
		assert evidence["provider_sha"] == provider_sha
		assert evidence["run_id"] == 123
		assert evidence["attempt"] == 2
		assert (output / package.name).read_bytes() == package.read_bytes()
