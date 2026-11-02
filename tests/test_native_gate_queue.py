from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TextIO

import pytest

from quality_gate import admission
from quality_gate.contracts import DEFAULT_GATE_TIMEOUT_SECONDS
from quality_gate.hooks import commit_gate_timeout_seconds

_GATE_TIMEOUT_SECONDS = 3
_QUEUE_TIMEOUT_SECONDS = 3
_NATIVE_DEFAULT_GATE_TIMEOUT_SECONDS = 600
_NATIVE_DEFAULT_QUEUE_TIMEOUT_SECONDS = 120
_NATIVE_MAX_QUEUE_TIMEOUT_SECONDS = 240
_SUCCESS_EXIT = 0
_UNCHECKED_EXIT = 2
_NATIVE_BRIDGE_NAME = "native_quality_gate.py"


def _native_bridge_path() -> Path:
	configured_root = os.environ.get("CODEX_HOME")
	settings_root = Path(configured_root) if configured_root else Path.home() / ".codex"
	return settings_root / "MY-SETTINGS" / "hooks" / _NATIVE_BRIDGE_NAME


def _load_native_bridge() -> tuple[ModuleType, Path]:
	bridge_path = _native_bridge_path()
	if not bridge_path.is_file():
		pytest.skip(f"native Quality Gate bridge is unavailable: {bridge_path}")
	spec = importlib.util.spec_from_file_location("native_quality_gate_queue_test", bridge_path)
	if spec is None or spec.loader is None:
		pytest.skip(f"native Quality Gate bridge cannot be loaded: {bridge_path}")
	bridge = importlib.util.module_from_spec(spec)
	sys.modules[spec.name] = bridge
	spec.loader.exec_module(bridge)
	return bridge, bridge_path


def _write_candidate_wheel(
	project_root: Path,
	selected_release_wheel: Path,
	temporary_release: Path,
	release_version: str,
) -> Path:
	shutil.copytree(selected_release_wheel.parent, temporary_release)
	wheel_version = release_version.removeprefix("v")
	wheel_name = selected_release_wheel.name
	wheel_path = temporary_release / wheel_name
	dist_info = f"quality_gate-{wheel_version}.dist-info"
	entries = {
		path.relative_to(project_root).as_posix(): path.read_bytes()
		for path in sorted((project_root / "quality_gate").rglob("*.py"))
		if "__pycache__" not in path.parts
	}
	with zipfile.ZipFile(selected_release_wheel) as selected_wheel:
		for name in selected_wheel.namelist():
			if name.startswith("quality_gate/policy/") and not name.endswith("/"):
				entries[name] = selected_wheel.read(name)
	entries[f"{dist_info}/METADATA"] = (
		"Metadata-Version: 2.1\n"
		"Name: quality-gate\n"
		f"Version: {wheel_version}\n"
		"Requires-Python: >=3.11\n"
	).encode()
	entries[f"{dist_info}/WHEEL"] = (
		b"Wheel-Version: 1.0\n"
		b"Generator: Quality Gate queue integration test\n"
		b"Root-Is-Purelib: true\n"
		b"Tag: py3-none-any\n"
	)
	record_name = f"{dist_info}/RECORD"
	record_lines = []
	for name, content in sorted(entries.items()):
		digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
		record_lines.append(f"{name},sha256={digest.rstrip(b'=').decode('ascii')},{len(content)}\n")
	record_lines.append(f"{record_name},,\n")
	entries[record_name] = "".join(record_lines).encode("utf-8")
	with zipfile.ZipFile(wheel_path, "w", compression=zipfile.ZIP_DEFLATED) as wheel:
		for name, content in sorted(entries.items()):
			wheel.writestr(name, content)
	digest = hashlib.sha256(wheel_path.read_bytes()).hexdigest()
	release_path = temporary_release / "release.toml"
	release_text = release_path.read_text(encoding="utf-8")
	release = tomllib.loads(release_text)
	previous_digest = next(
		item["sha256"] for item in release["release"]["files"] if item["path"] == wheel_name
	)
	assert release_text.count(previous_digest) == 1
	release_path.write_text(release_text.replace(previous_digest, digest, 1), encoding="utf-8")
	return wheel_path


