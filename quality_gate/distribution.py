"""Immutable policy release storage and selection."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

RELEASE_VERSION = re.compile(r"^v\d+\.\d+\.\d+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
DEFAULT_LOCK_TIMEOUT_SECONDS = 60
RELEASE_RETENTION_DAYS = 30
MAX_RELEASE_BYTES = 100 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60


class DistributionError(RuntimeError):
	"""A release cannot be installed, selected, or verified."""


@dataclass(frozen=True, slots=True)
class ReleaseFile:
	"""A release file with its expected integrity digest."""

	path: str
	sha256: str
	kind: str = "artifact"


@dataclass(frozen=True, slots=True)
class ExternalTool:
	"""An external executable pinned by a policy release."""

	name: str
	version: str
	path: str
	sha256: str


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
	"""The validated, immutable contents of a policy release manifest."""

	version: str
	files: tuple[ReleaseFile, ...]
	tools: tuple[ExternalTool, ...]

	@property
	def policy_files(self) -> tuple[ReleaseFile, ...]:
		return tuple(item for item in self.files if item.kind == "policy")

	@property
	def wheel(self) -> ReleaseFile | None:
		return next(
			(item for item in self.files if item.kind == "artifact" and item.path.endswith(".whl")),
			None,
		)

	def as_dict(self) -> dict[str, object]:
		"""Return a canonical, redaction-safe mapping for immutability checks."""
		return {
			"version": self.version,
			"files": [
				{"path": item.path, "sha256": item.sha256, "kind": item.kind} for item in self.files
			],
			"tools": [
				{
					"name": item.name,
					"version": item.version,
					"path": item.path,
					"sha256": item.sha256,
				}
				for item in self.tools
			],
		}


def _table(value: object, path: str) -> dict[str, object]:
	if not isinstance(value, dict):
		raise DistributionError(f"{path} must be a table")
	return cast(dict[str, object], value)


def _text(value: object, path: str) -> str:
	if not isinstance(value, str) or not value.strip():
		raise DistributionError(f"{path} must be a non-empty string")
	return value.strip()


def _safe_release_path(value: object, path: str) -> str:
	result = _text(value, path).replace("\\", "/")
	candidate = Path(result)
	if (
		candidate.is_absolute()
		or result == "."
		or result.startswith("../")
		or "/../" in f"/{result}"
	):
		raise DistributionError(f"{path} must stay inside the release")
	return result


def _digest(value: object, path: str) -> str:
	result = _text(value, path).lower()
	if not SHA256.fullmatch(result):
		raise DistributionError(f"{path} must be a SHA-256 digest")
	return result


def _entry_list(value: object, path: str) -> list[dict[str, object]]:
	if value is None:
		return []
	if isinstance(value, list):
		return [_table(item, f"{path}[{index}]") for index, item in enumerate(value)]
	if isinstance(value, dict):
		return [_table(item, f"{path}.{name}") | {"name": name} for name, item in value.items()]
	raise DistributionError(f"{path} must be a table or array of tables")


def load_release_manifest(path: Path) -> ReleaseManifest:
	"""Load and validate a release.toml manifest from an extracted release."""
	try:
		raw = tomllib.loads((path / "release.toml").read_text(encoding="utf-8"))
	except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
		raise DistributionError("release.toml is missing or invalid UTF-8 TOML") from error

	release = raw.get("release", raw)
	release_table = _table(release, "release")
	version = _text(release_table.get("version"), "release.version")
	if not RELEASE_VERSION.fullmatch(version):
		raise DistributionError("release.version must use vMAJOR.MINOR.PATCH")
	files = []
	for index, entry in enumerate(_entry_list(release_table.get("files"), "release.files")):
		files.append(
			ReleaseFile(
				_safe_release_path(entry.get("path"), f"release.files[{index}].path"),
				_digest(entry.get("sha256"), f"release.files[{index}].sha256"),
				_text(entry.get("kind", "artifact"), f"release.files[{index}].kind"),
			)
		)
	tools = []
	for index, entry in enumerate(_entry_list(release_table.get("tools"), "release.tools")):
		tools.append(
			ExternalTool(
				_text(entry.get("name"), f"release.tools[{index}].name"),
				_text(entry.get("version"), f"release.tools[{index}].version"),
				_safe_release_path(entry.get("path"), f"release.tools[{index}].path"),
				_digest(entry.get("sha256"), f"release.tools[{index}].sha256"),
			)
		)
	if not files:
		raise DistributionError("release.files must declare at least one artifact")
	if not any(item.path.endswith(".whl") for item in files):
		raise DistributionError("release.files must contain the policy wheel")
	paths = [item.path for item in files] + [item.path for item in tools]
	if len(paths) != len(set(paths)):
		raise DistributionError("release contains duplicate file paths")
	return ReleaseManifest(version, tuple(files), tuple(tools))


def _sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as stream:
		for block in iter(lambda: stream.read(1024 * 1024), b""):
			digest.update(block)
	return digest.hexdigest()


def verify_release(path: Path, manifest: ReleaseManifest | None = None) -> ReleaseManifest:
	"""Verify every declared release artifact and external tool."""
	validated = manifest or load_release_manifest(path)
	tool_files = tuple(ReleaseFile(tool.path, tool.sha256, "tool") for tool in validated.tools)
	for item in (*validated.files, *tool_files):
		file_path = path / item.path
		if not file_path.is_file():
			raise DistributionError(f"release file is missing: {item.path}")
		if _sha256(file_path) != item.sha256:
			raise DistributionError(f"release checksum mismatch: {item.path}")
	return validated


def default_cache_root() -> Path:
	"""Return the user-local cache directory without creating it."""
	if os.name == "nt":
		windows_base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
		return Path(windows_base) / "quality-gate"
	unix_base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
	return Path(unix_base) / "quality-gate"


@contextmanager
def locked(path: Path, timeout_seconds: float) -> Iterator[None]:
	"""Hold an exclusive cross-process lock until the context exits."""
	path.parent.mkdir(parents=True, exist_ok=True)
	deadline = time.monotonic() + timeout_seconds
	while True:
		try:
			with path.open("x", encoding="ascii") as stream:
				stream.write(f"{os.getpid()}\n")
			break
		except FileExistsError:
			if time.monotonic() >= deadline:
				raise DistributionError("release cache is locked")
			time.sleep(0.05)
	try:
		yield
	finally:
		try:
			path.unlink()
		except FileNotFoundError:
			pass


def _write_json(path: Path, value: Mapping[str, object]) -> None:
	temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
	temporary.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n", encoding="utf-8")
	os.replace(temporary, path)


class PolicyCache:
	"""Manage immutable releases and their active/previous selection state."""

	def __init__(self, root: Path | str | None = None) -> None:
		self.root = Path(root or default_cache_root()).expanduser().resolve()
		self.releases = self.root / "releases"
		self.quarantine = self.root / "quarantine"
		self.state_path = self.root / "state.json"
		self.lock_path = self.root / ".sync.lock"

	def _state(self) -> dict[str, object]:
		try:
			return cast(dict[str, object], json.loads(self.state_path.read_text(encoding="utf-8")))
		except FileNotFoundError:
			return {"active": None, "previous": [], "installed": {}}
		except (OSError, UnicodeError, json.JSONDecodeError) as error:
			raise DistributionError("policy cache state is corrupt") from error

	def _set_active(self, version: str) -> None:
		state = self._state()
		previous_value = state.get("previous", [])
		previous = (
			[item for item in previous_value if isinstance(item, str) and item != version]
			if isinstance(previous_value, list)
			else []
		)
		active = state.get("active")
		if isinstance(active, str) and active != version:
			previous.insert(0, active)
		installed = state.get("installed")
		installed_map = dict(installed) if isinstance(installed, dict) else {}
		installed_map.setdefault(version, datetime.now(UTC).isoformat())
		_write_json(
			self.state_path,
			{"active": version, "previous": previous, "installed": installed_map},
		)

	def cached(self, version: str) -> Path:
		"""Return a verified cached release directory for an exact version."""
		if not RELEASE_VERSION.fullmatch(version):
			raise DistributionError("policy release must use vMAJOR.MINOR.PATCH")
		path = self.releases / version
		try:
			manifest = verify_release(path)
		except DistributionError as error:
			if path.exists():
				self._quarantine(path, str(error))
			raise DistributionError(f"cached release {version} is unavailable: {error}") from error
		if manifest.version != version:
			raise DistributionError("cached release version does not match its directory")
		return path

	def _quarantine(self, path: Path, reason: str) -> None:
		self.quarantine.mkdir(parents=True, exist_ok=True)
		target = self.quarantine / f"{path.name}-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
		try:
			os.replace(path, target)
			(target / "quarantine-reason.txt").write_text(reason + "\n", encoding="utf-8")
		except OSError as error:
			raise DistributionError("corrupt release could not be quarantined") from error

	def sync(
		self,
		source: Path | str,
		*,
		version: str | None = None,
		lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
	) -> ReleaseManifest:
		"""Verify and atomically install one immutable release from a directory or zip."""
		with locked(self.lock_path, lock_timeout_seconds):
			self.root.mkdir(parents=True, exist_ok=True)
			with tempfile.TemporaryDirectory(
				prefix="quality-gate-release-", dir=self.root
			) as temporary:
				staged = Path(temporary) / "release"
				self._copy_source(Path(source), staged)
				manifest = verify_release(staged)
				if version is not None and manifest.version != version:
					raise DistributionError(
						f"requested {version}, source contains {manifest.version}"
					)
				self.releases.mkdir(parents=True, exist_ok=True)
				target = self.releases / manifest.version
				if target.exists():
					try:
						cached = verify_release(target)
					except DistributionError as error:
						self._quarantine(target, str(error))
						if target.exists():
							raise DistributionError(
								f"corrupt release {manifest.version} could not be quarantined"
							) from error
					else:
						if cached.as_dict() != manifest.as_dict():
							raise DistributionError(
								f"release {manifest.version} is immutable and already installed"
							)
						self._set_active(manifest.version)
						return manifest
				os.replace(staged, target)
				self._set_active(manifest.version)
				return manifest

	def _copy_source(self, source: Path, target: Path) -> None:
		if source.is_dir():
			self._validate_directory_size(source)
			shutil.copytree(source, target)
			return
		if source.is_file() and source.suffix.lower() == ".zip":
			target.mkdir(parents=True)
			with zipfile.ZipFile(source) as archive:
				total_size = 0
				for member in archive.infolist():
					total_size += member.file_size
					if total_size > MAX_RELEASE_BYTES:
						raise DistributionError("release archive exceeds the size limit")
					member_path = (target / member.filename).resolve()
					release_root = target.resolve()
					if release_root not in member_path.parents and member_path != release_root:
						raise DistributionError("release archive contains an unsafe path")
			archive.extractall(target)
			return
		raise DistributionError("release source does not exist")

	def _validate_directory_size(self, source: Path) -> None:
		total_size = 0
		for path in source.rglob("*"):
			if path.is_symlink():
				raise DistributionError("release directory contains a symbolic link")
			if path.is_file():
				total_size += path.stat().st_size
				if total_size > MAX_RELEASE_BYTES:
					raise DistributionError("release directory exceeds the size limit")

	def sync_url(
		self,
		url: str,
		*,
		version: str | None = None,
		lock_timeout_seconds: float = DEFAULT_LOCK_TIMEOUT_SECONDS,
	) -> ReleaseManifest:
		"""Download one release to a temporary file and pass it through sync."""
		parsed = urlparse(url)
		if parsed.scheme not in {"http", "https"} or not parsed.netloc:
			raise DistributionError("policy release URL must use http or https")
		self.root.mkdir(parents=True, exist_ok=True)
		with tempfile.TemporaryDirectory(
			prefix="quality-gate-download-", dir=self.root
		) as temporary:
			archive = Path(temporary) / "release.zip"
			try:
				with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
					content_length = response.headers.get("Content-Length")
					try:
						declared_size = int(content_length) if content_length else 0
					except ValueError as error:
						raise DistributionError("policy release size is invalid") from error
					if declared_size > MAX_RELEASE_BYTES:
						raise DistributionError("policy release download exceeds the size limit")
					with archive.open("wb") as stream:
						read_size = 0
						while block := response.read(1024 * 1024):
							read_size += len(block)
							if read_size > MAX_RELEASE_BYTES:
								raise DistributionError(
									"policy release download exceeds the size limit"
								)
							stream.write(block)
			except (OSError, urllib.error.URLError) as error:
				raise DistributionError("policy release download failed") from error
			return self.sync(
				archive,
				version=version,
				lock_timeout_seconds=lock_timeout_seconds,
			)

	def select(self, version: str) -> Path:
		"""Select one exact cached release without downloading or mutating it."""
		path = self.cached(version)
		return path

	def rollback(self, version: str | None = None) -> Path:
		"""Activate a retained release, defaulting to the immediately previous one."""
		with locked(self.lock_path, DEFAULT_LOCK_TIMEOUT_SECONDS):
			state = self._state()
			candidate = version
			if candidate is None:
				previous_value = state.get("previous", [])
				previous = (
					cast(list[str], previous_value) if isinstance(previous_value, list) else []
				)
				candidate = previous[0] if previous else None
			if not isinstance(candidate, str):
				raise DistributionError("no previous policy release is retained")
			path = self.cached(candidate)
			self._set_active(candidate)
			return path

	def prune(
		self,
		*,
		confirm: bool = False,
		older_than_days: int = RELEASE_RETENTION_DAYS,
	) -> tuple[str, ...]:
		"""Preview or explicitly remove releases outside the retention set."""
		if older_than_days < 0:
			raise DistributionError("retention must not be negative")
		cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
		if not confirm:
			return self._prune_candidates(cutoff)
		with locked(self.lock_path, DEFAULT_LOCK_TIMEOUT_SECONDS):
			candidates = self._prune_candidates(cutoff)
			for name in candidates:
				shutil.rmtree(self.releases / name)
		return tuple(sorted(candidates))

	def _prune_candidates(self, cutoff: datetime) -> tuple[str, ...]:
		state = self._state()
		previous_value = state.get("previous", [])
		previous = cast(list[str], previous_value) if isinstance(previous_value, list) else []
		retained = {state.get("active"), *previous}
		candidates: list[str] = []
		for path in self.releases.iterdir() if self.releases.exists() else ():
			if not path.is_dir() or path.name in retained:
				continue
			if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
				candidates.append(path.name)
		return tuple(sorted(candidates))

	def status(self) -> dict[str, object]:
		"""Return redaction-safe cache state for diagnostics."""
		state = self._state()
		return {
			"root": str(self.root),
			"active": state.get("active"),
			"previous": state.get("previous", []),
			"releases": sorted(path.name for path in self.releases.iterdir() if path.is_dir())
			if self.releases.exists()
			else [],
		}
