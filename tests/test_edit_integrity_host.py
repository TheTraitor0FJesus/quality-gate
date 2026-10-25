"""Exercise the active host JSON route against the candidate CLI source."""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from test_edit_check import prepare_edit_cache
from test_edit_integrity import manifest_text

from quality_gate import __version__
from quality_gate import cli as gate_cli

ENGINE_RELEASE = f"v{__version__}"
POLICY_RELEASE = "v2.4.2"
FAILED_EXIT_CODE = 1
UNCHECKED_EXIT_CODE = 2


def _host_hooks_root() -> Path:
	root = os.environ.get("CODEX_HOME")
	if not root:
		profile = os.environ.get("USERPROFILE") if os.name == "nt" else os.environ.get("HOME")
		if not profile:
			pytest.skip("The active Codex root is unavailable in this test environment")
		root = str(Path(profile) / ".codex")
	hooks = Path(root) / "hooks"
	if not (hooks / "file_integrity" / "check.py").is_file():
		pytest.skip("The active host integrity adapter is not installed in this test environment")
	return hooks


class CandidateHost:
	"""Run host sources while replacing only the immutable-wheel launch boundary."""

	def __init__(
		self,
		tmp_path: Path,
		monkeypatch: pytest.MonkeyPatch,
		*,
		managed: bool = False,
		enabled: list[str] | None = None,
	) -> None:
		self.tmp_path = tmp_path
		self.monkeypatch = monkeypatch
		self.hooks_root = _host_hooks_root()
		monkeypatch.syspath_prepend(str(self.hooks_root))
		self.check = importlib.import_module("file_integrity.check")
		self.bridge = importlib.import_module("file_integrity.provider_bridge")
		self.coordinator = importlib.import_module("_shared.quality_coordinator")
		self.native = importlib.import_module("_shared.native_quality_gate")
		self.settings_path = self._configure_host(enabled or ["integrity"])
		self.cache_home = tmp_path / "cache"
		if os.name == "nt":
			monkeypatch.setenv("LOCALAPPDATA", str(self.cache_home))
		else:
			monkeypatch.setenv("XDG_CACHE_HOME", str(self.cache_home))
		prepare_edit_cache(self.cache_home / "quality-gate", POLICY_RELEASE)
		self.project = tmp_path / "project"
		self.project.mkdir()
		if managed:
			(self.project / "quality-gate.toml").write_text(
				manifest_text(POLICY_RELEASE), encoding="utf-8"
			)
		self.cli_calls = 0
		monkeypatch.chdir(self.project)
		monkeypatch.setattr(self.check, "execute", self._run_bridge)
		monkeypatch.setattr(self.native, "run_released_command", self._run_candidate_cli)

	def _configure_host(self, enabled: list[str]) -> Path:
		codex_root = self.tmp_path / "codex"
		settings_path = codex_root / "hooks" / "_shared" / "python-quality.json"
		settings_path.parent.mkdir(parents=True)
		selection: dict[str, object] = {
			"baseline_release": POLICY_RELEASE,
			"provider_release": ENGINE_RELEASE,
			"enabled": enabled,
			"budget_seconds": 10,
		}
		if "ruff" in enabled:
			selection.update(
				baseline_python=sys.executable,
				baseline_python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
			)
		settings_path.write_text(
			json.dumps(
				{
					"managed_by": "codex-python-quality-hooks",
					"schema": 1,
					"current": selection,
					"previous": None,
				}
			),
			encoding="utf-8",
		)
		self.monkeypatch.setenv("CODEX_HOME", str(codex_root))
		return settings_path

	def _run_candidate_cli(
		self,
		_repository: Path,
		arguments: list[str],
		*,
		manifest: bytes,
		capture_output: bool = False,
	) -> SimpleNamespace:
		assert arguments == ["edit-integrity"]
		assert f'policy_release = "{ENGINE_RELEASE}"'.encode() in manifest
		self.cli_calls += 1
		request = sys.stdin.buffer.read()
		result = self._invoke_gate_cli(arguments, request)
		if not capture_output:
			sys.stdout.write(result.stdout)
			sys.stderr.write(result.stderr)
		return result

	def _invoke_gate_cli(self, arguments: list[str], request: bytes) -> SimpleNamespace:
		stdout_buffer, stderr_buffer = io.BytesIO(), io.BytesIO()
		stdout = io.TextIOWrapper(stdout_buffer, encoding="utf-8")
		stderr = io.TextIOWrapper(stderr_buffer, encoding="utf-8")
		stdin = io.TextIOWrapper(io.BytesIO(request), encoding="utf-8")
		with (
			patch.object(sys, "argv", ["quality-gate", *arguments]),
			patch.object(sys, "stdin", stdin),
			patch.object(sys, "stdout", stdout),
			patch.object(sys, "stderr", stderr),
		):
			returncode = gate_cli.main()
			stdout.flush()
			stderr.flush()
		return SimpleNamespace(
			returncode=returncode,
			stdout=stdout_buffer.getvalue().decode("utf-8"),
			stderr=stderr_buffer.getvalue().decode("utf-8"),
		)

	def _run_bridge(
		self, command: list[str], *, deadline: float, input_text: str
	) -> SimpleNamespace:
		assert deadline > 0
		stdout, stderr = io.StringIO(), io.StringIO()
		stdin = io.TextIOWrapper(io.BytesIO(input_text.encode("utf-8")), encoding="utf-8")
		with (
			patch.object(sys, "argv", [command[2], *command[3:]]),
			patch.object(sys, "stdin", stdin),
			contextlib.redirect_stdout(stdout),
			contextlib.redirect_stderr(stderr),
		):
			returncode = self.bridge.main()
		return SimpleNamespace(
			returncode=returncode, stdout=stdout.getvalue(), stderr=stderr.getvalue()
		)

	def invoke_host(self, payload: bytes) -> tuple[int, dict[str, object]]:
		stdout = io.StringIO()
		stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")
		with patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(stdout):
			returncode = self.check.main()
		return returncode, json.loads(stdout.getvalue())

	def invoke_coordinator(
		self, event: dict[str, object], *, ruff_writer: object
	) -> dict[str, object]:
		stdout = io.StringIO()
		stdin = io.TextIOWrapper(io.BytesIO(json.dumps(event).encode("utf-8")), encoding="utf-8")
		arguments = [
			"quality_coordinator.py",
			"--settings",
			str(self.settings_path),
			"--state-dir",
			str(self.tmp_path / "state"),
			"--evidence-dir",
			str(self.tmp_path / "evidence"),
		]
		with (
			patch("python_quality.check.check_files", side_effect=ruff_writer),
			patch.object(
				self.coordinator,
				"legacy_security",
				return_value={"check": "legacy_security", "status": "passed", "files": []},
			),
			patch.object(sys, "argv", arguments),
			patch.object(sys, "stdin", stdin),
			contextlib.redirect_stdout(stdout),
		):
			self.coordinator.main()
		return json.loads(stdout.getvalue())