def _start_slot_holder(
	cache_root: Path, project_root: Path, environment: dict[str, str]
) -> subprocess.Popen[str]:
	code = (
		"from pathlib import Path\n"
		"import sys\n"
		"from quality_gate.admission import gate_admission\n"
		"with gate_admission(cache_root=Path(sys.argv[1])):\n"
		"    print('ready', flush=True)\n"
		"    sys.stdin.readline()\n"
	)
	process = subprocess.Popen(
		[sys.executable, "-B", "-c", code, str(cache_root)],
		cwd=project_root,
		env=environment,
		stdin=subprocess.PIPE,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		encoding="utf-8",
		errors="replace",
		bufsize=1,
	)
	if process.stdout is None:
		process.kill()
		process.wait()
		pytest.fail("queue holder did not expose its readiness stream")
	if process.stdout.readline() != "ready\n":
		stdout, stderr = process.communicate(timeout=5)
		pytest.fail(f"queue holder failed to acquire a slot: {stdout}{stderr}")
	return process


def _release_slot_holder(process: subprocess.Popen[str]) -> int:
	if process.poll() is None and process.stdin is not None:
		process.stdin.write("\n")
		process.stdin.flush()
		process.stdin.close()
		process.stdin = None
	try:
		process.communicate(timeout=5)
	except subprocess.TimeoutExpired:
		process.kill()
		process.communicate(timeout=5)
		raise
	return_code = process.returncode
	assert return_code is not None
	return return_code


@contextmanager
def _occupied_slots(
	cache_root: Path, project_root: Path, environment: dict[str, str]
) -> Iterator[list[subprocess.Popen[str]]]:
	holders: list[subprocess.Popen[str]] = []
	try:
		for _ in range(admission.SLOT_COUNT):
			holders.append(_start_slot_holder(cache_root, project_root, environment))
		yield holders
	finally:
		for holder in holders:
			if holder.poll() is None:
				_release_slot_holder(holder)


def _read_output(
	stream: TextIO,
	channel: str,
	lines: list[str],
	messages: queue.Queue[tuple[str, str | None]],
) -> None:
	for line in stream:
		lines.append(line)
		messages.put((channel, line))
	messages.put((channel, None))


@dataclass(slots=True)
class _NativeLaunch:
	process: subprocess.Popen[str]
	stdout_lines: list[str]
	stderr_lines: list[str]
	readers: tuple[threading.Thread, ...]
	messages: queue.Queue[tuple[str, str | None]]


