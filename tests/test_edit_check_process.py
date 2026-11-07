"""Working-tree edit-check contract tests."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from test_edit_check import (
	EXIT_UNCHECKED,
	REPOSITORY,
	digest,
	invoke,
	request,
	runtime_wrapper,
)

pytest_plugins = ("test_edit_check",)


@pytest.mark.parametrize(
	"failure", ["json", "output_limit", "source_changed", "configuration_changed"]
)
def test_runtime_boundary_and_freshness_fail_closed(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	(tmp_path / "pyproject.toml").write_bytes(b"# original\n")
	wrapper_name = "runtime.cmd" if os.name == "nt" else "runtime"
	response = json.dumps(
		{
			"version": f"{sys.version_info.major}.{sys.version_info.minor}",
			"executable": str(tmp_path / wrapper_name),
		}
	)
	body = f"print({response!r})\n"
	expected = "stale_input"
	if failure == "json":
		body = "print('synthetic-sensitive-value')\n"
		expected = "runtime"
	elif failure == "output_limit":
		body = "print('x' * (512 * 1024 + 1))\n"
		expected = "limits"
	else:
		target = "example.py" if failure == "source_changed" else "pyproject.toml"
		body = (
			f"from pathlib import Path\nPath({str(tmp_path / target)!r})"
			".write_bytes(b'# concurrent change\\n')\n" + body
		)
	wrapper = runtime_wrapper(tmp_path, body)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(wrapper)
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, expected), result
	assert "synthetic-sensitive-value" not in json.dumps(result)
	assert (tmp_path / "example.py").read_bytes() == (
		b"# concurrent change\n" if failure == "source_changed" else b"value=  1\n"
	)


def test_policy_change_during_check_invalidates_result(tmp_path: Path, edit_cache: Path) -> None:
	cache = tmp_path / "cache"
	shutil.copytree(edit_cache / "releases", cache / "releases")
	shutil.copytree(edit_cache / "edit-tools", cache / "edit-tools")
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	policy = cache / "releases/v2.4.2/quality_gate/policy/ruff.toml"
	wrapper = tmp_path / ("runtime.cmd" if os.name == "nt" else "runtime")
	response = json.dumps(
		{
			"version": f"{sys.version_info.major}.{sys.version_info.minor}",
			"executable": str(wrapper),
		}
	)
	runtime_wrapper(
		tmp_path,
		f"from pathlib import Path\npolicy = Path({str(policy)!r})\n"
		"policy.write_bytes(policy.read_bytes() + b'\\n# changed concurrently\\n')\n"
		f"print({response!r})\n",
	)
	payload = request(tmp_path, cache, "example.py")
	payload["baseline"]["python"] = str(wrapper)
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, "stale_input"), result
	assert (tmp_path / "example.py").read_bytes() == b"value=  1\n"


@pytest.mark.parametrize("damage", ["missing", "wrong_digest"])
def test_setup_verifies_the_pinned_wheel_native_record(
	tmp_path: Path, edit_cache: Path, damage: str
) -> None:
	cache = tmp_path / "cache"
	shutil.copytree(edit_cache / "releases", cache / "releases")
	wheel = next((cache / "releases/v2.4.2").glob("ruff-*.whl"))
	before = digest(wheel.read_bytes())
	with zipfile.ZipFile(wheel) as archive:
		members = {item.filename: archive.read(item) for item in archive.infolist()}
	record = next(name for name in members if name.endswith(".dist-info/RECORD"))
	if damage == "missing":
		del members[record]
	else:
		lines = members[record].decode().splitlines()
		members[record] = (
			"\n".join(
				line.split(",")[0] + ",sha256=" + "A" * 43 + "," + line.split(",")[2]
				if line.split(",")[0].endswith(("/ruff", "/ruff.exe"))
				else line
				for line in lines
			)
			+ "\n"
		).encode()
	with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
		for name, content in members.items():
			archive.writestr(name, content)
	manifest = cache / "releases/v2.4.2/release.toml"
	manifest.write_text(
		manifest.read_text().replace(before, digest(wheel.read_bytes())), encoding="utf-8"
	)
	process = subprocess.run(
		[
			sys.executable,
			"-B",
			"-m",
			"quality_gate.cli",
			"edit-setup",
			"--policy-release",
			"v2.4.2",
			"--cache-dir",
			str(cache),
		],
		cwd=REPOSITORY,
		capture_output=True,
		check=False,
		timeout=15,
	)
	assert process.returncode == EXIT_UNCHECKED
	assert b"unchecked" in process.stdout
	assert process.stderr == b""


def test_result_delivery_obeys_the_same_deadline(tmp_path: Path, edit_cache: Path) -> None:
	(tmp_path / "example.py").write_bytes(b"assert True\n" * 180)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 4000
	process = subprocess.Popen(
		[sys.executable, "-B", "-m", "quality_gate.cli", "edit-check"],
		cwd=REPOSITORY,
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		pipesize=4096 if sys.platform == "linux" else -1,
	)
	assert process.stdin is not None
	process.stdin.write(json.dumps(payload).encode())
	process.stdin.close()
	try:
		try:
			exit_code = process.wait(timeout=6)
		except subprocess.TimeoutExpired:
			pytest.fail("Result delivery blocked beyond the invocation deadline")
		assert exit_code == EXIT_UNCHECKED
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		if process.poll() is None:
			process.kill()
		process.wait(timeout=2)
		if process.stdout is not None:
			process.stdout.close()
		if process.stderr is not None:
			process.stderr.close()


def test_unicode_spaces_duplicate_and_noop_preserve_content_identity(
	tmp_path: Path, edit_cache: Path
) -> None:
	root = tmp_path / "проект с пробелами"
	root.mkdir()
	path = root / "example.py"
	path.write_bytes(b"value = 1\n")
	before = path.stat().st_mtime_ns
	exit_code, result = invoke(request(root, edit_cache, "example.py", "example.py"))
	assert exit_code == 0, result
	assert len(result["files"]) == 1
	assert result["files"][0]["changed"] is False
	assert result["files"][0]["sha256"] == digest(b"value = 1\n")
	assert path.stat().st_mtime_ns == before


def test_gitignore_explicit_file_semantics_and_working_tree_identity(
	tmp_path: Path, edit_cache: Path
) -> None:
	process = subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=False)
	assert process.returncode == 0
	path = tmp_path / "example.py"
	path.write_bytes(b"value=  1\n")
	ignore = tmp_path / ".gitignore"
	ignore.write_bytes(b"example.py\n")
	tool = next((edit_cache / "edit-tools").glob("*/ruff.exe" if os.name == "nt" else "*/ruff"))
	discovery = subprocess.run(
		[
			str(tool),
			"check",
			"--config",
			str(edit_cache / "releases/v2.4.2/quality_gate/policy/ruff.toml"),
			"--no-cache",
			"--force-exclude",
			"--show-files",
			"--",
			str(path),
		],
		cwd=tmp_path,
		capture_output=True,
		check=False,
		timeout=10,
	)
	assert discovery.returncode == 0
	assert Path(discovery.stdout.decode().strip()).resolve() == path.resolve()
	exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0
	assert result["status"] == "passed"
	assert path.read_bytes() == b"value = 1\n"
	first_identity = next(
		item["sha256"] for item in result["inputs"] if item["path"] == ".gitignore"
	)
	ignore.write_bytes(b"# no ignored source\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0, result
	assert result["status"] == "passed"
	assert (
		next(item["sha256"] for item in result["inputs"] if item["path"] == ".gitignore")
		!= first_identity
	)
	assert path.read_bytes() == b"value = 1\n"


@pytest.mark.parametrize("baseline", [None, {}, {"policy_release": "v2.4.2"}])
def test_malformed_optional_baseline_is_rejected(tmp_path: Path, baseline: object) -> None:
	payload = {
		"schema": 1,
		"root": str(tmp_path),
		"files": [],
		"deadline_unix_ms": int(time.time() * 1000) + 5000,
		"baseline": baseline,
	}
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, "invalid_request")


def test_junction_or_symlink_escape_is_rejected(tmp_path: Path, edit_cache: Path) -> None:
	outside = tmp_path / "outside"
	outside.mkdir()
	(outside / "example.py").write_bytes(b"value=  1\n")
	root = tmp_path / "project"
	root.mkdir()
	link = root / "link"
	if os.name == "nt":
		junction_result = subprocess.run(
			["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, check=False
		)
		assert junction_result.returncode == 0
	else:
		link.symlink_to(outside, target_is_directory=True)
	try:
		payload = request(root, edit_cache, "link/example.py")
		exit_code, result = invoke(payload)
		assert (exit_code, result["reason"]) == (2, "unsafe_path")
		assert (outside / "example.py").read_bytes() == b"value=  1\n"
	finally:
		if os.name == "nt":
			link.rmdir()
		else:
			link.unlink()
