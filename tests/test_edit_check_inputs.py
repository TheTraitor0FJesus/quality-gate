"""Working-tree edit-check contract tests."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_edit_check import (
	FRESH_PROCESSES,
	REUSED_PROCESSES,
	TIMEOUT_WITH_CLEANUP_SECONDS,
	digest,
	invoke,
	managed,
	process_alive,
	request,
	runtime_wrapper,
)

from quality_gate.contracts import load_manifest
from quality_gate.runtime import RuntimeManager, runtime_identity

pytest_plugins = ("test_edit_check",)


def test_version_probe_never_runs_installed_startup_hooks(tmp_path: Path, edit_cache: Path) -> None:
	managed(tmp_path, edit_cache)
	manifest = load_manifest(tmp_path)
	manager = RuntimeManager(edit_cache)
	runtime = manager.path_for(tmp_path, runtime_identity(tmp_path, manifest, manifest.python[0]))
	site = runtime / (
		"Lib/site-packages"
		if os.name == "nt"
		else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
	)
	marker = tmp_path / "startup-executed.txt"
	(site / "edit_probe_side_effect.pth").write_text(
		f"import pathlib; pathlib.Path({str(marker)!r}).write_bytes(b'executed')\n",
		encoding="utf-8",
	)
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py"))
	assert exit_code == 0, result
	assert not marker.exists(), "Version probing must not execute installed project startup hooks"


@pytest.mark.skipif(os.name == "nt", reason="POSIX Python executable symlink layout")
def test_baseline_interpreter_symlink_is_supported(tmp_path: Path, edit_cache: Path) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	python = tmp_path / "python-link"
	python.symlink_to(sys.executable)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(python)
	exit_code, result = invoke(payload)
	assert exit_code == 0, result
	assert result["selections"][0]["python"] == str(python)
	assert result["selections"][0]["python_sha256"] == digest(Path(sys.executable).read_bytes())


def test_interpreter_change_during_probe_invalidates_result(
	tmp_path: Path, edit_cache: Path
) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	python = tmp_path / ("runtime.cmd" if os.name == "nt" else "runtime")
	response = json.dumps(
		{"version": f"{sys.version_info.major}.{sys.version_info.minor}", "executable": str(python)}
	)
	runtime_wrapper(
		tmp_path,
		f"from pathlib import Path\nPath({str(python)!r}).write_bytes(b'changed\\n')\n"
		f"print({response!r})\n",
	)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(python)
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, "stale_input"), result
	assert (tmp_path / "example.py").read_bytes() == b"value=  1\n"


def test_working_tree_is_checked_without_reading_or_rewriting_index(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	staged = b"value = 1\n"
	path.write_bytes(staged)
	for arguments in (["init"], ["add", "example.py"]):
		process = subprocess.run(
			["git", "-C", str(tmp_path), *arguments], capture_output=True, check=False
		)
		assert process.returncode == 0
	index_before = (tmp_path / ".git/index").read_bytes()
	path.write_bytes(b"import os\nvalue=  2\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0, result
	assert result["files"][0]["findings"] == []
	assert path.read_bytes() == b"value = 2\n"
	assert (tmp_path / ".git/index").read_bytes() == index_before
	process = subprocess.run(
		["git", "-C", str(tmp_path), "show", ":example.py"], capture_output=True, check=False
	)
	assert process.returncode == 0
	assert process.stdout == staged


def test_empty_non_python_duplicate_and_policy_excluded_inputs(
	tmp_path: Path, edit_cache: Path
) -> None:
	assert invoke(request(tmp_path, edit_cache))[1]["status"] == "not_applicable"
	(tmp_path / "text.txt").write_bytes(b"ordinary text\n")
	assert invoke(request(tmp_path, edit_cache, "text.txt"))[1]["status"] == "not_applicable"
	(tmp_path / ".venv").mkdir()
	path = tmp_path / ".venv/example.py"
	path.write_bytes(b"value=  1\n")
	exit_code, result = invoke(
		request(tmp_path, edit_cache, ".venv/example.py", ".venv/example.py")
	)
	assert exit_code == 0
	assert result["status"] == "not_applicable"
	assert len(result["files"]) == 1
	assert path.read_bytes() == b"value=  1\n"


@pytest.mark.parametrize(
	"failure", ["escape", "directory", "deleted", "file_size", "count", "configuration", "findings"]
)
def test_bounded_input_failures_never_publish(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"value=  1\n")
	payload = request(tmp_path, edit_cache, "example.py")
	expected = "limits"
	if failure == "escape":
		payload["files"] = [{"path": "../escape.py", "sha256": "0" * 64}]
		expected = "unsafe_path"
	elif failure == "directory":
		payload["files"] = [{"path": "directory", "sha256": "0" * 64}]
		(tmp_path / "directory").mkdir()
		expected = "unsafe_path"
	elif failure == "deleted":
		path.unlink()
		expected = "io"
	elif failure == "file_size":
		path.write_bytes(b"#" * (128 * 1024 + 1))
		payload = request(tmp_path, edit_cache, "example.py")
	elif failure == "count":
		payload["files"] *= 11
	elif failure == "configuration":
		(tmp_path / "pyproject.toml").write_bytes(b"#" * (256 * 1024 + 1))
	elif failure == "findings":
		path.write_bytes(b"assert True\n" * 201)
		payload = request(tmp_path, edit_cache, "example.py")
	before = path.read_bytes() if path.exists() else None
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, expected), result
	assert (path.read_bytes() if path.exists() else None) == before


def test_syntax_error_is_redacted_and_does_not_prevent_other_file_formatting(
	tmp_path: Path, edit_cache: Path
) -> None:
	(tmp_path / "broken.py").write_bytes(b'value = "synthetic-sensitive-value\n')
	(tmp_path / "valid.py").write_bytes(b"value=  1\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "broken.py", "valid.py"))
	assert exit_code == 1, (result.get("reason"), result.get("stage"))
	assert result["files"][0]["status"] == "failed"
	assert result["files"][1]["format"] == "passed"
	assert (tmp_path / "valid.py").read_bytes() == b"value = 1\n"
	assert "synthetic-sensitive-value" not in json.dumps(result)


@pytest.mark.parametrize(
	"failure",
	["release", "tool", "python", "version", "missing_baseline", "config", "missing_release"],
)
def test_missing_and_corrupt_prerequisites_are_unchecked(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	payload = request(tmp_path, edit_cache, "example.py")
	expected = "runtime"
	cache = tmp_path / "cache"
	if failure in {"release", "tool"}:
		shutil.copytree(edit_cache / "releases", cache / "releases")
		shutil.copytree(edit_cache / "edit-tools", cache / "edit-tools")
		payload["cache_dir"] = str(cache)
	exit_code, fresh = invoke(payload)
	assert exit_code == 0 and fresh["process_count"] == FRESH_PROCESSES, fresh
	payload["files"][0]["sha256"] = digest((tmp_path / "example.py").read_bytes())
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 10_000
	exit_code, reused = invoke(payload)
	assert exit_code == 0 and reused["process_count"] == REUSED_PROCESSES, reused
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 10_000
	if failure == "missing_release":
		payload["cache_dir"] = str(tmp_path / "missing-cache")
		expected = "release"
	elif failure in {"release", "tool"}:
		if failure == "release":
			(cache / "releases/v2.4.2/quality_gate/policy/ruff.toml").write_bytes(b"corrupt")
		else:
			next((cache / "edit-tools").glob("*/*")).write_bytes(b"corrupt")
		expected = failure
	elif failure == "python":
		payload["baseline"]["python"] = str(tmp_path / "missing-python")
	elif failure == "version":
		payload["baseline"]["python_version"] = "3.99"
	elif failure == "missing_baseline":
		del payload["baseline"]
		expected = "invalid_request"
	elif failure == "config":
		(tmp_path / "pyproject.toml").write_text(
			'[[secret = "synthetic-sensitive-value"', encoding="utf-8"
		)
		expected = "configuration"
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, expected), result
	assert (tmp_path / "example.py").read_bytes() == b"value = 1\n"
	assert "synthetic-sensitive-value" not in json.dumps(result)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permission bits")
def test_nonexecutable_tool_invalidates_a_cached_verdict(tmp_path: Path, edit_cache: Path) -> None:
	cache = tmp_path / "cache"
	shutil.copytree(edit_cache / "releases", cache / "releases")
	shutil.copytree(edit_cache / "edit-tools", cache / "edit-tools")
	path = tmp_path / "example.py"
	path.write_bytes(b"value = 1\n")
	for expected_processes in (FRESH_PROCESSES, REUSED_PROCESSES):
		exit_code, result = invoke(request(tmp_path, cache, "example.py"))
		assert exit_code == 0 and result["process_count"] == expected_processes, result
	next((cache / "edit-tools").glob("*/*")).chmod(0o600)
	exit_code, result = invoke(request(tmp_path, cache, "example.py"))
	assert (exit_code, result["reason"]) == (2, "tool"), result
	assert path.read_bytes() == b"value = 1\n"


def test_deadline_terminates_owned_runtime_descendants(tmp_path: Path, edit_cache: Path) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	marker = tmp_path / "child.pid"
	wrapper = runtime_wrapper(
		tmp_path,
		f"""import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
Path({str(marker)!r}).write_text(str(child.pid))
time.sleep(30)
""",
	)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(wrapper)
	deadline_seconds = 5
	payload["deadline_unix_ms"] = int(time.time() * 1000) + deadline_seconds * 1000
	started = time.monotonic()
	exit_code, result = invoke(payload)
	assert marker.exists(), ("The owned descendant must have started before timeout", result)
	pid = int(marker.read_text())
	try:
		assert (exit_code, result["reason"]) == (2, "deadline")
		assert time.monotonic() - started < deadline_seconds + TIMEOUT_WITH_CLEANUP_SECONDS
		assert not process_alive(pid)
		assert (tmp_path / "example.py").read_bytes() == b"value=  1\n"
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		if process_alive(pid):
			os.kill(pid, signal.SIGTERM)
