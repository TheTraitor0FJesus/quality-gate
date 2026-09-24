"""Fail-closed validation of a self-hosted Quality Gate release candidate."""

from __future__ import annotations

import argparse
import ast
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .contracts import Manifest, Status, ValidationError, load_manifest
from .distribution import DistributionError, PolicyCache, ReleaseManifest
from .lessons import ReleaseBlockedError, ensure_release_ready
from .runner import required_documents_result
from .temp_workspace import TemporaryWorkspaceError, temporary_workspace

RELEASE_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")
PACKAGE_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
MAX_RELEASE_VERSION_LENGTH = 32
MAX_RELEASE_VERSION_COMPONENT_LENGTH = 8
MAX_RELEASE_NOTES_BYTES = 64 * 1024
# Keep enough room for the venv, installed package, and release cache below this path.
WINDOWS_MAX_WORKSPACE_PATH_CHARS = 180
WINDOWS_MAX_PATH_BUFFER_CHARS = 32768
_LOGGER = logging.getLogger(__name__)


class ReleaseControllerError(RuntimeError):
	"""A source tree or artifact cannot be released safely."""


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
	"""A source tree and integrity-checked release artifact at one version."""

	version: str
	source: Path
	artifact: Path
	manifest: ReleaseManifest


def _project_version(root: Path) -> str:
	try:
		raw = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
		project = raw.get("project")
		version = project.get("version") if isinstance(project, dict) else None
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ReleaseControllerError("pyproject.toml is missing or invalid") from error
	if not isinstance(version, str) or not version.strip():
		raise ReleaseControllerError("project.version is missing from pyproject.toml")
	return version.strip()