def _prepare_native_fixture(
	tmp_path: Path,
	project_root: Path,
	bridge: ModuleType,
	monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, int]:
	assert _NATIVE_DEFAULT_GATE_TIMEOUT_SECONDS == DEFAULT_GATE_TIMEOUT_SECONDS
	assert _NATIVE_DEFAULT_QUEUE_TIMEOUT_SECONDS == admission.DEFAULT_QUEUE_TIMEOUT_SECONDS
	assert _NATIVE_MAX_QUEUE_TIMEOUT_SECONDS == admission.MAX_QUEUE_TIMEOUT_SECONDS
	provider_manifest = (project_root / "quality-gate.toml").read_bytes()
	release_version = bridge._policy_release(provider_manifest)
	staged_manifest = f"""[quality]
schema = 2
policy_release = "{release_version}"

[repository]
name = "native-queue-integration"
domains = ["repository"]
required_documents = ["quality-gate.toml"]

[repository.defaults]
gate_timeout_seconds = {_GATE_TIMEOUT_SECONDS}
""".encode()
	try:
		selected_release_wheel = bridge._release_wheel(release_version)
	except (OSError, ValueError) as error:
		pytest.skip(
			f"the staged policy wheel is unavailable for native launch integration: {error}"
		)
	repository = tmp_path / "repository"
	repository.mkdir()
	(repository / "quality-gate.toml").write_bytes(staged_manifest)
	(repository / ".gitignore").write_text("/temp/\n", encoding="utf-8")
	workflow = repository / ".github" / "workflows" / "quality.yml"
	workflow.parent.mkdir(parents=True)
	workflow.write_text(
		"name: Quality gate\non:\n  pull_request:\n  push:\npermissions: read\n"
		"concurrency:\n  group: quality-${{ github.workflow }}-${{ github.ref }}\n"
		"  cancel-in-progress: true\njobs:\n  quality-gate:\n    name: quality-gate\n"
		"    runs-on: ubuntu-latest\n    timeout-minutes: 10\n    steps:\n"
		"      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567\n",
		encoding="utf-8",
	)
	subprocess.run(["git", "init", "--quiet"], cwd=repository, check=True, capture_output=True)
	subprocess.run(
		["git", "add", "--", "quality-gate.toml", ".gitignore", ".github"],
		cwd=repository,
		check=True,
		capture_output=True,
	)
	actual_staged_manifest = bridge._staged_manifest(repository)
	assert actual_staged_manifest == staged_manifest
	assert actual_staged_manifest is not None
	working_manifest = staged_manifest.replace(
		f"gate_timeout_seconds = {_GATE_TIMEOUT_SECONDS}".encode("ascii"),
		f"gate_timeout_seconds = {_GATE_TIMEOUT_SECONDS + _QUEUE_TIMEOUT_SECONDS + 1}".encode(
			"ascii"
		),
	)
	assert working_manifest != staged_manifest
	(repository / "quality-gate.toml").write_bytes(working_manifest)
	staged_manifest_path = tmp_path / "staged-manifest.bin"
	staged_manifest_path.write_bytes(actual_staged_manifest)
	local_app_data = tmp_path / "local-app-data"
	queue_root = local_app_data / "quality-gate"
	temporary_release = queue_root / "releases" / release_version
	candidate_wheel = _write_candidate_wheel(
		project_root,
		selected_release_wheel,
		temporary_release,
		release_version,
	)
	monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
	monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, str(_QUEUE_TIMEOUT_SECONDS))
	monkeypatch.delenv("CI", raising=False)
	monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
	monkeypatch.delenv("QUALITY_GATE_ADMISSION_CI", raising=False)
	monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
	assert bridge._release_wheel(release_version) == candidate_wheel
	outer_timeout = commit_gate_timeout_seconds(actual_staged_manifest)
	assert outer_timeout == _GATE_TIMEOUT_SECONDS + _QUEUE_TIMEOUT_SECONDS
	return repository, staged_manifest_path, queue_root, outer_timeout