@pytest.mark.parametrize("managed", [False, True])
def test_host_json_boundary_runs_candidate_public_cli(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: bool
) -> None:
	"""Only the unavailable immutable-wheel launcher is replaced.

	The installed host JSON entrypoint and bridge run against the candidate
	public CLI and Gate implementation from this checkout.
	"""
	assert ENGINE_RELEASE == "v4.1.0"
	host = CandidateHost(tmp_path, monkeypatch, managed=managed)
	conflict = b"<<<<<<< HEAD\nlocal\n=======\nremote\n>>>>>>> branch\n"
	clean = b"no conflict here\n"
	(host.project / "merge.py").write_bytes(conflict)
	(host.project / "clean.txt").write_bytes(clean)
	failed_code, failed = host.invoke_host(_host_request(host.project, "merge.py", "clean.txt"))
	assert failed_code == FAILED_EXIT_CODE
	assert failed == {
		"check": "integrity",
		"status": "failed",
		"findings": [
			{"path": "merge.py", "line": 1},
			{"path": "merge.py", "line": 3},
			{"path": "merge.py", "line": 5},
		],
	}
	clean_code, clean_report = host.invoke_host(_host_request(host.project, "clean.txt"))
	assert clean_code == 0
	assert clean_report == {"check": "integrity", "status": "passed", "findings": []}
	assert host.cli_calls == len((failed, clean_report))
	assert not list(host.project.glob(".quality-gate-edit-*"))


def _host_request(project: Path, *paths: str) -> bytes:
	files = [
		{
			"path": path,
			"sha256": hashlib.sha256((project / path).read_bytes()).hexdigest(),
		}
		for path in paths
	]
	return json.dumps({"schema": 1, "root": str(project), "files": files}).encode("utf-8")


def test_host_json_boundary_rejects_malformed_request_without_provider_call(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	host = CandidateHost(tmp_path, monkeypatch)
	code, result = host.invoke_host(b"{malformed")
	assert code == UNCHECKED_EXIT_CODE
	assert result == {
		"check": "integrity",
		"status": "unchecked",
		"reason": "invalid_request",
		"findings": [],
	}
	assert host.cli_calls == 0


def test_coordinator_scans_post_ruff_rename_destination_and_omits_deletion(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	host = CandidateHost(tmp_path, monkeypatch, enabled=["ruff", "integrity"])
	(host.project / "renamed.py").write_bytes(b"value = 1\n")
	(host.project / "gone.py").write_bytes(b"old value\n")
	(host.project / "gone.py").unlink()
	command = "\n".join(
		[
			"*** Begin Patch",
			"*** Update File: old.py",
			"*** Move to: renamed.py",
			"@@",
			"-value = 0",
			"+value = 1",
			"*** Delete File: gone.py",
			"*** End Patch",
		]
	)
	event: dict[str, object] = {
		"hook_event_name": "PostToolUse",
		"tool_name": "apply_patch",
		"cwd": str(host.project),
		"tool_use_id": "candidate-host-event",
		"tool_input": {"command": command, "workdir": str(host.project)},
		"tool_response": "Success. Updated the following files:\nM renamed.py\nD gone.py\n",
	}

	def write_after_ruff(root: Path, files: list[dict[str, str]], *_args: object) -> dict[str, str]:
		assert [item["path"] for item in files] == ["renamed.py"]
		(root / "renamed.py").write_bytes(b"<<<<<<< HEAD\npost-Ruff\n=======\nother\n")
		return {"status": "passed"}

	response = host.invoke_coordinator(event, ruff_writer=write_after_ruff)
	hook_output = response.get("hookSpecificOutput")
	assert isinstance(hook_output, dict)
	context = hook_output.get("additionalContext")
	assert isinstance(context, str)
	assert "renamed.py:1 repository.git.conflict_markers" in context
	assert "post-Ruff" not in json.dumps(response)
	assert "gone.py" not in context
	assert host.cli_calls == 1