def _version_authority(root: Path) -> str:
	try:
		raw = tomllib.loads((root / ".release" / "version.toml").read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise ReleaseControllerError(".release/version.toml is missing or invalid") from error
	if set(raw) != {"version"}:
		raise ReleaseControllerError(
			".release/version.toml must contain exactly one top-level version"
		)
	version = raw.get("version")
	if not isinstance(version, str) or PACKAGE_VERSION.fullmatch(version) is None:
		raise ReleaseControllerError(".release/version.toml version must use MAJOR.MINOR.PATCH")
	return version


def _runtime_version(root: Path) -> str:
	try:
		tree = ast.parse(
			(root / "quality_gate" / "__init__.py").read_text(encoding="utf-8"),
			filename="quality_gate/__init__.py",
		)
	except (OSError, UnicodeError, SyntaxError) as error:
		raise ReleaseControllerError("runtime version projection is unreadable") from error
	for node in ast.walk(tree):
		if isinstance(node, ast.Assign) and any(
			isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
		):
			if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
				return node.value.value
			break
	raise ReleaseControllerError("runtime version projection is missing")


def _release_notes(root: Path) -> None:
	try:
		notes = (root / ".release" / "notes.md").read_text(encoding="utf-8")
	except (OSError, UnicodeError) as error:
		raise ReleaseControllerError(".release/notes.md is missing or unreadable") from error
	if not notes.strip() or len(notes.encode("utf-8")) > MAX_RELEASE_NOTES_BYTES:
		raise ReleaseControllerError("release notes must be nonempty and at most 64 KiB")


def _expected_version(authoritative_version: str, requested: str | None) -> str:
	version = requested or f"v{authoritative_version}"
	components = version[1:].split(".") if version.startswith("v") else ()
	if len(version) > MAX_RELEASE_VERSION_LENGTH or any(
		len(component) > MAX_RELEASE_VERSION_COMPONENT_LENGTH for component in components
	):
		raise ReleaseControllerError("release version is too long")
	if not RELEASE_VERSION.fullmatch(version):
		raise ReleaseControllerError("release version must use vMAJOR.MINOR.PATCH")
	if version != f"v{authoritative_version}":
		raise ReleaseControllerError(
			f"release version {version} does not match product version "
			f"{authoritative_version} from .release/version.toml"
		)
	return version


def validate_release_source(root: Path | str = ".", *, version: str | None = None) -> Manifest:
	"""Validate the self-host manifest, documents, package version, and lessons."""
	actual_root = Path(root).resolve()
	try:
		manifest = load_manifest(actual_root)
	except ValidationError as error:
		raise ReleaseControllerError(f"manifest is unverifiable: {error}") from error
	authoritative_version = _version_authority(actual_root)
	_expected_version(authoritative_version, version)
	project_version = _project_version(actual_root)
	if project_version != authoritative_version:
		raise ReleaseControllerError(
			f"project.version {project_version} does not match .release/version.toml"
		)
	if _runtime_version(actual_root) != authoritative_version:
		raise ReleaseControllerError("runtime __version__ does not match .release/version.toml")
	_release_notes(actual_root)
	documents = required_documents_result(actual_root, manifest)
	if documents.status is not Status.PASSED:
		missing = documents.findings[0].path if documents.findings else "required documents"
		raise ReleaseControllerError(
			f"required documents are unverifiable: repair {missing} before releasing"
		)
	try:
		ensure_release_ready(actual_root)
	except ReleaseBlockedError as error:
		raise ReleaseControllerError(f"lessons are not release-ready: {error}") from error
	return manifest


def _python_executable(root: Path) -> Path:
	return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _windows_final_path(path: Path) -> Path:
	"""Return the final path Windows assigned to an opened directory."""
	try:
		import ctypes
		from ctypes import wintypes

		win_dll = getattr(ctypes, "WinDLL", None)
		get_last_error = getattr(ctypes, "get_last_error", None)
		if win_dll is None or get_last_error is None:
			raise OSError("Windows ctypes APIs are unavailable")
		kernel32 = win_dll("kernel32", use_last_error=True)
	except (AttributeError, OSError) as error:
		raise ReleaseControllerError("release workspace final path is unavailable") from error

	create_file = kernel32.CreateFileW
	create_file.argtypes = (
		wintypes.LPCWSTR,
		wintypes.DWORD,
		wintypes.DWORD,
		wintypes.LPVOID,
		wintypes.DWORD,
		wintypes.DWORD,
		wintypes.HANDLE,
	)
	create_file.restype = wintypes.HANDLE
	get_final_path = kernel32.GetFinalPathNameByHandleW
	get_final_path.argtypes = (
		wintypes.HANDLE,
		wintypes.LPWSTR,
		wintypes.DWORD,
		wintypes.DWORD,
	)
	get_final_path.restype = wintypes.DWORD
	close_handle = kernel32.CloseHandle
	close_handle.argtypes = (wintypes.HANDLE,)
	close_handle.restype = wintypes.BOOL

	handle = create_file(
		str(path),
		0,
		0x00000001 | 0x00000002 | 0x00000004,
		None,
		3,
		0x02000000,
		None,
	)
	if handle == ctypes.c_void_p(-1).value:
		raise OSError(get_last_error(), "release workspace cannot be opened")
	try:
		buffer_size = 256
		while True:
			buffer = ctypes.create_unicode_buffer(buffer_size)
			length = get_final_path(handle, buffer, buffer_size, 0)
			if length == 0:
				raise OSError(get_last_error(), "release workspace final path is unavailable")
			if length < buffer_size - 1:
				break
			if buffer_size >= WINDOWS_MAX_PATH_BUFFER_CHARS:
				raise ReleaseControllerError("release workspace final path is too long")
			buffer_size *= 2
	finally:
		close_handle(handle)

	final = buffer.value
	if final.startswith("\\\\?\\UNC\\"):
		final = "\\\\" + final[len("\\\\?\\UNC\\") :]
	elif final.startswith("\\\\?\\"):
		final = final[4:]
	return Path(final)


def _workspace_parents(workspace_parent: Path | str | None) -> tuple[Path, ...]:
	if workspace_parent is None:
		return (Path(tempfile.gettempdir()),)
	candidate = Path(workspace_parent).expanduser()
	try:
		candidate = candidate.resolve()
		is_directory = candidate.is_dir()
	except OSError as error:
		raise ReleaseControllerError(
			"release workspace parent is unavailable; provide an existing directory"
		) from error
	if not is_directory:
		raise ReleaseControllerError(
			"release workspace parent is unavailable; provide an existing directory"
		)
	return (candidate,)


@contextmanager
def _release_workspace(workspace_parent: Path | str | None) -> Iterator[Path]:
	"""Yield one isolated workspace with enough Windows path headroom."""
	if workspace_parent is None:
		yield _default_release_workspace()
		return
	parent = _workspace_parents(workspace_parent)[0]
	try:
		temporary = tempfile.TemporaryDirectory(prefix="qg-", dir=str(parent))
	except OSError as error:
		raise ReleaseControllerError(
			"release workspace is unavailable; provide an existing writable "
			"directory with --workspace-parent"
		) from error
	with temporary:
		workspace = Path(temporary.name)
		if os.name == "nt":
			try:
				final = _windows_final_path(workspace)
			except (OSError, ReleaseControllerError) as error:
				raise ReleaseControllerError(
					"release workspace final path is unavailable; provide a shorter "
					"--workspace-parent directory"
				) from error
			if len(str(final)) > WINDOWS_MAX_WORKSPACE_PATH_CHARS:
				raise ReleaseControllerError(
					"release workspace path is too long; provide a shorter "
					"--workspace-parent directory"
				)
		yield workspace


def _default_release_workspace() -> Path:
	workspace = Path(tempfile.gettempdir())
	if os.name != "nt":
		return workspace
	try:
		final = _windows_final_path(workspace)
	except (OSError, ReleaseControllerError) as error:
		raise ReleaseControllerError(
			"release workspace final path is unavailable; provide a shorter "
			"--workspace-parent directory"
		) from error
	if len(str(final)) > WINDOWS_MAX_WORKSPACE_PATH_CHARS:
		raise ReleaseControllerError(
			"release workspace path is too long; provide a shorter --workspace-parent directory"
		)
	return workspace


def _run_release_command(
	command: list[str],
	*,
	cwd: Path,
	environment: dict[str, str],
	deadline: float,
	stage: str,
) -> None:
	_LOGGER.debug("run release self-host stage=%s executable=%s", stage, command[0])
	timeout = max(deadline - time.monotonic(), 0.1)
	try:
		result = subprocess.run(
			command,
			cwd=cwd,
			env=environment,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			check=False,
			timeout=timeout,
		)
	except (OSError, subprocess.TimeoutExpired) as error:
		_LOGGER.warning("release self-host stage=%s is unavailable", stage, exc_info=True)
		raise ReleaseControllerError(
			f"release artifact self-host {stage} is unavailable"
		) from error
	if result.returncode:
		_LOGGER.warning(
			"release self-host stage=%s failed with return code %s", stage, result.returncode
		)
		raise ReleaseControllerError(f"release artifact self-host {stage} failed")
	_LOGGER.debug("release self-host stage=%s completed", stage)


def _execute_artifact_gate(
	root: Path,
	cache: PolicyCache,
	release_manifest: ReleaseManifest,
	workspace: Path,
	timeout_seconds: int,
) -> None:
	wheel = release_manifest.wheel
	if wheel is None:
		raise ReleaseControllerError("release artifact has no policy wheel")
	wheel_path = cache.releases / release_manifest.version / wheel.path
	gate_root = workspace
	venv_root = workspace / "venv"
	deadline = time.monotonic() + timeout_seconds
	environment = os.environ.copy()
	environment.pop("PYTHONHOME", None)
	environment.pop("PYTHONPATH", None)
	environment.pop("VIRTUAL_ENV", None)
	if os.name == "nt":
		environment["LOCALAPPDATA"] = str(workspace)
	else:
		environment["XDG_CACHE_HOME"] = str(workspace)
	_run_release_command(
		[sys.executable, "-m", "venv", "--without-pip", str(venv_root)],
		cwd=gate_root,
		environment=environment,
		deadline=deadline,
		stage="virtual environment creation",
	)
	python = _python_executable(venv_root)
	_run_release_command(
		[
			sys.executable,
			"-m",
			"pip",
			"--python",
			str(python),
			"install",
			"--no-deps",
			str(wheel_path),
		],
		cwd=gate_root,
		environment=environment,
		deadline=deadline,
		stage="wheel installation",
	)
	_run_release_command(
		[
			str(python),
			"-c",
			"import sys, quality_gate; "
			"raise SystemExit(0 if quality_gate.__version__ == sys.argv[1] else 1)",
			release_manifest.version.removeprefix("v"),
		],
		cwd=gate_root,
		environment=environment,
		deadline=deadline,
		stage="version probe",
	)
	setup_script = (
		"import sys\n"
		"from pathlib import Path\n"
		"from quality_gate.launcher import prepare_bootstrap\n"
		"prepare_bootstrap(\n"
		"    Path(sys.argv[1]),\n"
		"    policy_release=sys.argv[2],\n"
		"    cache_dir=Path(sys.argv[3]),\n"
		"    create_runtimes=True,\n"
		")\n"
	)
	_run_release_command(
		[
			str(python),
			"-c",
			setup_script,
			str(root),
			release_manifest.version,
			str(cache.root),
		],
		cwd=gate_root,
		environment=environment,
		deadline=deadline,
		stage="runtime setup",
	)
	audit_script = (
		"import sys\n"
		"from pathlib import Path\n"
		"from quality_gate.runner import bootstrap_audit\n"
		"verdict = bootstrap_audit(\n"
		"    Path(sys.argv[1]),\n"
		"    policy_release=sys.argv[2],\n"
		")\n"
		"raise SystemExit(verdict.exit_code)\n"
	)
	_run_release_command(
		[
			str(python),
			"-c",
			audit_script,
			str(root),
			release_manifest.version,
		],
		cwd=gate_root,
		environment=environment,
		deadline=deadline,
		stage="audit",
	)


def verify_release_candidate(
	root: Path | str = ".",
	artifact: Path | str = ".",
	*,
	version: str | None = None,
	workspace_parent: Path | str | None = None,
) -> ReleaseCandidate:
	"""Validate the source and verify one immutable release directory or archive."""
	actual_root = Path(root).resolve()
	manifest = validate_release_source(actual_root, version=version)
	release_version = _expected_version(_version_authority(actual_root), version)
	artifact_path = Path(artifact).resolve()
	try:
		with temporary_workspace(actual_root):
			with _release_workspace(workspace_parent) as workspace:
				cache = PolicyCache(workspace / "quality-gate")
				checked = cache.sync(artifact_path, version=release_version)
				_execute_artifact_gate(
					actual_root,
					cache,
					checked,
					workspace,
					manifest.repository.gate_timeout_seconds,
				)
	except TemporaryWorkspaceError as error:
		raise ReleaseControllerError(f"release workspace is unavailable: {error}") from error
	except DistributionError as error:
		raise ReleaseControllerError(f"release artifact is unverifiable: {error}") from error
	return ReleaseCandidate(release_version, actual_root, artifact_path, checked)


def _parser() -> argparse.ArgumentParser:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", type=Path, default=Path("."))
	parser.add_argument("--artifact", type=Path, required=True)
	parser.add_argument("--version")
	parser.add_argument("--workspace-parent", type=Path)
	return parser


def main(arguments: list[str] | None = None) -> int:
	"""Validate one self-hosted source and its release artifact."""
	options = _parser().parse_args(arguments)
	try:
		candidate = verify_release_candidate(
			options.root,
			options.artifact,
			version=options.version,
			workspace_parent=options.workspace_parent,
		)
	except ReleaseControllerError as error:
		sys.stderr.write(f"quality-gate release: unchecked - {error}\n")
		return 2
	sys.stdout.write(f"release: ready - {candidate.version}\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
