"""Repository-specific verification runtime fingerprints and preparation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from .contracts import Manifest, PythonComponent
from .distribution import (
	DEFAULT_LOCK_TIMEOUT_SECONDS,
	ExternalTool,
	PolicyCache,
	ReleaseManifest,
	locked,
)


class RuntimeErrorBaseError(RuntimeError):
	"""A verification runtime is missing, stale, or cannot be prepared."""


class RuntimeUnavailableError(RuntimeErrorBaseError):
	"""A required runtime prerequisite is not available."""


RuntimeUnavailable = RuntimeUnavailableError


@dataclass(frozen=True, slots=True)
class RuntimeIdentity:
	"""All declared inputs that determine a consumer verification runtime."""

	repository: str
	policy_release: str
	component: str
	python_version: str
	component_contract: dict[str, object]
	dependency_inputs: tuple[dict[str, object], ...]


@dataclass(frozen=True, slots=True)
class RuntimeInspection:
	"""The observable state of one expected runtime."""

	path: Path
	python: Path | None
	current: bool
	reason: str | None = None


def _file_identity(root: Path, relative: str) -> dict[str, object]:
	path = (root / relative).resolve()
	if root.resolve() not in path.parents and path != root.resolve():
		raise RuntimeUnavailable(f"dependency input escapes the repository: {relative}")
	try:
		content = path.read_bytes()
	except (OSError, UnicodeError) as error:
		raise RuntimeUnavailable(f"dependency input is unavailable: {relative}") from error
	return {"path": relative.replace("\\", "/"), "sha256": hashlib.sha256(content).hexdigest()}


def runtime_identity(
	root: Path,
	manifest: Manifest,
	component: PythonComponent,
	*,
	repository: str | None = None,
) -> RuntimeIdentity:
	"""Build a canonical identity from a manifest and repository files."""
	return RuntimeIdentity(
		repository=repository or str(root.resolve()),
		policy_release=manifest.policy_release,
		component=component.name,
		python_version=component.python_version,
		component_contract={
			"name": component.name,
			"path": component.path,
			"test_paths": list(component.test_paths),
			"tests_applicable": component.tests_applicable,
			"tests_reason": component.tests_reason,
			"timeout_seconds": component.timeout_seconds,
		},
		dependency_inputs=tuple(
			_file_identity(root, dependency) for dependency in component.dependency_inputs
		),
	)


def runtime_fingerprint(identity: RuntimeIdentity) -> str:
	"""Return the stable SHA-256 identity of a verification runtime."""
	canonical = json.dumps(asdict(identity), sort_keys=True, separators=(",", ":"))
	return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _repository_key(root: Path) -> str:
	return hashlib.sha256(str(root.resolve()).casefold().encode("utf-8")).hexdigest()[:32]


def _python_for_version(version: str) -> Path:
	requested = version.strip()
	if not requested:
		raise RuntimeUnavailable("Python version is empty")
	candidates = [f"python{requested}", "python"]
	for candidate in candidates:
		path = shutil.which(candidate)
		if path is None:
			continue
		result = subprocess.run(
			[path, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"],
			capture_output=True,
			text=True,
			check=False,
		)
		if result.returncode == 0 and result.stdout.strip() == requested:
			return Path(path).resolve()
	raise RuntimeUnavailable(f"Python {requested} is not installed")


def _python_executable(path: Path) -> Path:
	return path / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class RuntimeManager:
	"""Prepare and inspect runtimes under a cache independent of developer venvs."""

	def __init__(self, cache: PolicyCache | Path | str | None = None) -> None:
		self.cache = cache if isinstance(cache, PolicyCache) else PolicyCache(cache)
		self.root = self.cache.root / "runtimes"

	def path_for(self, root: Path, identity: RuntimeIdentity) -> Path:
		"""Return the runtime path for a repository and exact identity."""
		return self.root / _repository_key(root) / runtime_fingerprint(identity)

	def inspect(self, root: Path, identity: RuntimeIdentity) -> RuntimeInspection:
		"""Inspect whether the runtime for an identity is current and executable."""
		path = self.path_for(root, identity)
		python = _python_executable(path)
		metadata = path / "runtime.json"
		if not metadata.is_file() or not python.is_file():
			return RuntimeInspection(
				path, python if python.is_file() else None, False, "runtime is missing"
			)
		try:
			stored = json.loads(metadata.read_text(encoding="utf-8"))
		except (OSError, UnicodeError, json.JSONDecodeError):
			return RuntimeInspection(path, python, False, "runtime metadata is corrupt")
		if stored != _runtime_metadata(identity):
			return RuntimeInspection(path, python, False, "runtime identity is stale")
		return RuntimeInspection(path, python, True)

	def ensure(
		self,
		root: Path,
		manifest: Manifest,
		component: PythonComponent,
		*,
		python_executable: Path | None = None,
		install: Callable[[Path, Path, tuple[str, ...]], None] | None = None,
		policy_root: Path | None = None,
		release_manifest: ReleaseManifest | None = None,
		lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
	) -> RuntimeInspection:
		"""Create an isolated runtime after explicit setup or sync authorization."""
		identity = runtime_identity(root, manifest, component)
		current = self.inspect(root, identity)
		if current.current:
			return current
		python = python_executable or _python_for_version(component.python_version)
		target = current.path
		target.parent.mkdir(parents=True, exist_ok=True)
		lock_path = target.parent / f".{target.name}.lock"
		with locked(lock_path, lock_timeout_seconds):
			current = self.inspect(root, identity)
			if current.current:
				return current
			with tempfile.TemporaryDirectory(
				prefix="quality-gate-runtime-", dir=target.parent
			) as temporary:
				staged = Path(temporary) / "runtime"
				venv_options = (
					()
					if component.dependency_inputs or policy_root is not None
					else ("--without-pip",)
				)
				try:
					result = subprocess.run(
						[str(python), "-m", "venv", *venv_options, str(staged)],
						cwd=root,
						capture_output=True,
						text=True,
						check=False,
					)
				except OSError as error:
					raise RuntimeUnavailable(
						"requested Python executable is unavailable"
					) from error
				if result.returncode:
					raise RuntimeUnavailable("Python virtual environment creation failed")
				staged_python = _python_executable(staged)
				if policy_root is not None and release_manifest is not None:
					self._install_policy(staged_python, policy_root, release_manifest)
				if install is not None:
					install(staged_python, root, component.dependency_inputs)
				else:
					self._install_dependencies(staged_python, root, component.dependency_inputs)
				_write_runtime_metadata(staged / "runtime.json", identity)
				if target.exists():
					shutil.rmtree(target)
				os.replace(staged, target)
		return self.inspect(root, identity)

	def _install_policy(
		self,
		python: Path,
		policy_root: Path,
		release_manifest: ReleaseManifest,
	) -> None:
		wheels = [policy_root / release_manifest.wheel.path] if release_manifest.wheel else []
		wheels.extend(
			policy_root / tool.path
			for tool in release_manifest.tools
			if tool.path.lower().endswith(".whl")
		)
		if wheels:
			result = subprocess.run(
				[
					str(python),
					"-m",
					"pip",
					"install",
					"--no-index",
					"--find-links",
					str(policy_root),
					*(str(path) for path in wheels),
				],
				capture_output=True,
				text=True,
				check=False,
			)
			if result.returncode:
				raise RuntimeUnavailable("policy or external tool installation failed")
		for tool in release_manifest.tools:
			if not tool.path.lower().endswith(".whl"):
				self._install_tool(policy_root, tool, python.parent)

	def _install_tool(self, policy_root: Path, tool: ExternalTool, bin_path: Path) -> None:
		source = policy_root / tool.path
		target = bin_path / Path(tool.path).name
		try:
			shutil.copy2(source, target)
			if os.name != "nt":
				target.chmod(target.stat().st_mode | 0o111)
		except OSError as error:
			raise RuntimeUnavailable(f"external tool installation failed: {tool.name}") from error

	def _install_dependencies(self, python: Path, root: Path, inputs: tuple[str, ...]) -> None:
		for relative in inputs:
			path = root / relative
			if path.name.startswith("requirements") and path.suffix in {".txt", ".in"}:
				command = [str(python), "-m", "pip", "install", "-r", str(path)]
			elif path.name == "pyproject.toml":
				command = [str(python), "-m", "pip", "install", "--no-deps", str(root)]
			else:
				continue
			result = subprocess.run(command, cwd=root, capture_output=True, text=True, check=False)
			if result.returncode:
				raise RuntimeUnavailable(f"dependency installation failed for {relative}")


def _write_runtime_metadata(path: Path, identity: RuntimeIdentity) -> None:
	value = _runtime_metadata(identity)
	path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def _runtime_metadata(identity: RuntimeIdentity) -> dict[str, object]:
	canonical = json.loads(json.dumps(asdict(identity), sort_keys=True))
	return canonical | {"fingerprint": runtime_fingerprint(identity)}