def _start_native_launch(
	project_root: Path,
	repository: Path,
	staged_manifest_path: Path,
	outer_timeout: int,
) -> _NativeLaunch:
	# The host adapter is standard-library-only and copied into the stable bridge at activation.
	harness = (
		"import importlib.util, os, re, sys, tomllib\n"
		"from pathlib import Path\n"
		"def _proposed_gate_timeout(manifest):\n"
		"    document = tomllib.loads(manifest.decode('utf-8'))\n"
		"    quality = document.get('quality')\n"
		"    repository = document.get('repository')\n"
		"    if not isinstance(quality, dict) or quality.get('schema') != 2:\n"
		"        raise ValueError('the staged manifest must use schema 2')\n"
		"    if not isinstance(repository, dict):\n"
		"        raise ValueError('the staged manifest repository is malformed')\n"
		"    defaults = repository.get('defaults', {})\n"
		"    if not isinstance(defaults, dict):\n"
		"        raise ValueError('the staged manifest defaults are malformed')\n"
		"    gate = defaults.get('gate_timeout_seconds', "
		f"{_NATIVE_DEFAULT_GATE_TIMEOUT_SECONDS})\n"
		"    if isinstance(gate, bool) or not isinstance(gate, int) or gate <= 0:\n"
		"        raise ValueError('staged gate timeout must be a positive integer')\n"
		"    ci = any(\n"
		"        os.environ.get(name, '').casefold() in {'1', 'true', 'yes', 'on'}\n"
		"        for name in ('CI', 'GITHUB_ACTIONS', 'QUALITY_GATE_ADMISSION_CI')\n"
		"    )\n"
		"    if ci:\n"
		"        return gate\n"
		"    raw = os.environ.get('QUALITY_GATE_QUEUE_TIMEOUT_SECONDS')\n"
		"    if raw is None:\n"
		f"        queue = {_NATIVE_DEFAULT_QUEUE_TIMEOUT_SECONDS}\n"
		"    elif re.fullmatch(r'[0-9]{1,3}', raw):\n"
		"        queue = int(raw)\n"
		f"        if queue > {_NATIVE_MAX_QUEUE_TIMEOUT_SECONDS}:\n"
		"            raise ValueError('invalid local Quality Gate queue timeout')\n"
		"    else:\n"
		"        raise ValueError('invalid local Quality Gate queue timeout')\n"
		"    return gate + queue\n"
		"spec = importlib.util.spec_from_file_location(\n"
		"    'native_quality_gate_adapter', sys.argv[1])\n"
		"bridge = importlib.util.module_from_spec(spec)\n"
		"sys.modules[spec.name] = bridge\n"
		"spec.loader.exec_module(bridge)\n"
		"manifest = Path(sys.argv[2]).read_bytes()\n"
		"outer_timeout = _proposed_gate_timeout(manifest)\n"
		"assert outer_timeout == int(os.environ['EXPECTED_GATE_TIMEOUT_SECONDS'])\n"
		"bridge.COMMIT_GATE_TIMEOUT_SECONDS = outer_timeout\n"
		"repository = Path(sys.argv[3])\n"
		"result = bridge.run_released_command(\n"
		"    repository, ['--root', str(repository), 'check', '--verbose'], manifest=manifest)\n"
		"if result.stdout: sys.stdout.write(result.stdout)\n"
		"if result.stderr: sys.stderr.write(result.stderr)\n"
		"raise SystemExit(result.returncode)\n"
	)
	environment = os.environ.copy()
	environment["EXPECTED_GATE_TIMEOUT_SECONDS"] = str(outer_timeout)
	process = subprocess.Popen(
		[
			sys.executable,
			"-B",
			"-c",
			harness,
			str(_native_bridge_path()),
			str(staged_manifest_path),
			str(repository),
		],
		cwd=project_root,
		env=environment,
		stdin=subprocess.DEVNULL,
		stdout=subprocess.PIPE,
		stderr=subprocess.PIPE,
		text=True,
		encoding="utf-8",
		errors="replace",
		bufsize=1,
	)
	assert process.stdout is not None
	assert process.stderr is not None
	stdout_lines: list[str] = []
	stderr_lines: list[str] = []
	messages: queue.Queue[tuple[str, str | None]] = queue.Queue()
	readers = (
		threading.Thread(
			target=_read_output,
			args=(process.stdout, "stdout", stdout_lines, messages),
			daemon=True,
		),
		threading.Thread(
			target=_read_output,
			args=(process.stderr, "stderr", stderr_lines, messages),
			daemon=True,
		),
	)
	for reader in readers:
		reader.start()
	return _NativeLaunch(process, stdout_lines, stderr_lines, readers, messages)


def _wait_for_queue_notice(launch: _NativeLaunch, timeout_seconds: int) -> bool:
	deadline = time.monotonic() + timeout_seconds
	while time.monotonic() < deadline and launch.process.poll() is None:
		try:
			channel, line = launch.messages.get(timeout=0.05)
		except queue.Empty:
			continue
		if channel == "stderr" and line and "waiting for a local run slot" in line:
			return True
	return False


def _finish_native_launch(launch: _NativeLaunch, timeout_seconds: int) -> tuple[int, str, str]:
	try:
		return_code = launch.process.wait(timeout=timeout_seconds)
	except subprocess.TimeoutExpired:
		launch.process.kill()
		launch.process.wait(timeout=5)
		raise
	for reader in launch.readers:
		reader.join(timeout=2)
	assert all(not reader.is_alive() for reader in launch.readers)
	return return_code, "".join(launch.stdout_lines), "".join(launch.stderr_lines)


def _stop_native_launch(launch: _NativeLaunch) -> None:
	if launch.process.poll() is None:
		try:
			launch.process.wait(timeout=_GATE_TIMEOUT_SECONDS + _QUEUE_TIMEOUT_SECONDS + 5)
		except subprocess.TimeoutExpired:
			launch.process.kill()
			launch.process.wait(timeout=5)
	for reader in launch.readers:
		reader.join(timeout=2)


def test_native_bridge_launches_its_verified_staged_release_wheel() -> None:
	bridge, _bridge_path = _load_native_bridge()
	project_root = Path(__file__).resolve().parents[1]
	staged_manifest = (project_root / "quality-gate.toml").read_bytes()
	release_version = bridge._policy_release(staged_manifest)
	try:
		selected_wheel = bridge._release_wheel(release_version)
	except (OSError, ValueError) as error:
		pytest.skip(
			f"the staged policy wheel is unavailable for native launch integration: {error}"
		)
	bridge.COMMIT_GATE_TIMEOUT_SECONDS = _GATE_TIMEOUT_SECONDS
	result = bridge.run_released_command(
		project_root, ["version"], manifest=staged_manifest, capture_output=True
	)
	assert bridge._release_wheel(release_version) == selected_wheel
	assert result.returncode == _SUCCESS_EXIT
	assert result.stdout is not None and result.stdout.startswith("quality-gate ")
	assert result.stderr == ""


def test_proposed_native_wrapper_waits_and_continues_through_verified_candidate_wheel(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	bridge, _ = _load_native_bridge()
	project_root = Path(__file__).resolve().parents[1]
	repository, staged_path, queue_root, outer_timeout = _prepare_native_fixture(
		tmp_path, project_root, bridge, monkeypatch
	)
	holder_environment = os.environ.copy()
	holder_environment["PYTHONPATH"] = str(project_root)
	with _occupied_slots(queue_root, project_root, holder_environment) as holders:
		launch = _start_native_launch(project_root, repository, staged_path, outer_timeout)
		try:
			assert _wait_for_queue_notice(launch, _QUEUE_TIMEOUT_SECONDS)
			assert _release_slot_holder(holders.pop(0)) == _SUCCESS_EXIT
			return_code, stdout, stderr = _finish_native_launch(launch, outer_timeout + 2)
			assert return_code == _SUCCESS_EXIT, stdout + stderr
			assert "local run slot acquired" in stderr
			assert "Quality Gate: 0" in stdout
			assert "secrets.candidate: passed" in stdout
		finally:
			_stop_native_launch(launch)
	with admission.gate_admission(cache_root=queue_root):
		pass


def test_proposed_native_wrapper_preserves_nested_ci_context(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	bridge, _ = _load_native_bridge()
	project_root = Path(__file__).resolve().parents[1]
	_repository, staged_path, queue_root, _outer_timeout = _prepare_native_fixture(
		tmp_path, project_root, bridge, monkeypatch
	)
	# Missing Git metadata exposes snapshot entry without running repository checks.
	repository = tmp_path / "ci-empty-repository"
	repository.mkdir()
	holder_environment = os.environ.copy()
	holder_environment["PYTHONPATH"] = str(project_root)
	with _occupied_slots(queue_root, project_root, holder_environment):
		monkeypatch.setenv("QUALITY_GATE_ADMISSION_CI", "1")
		monkeypatch.setenv(admission.QUEUE_TIMEOUT_ENV, "invalid")
		launch = _start_native_launch(project_root, repository, staged_path, _GATE_TIMEOUT_SECONDS)
		try:
			return_code, stdout, stderr = _finish_native_launch(launch, _GATE_TIMEOUT_SECONDS + 2)
			assert return_code == _UNCHECKED_EXIT, stdout + stderr
			assert "candidate.snapshot: unchecked" in stdout
			assert "runtime.admission" not in stdout
			assert "waiting for a local run slot" not in stderr
		finally:
			_stop_native_launch(launch)


def test_proposed_native_wrapper_reports_expired_queue_without_starting_checks(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	bridge, _ = _load_native_bridge()
	project_root = Path(__file__).resolve().parents[1]
	repository, staged_path, queue_root, outer_timeout = _prepare_native_fixture(
		tmp_path, project_root, bridge, monkeypatch
	)
	holder_environment = os.environ.copy()
	holder_environment["PYTHONPATH"] = str(project_root)
	with _occupied_slots(queue_root, project_root, holder_environment):
		launch = _start_native_launch(project_root, repository, staged_path, outer_timeout)
		try:
			return_code, stdout, stderr = _finish_native_launch(launch, outer_timeout + 2)
			assert return_code == _UNCHECKED_EXIT
			assert "waiting for a local run slot" in stderr
			assert "runtime.admission: unchecked" in stdout
			assert "manifest.documents" not in stdout
		finally:
			_stop_native_launch(launch)
