"""Bounded provider-owned storage for authenticated verification evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .distribution import DistributionError, default_cache_root, locked
from .evidence_identity import (
	EngineIdentity as EngineIdentity,
)
from .evidence_identity import (
	ruff_file_identity as ruff_file_identity,
)
from .evidence_identity import (
	valid_evidence_outcome,
)
from .evidence_identity import (
	valid_ruff_file_outcome as valid_ruff_file_outcome,
)

_SCHEMA = "quality-gate.verification-evidence"
_VERSION = 1
_DIRECTORY = "verification-evidence-v1"
_KEY_BYTES = 32
_MAX_RECORDS = 128
_MAX_RECORD_BYTES = 320 * 1024
_MAX_IDENTITY_BYTES = 64 * 1024
_MAX_ENGINE_ENTRIES = 512
_MAX_ENGINE_FILE_BYTES = 1024 * 1024
_MAX_ENGINE_TOTAL_BYTES = 8 * 1024 * 1024
_MAX_DURATION_MS = 86_400_000
_MAX_TIMESTAMP_LENGTH = 40
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_OPERATIONS = frozenset(
	{
		"quality-gate.check",
		"edit-check.legacy",
		"edit-check.validate",
		"edit-check.write",
	}
)


def _canonical(value: object) -> bytes:
	return json.dumps(
		value,
		sort_keys=True,
		ensure_ascii=True,
		separators=(",", ":"),
		allow_nan=False,
	).encode("ascii")


def identity_digest(identity: dict[str, Any]) -> str:
	"""Return the digest of one complete canonical evidence identity."""
	encoded = _canonical(identity)
	if len(encoded) > _MAX_IDENTITY_BYTES:
		raise ValueError("oversized evidence identity")
	return hashlib.sha256(encoded).hexdigest()


def engine_identity(package_root: Path, *, version: str) -> EngineIdentity | None:
	"""Digest the executing Python package and distinguish source from installed wheels."""
	try:
		root = Path(package_root)
		if root.name == "quality_gate" and root.parent.suffix.lower() == ".whl":
			if not isinstance(version, str) or not version.strip():
				return None
			wheel_bytes = _read(root.parent, 64 * 1024 * 1024, require_single_link=False)
			with zipfile.ZipFile(root.parent) as archive:
				if "quality_gate/__init__.py" not in archive.namelist():
					return None
			encoded = _canonical(
				{
					"domain": "quality-gate-wheel-v1",
					"version": version,
					"sha256": hashlib.sha256(wheel_bytes).hexdigest(),
				}
			)
			return EngineIdentity("wheel", version, hashlib.sha256(encoded).hexdigest())
		metadata = root.lstat()
		if (
			not stat.S_ISDIR(metadata.st_mode)
			or root.is_symlink()
			or getattr(metadata, "st_file_attributes", 0)
			& getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
			or root.name != "quality_gate"
			or not isinstance(version, str)
			or not version.strip()
		):
			return None
		form = _engine_form(root.parent)
		if form is None:
			return None
		files = _engine_files(root)
		if not files:
			return None
		encoded = _canonical(
			{
				"domain": "quality-gate-engine-v1",
				"form": form,
				"version": version,
				"files": files,
			}
		)
		if len(encoded) > _MAX_IDENTITY_BYTES:
			return None
		return EngineIdentity(form, version, hashlib.sha256(encoded).hexdigest())
	except (OSError, ValueError, TypeError, RecursionError, zipfile.BadZipFile):
		return None


def _engine_files(root: Path) -> list[dict[str, str]]:
	files: list[dict[str, str]] = []
	pending = [root]
	visited = 0
	total_bytes = 0
	while pending:
		directory = pending.pop()
		with os.scandir(directory) as scan:
			entries = sorted(scan, key=lambda entry: entry.name)
		visited += len(entries)
		if visited > _MAX_ENGINE_ENTRIES:
			raise ValueError("engine inventory is too large")
		for entry in entries:
			path = Path(entry.path)
			metadata = entry.stat(follow_symlinks=False)
			if stat.S_ISLNK(metadata.st_mode) or getattr(
				metadata, "st_file_attributes", 0
			) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
				raise ValueError("engine contains a link")
			if stat.S_ISDIR(metadata.st_mode):
				if entry.name != "__pycache__":
					pending.append(path)
				continue
			if not entry.name.endswith(".py"):
				continue
			if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_ENGINE_FILE_BYTES:
				raise ValueError("engine file is invalid")
			content = _read(path, _MAX_ENGINE_FILE_BYTES, require_single_link=False)
			total_bytes += len(content)
			if total_bytes > _MAX_ENGINE_TOTAL_BYTES:
				raise ValueError("engine inventory is too large")
			files.append(
				{
					"path": path.relative_to(root).as_posix(),
					"sha256": hashlib.sha256(content).hexdigest(),
				}
			)
	return files


def repository_digest(root: Path) -> str | None:
	"""Hash a canonical repository identity without persisting absolute paths."""
	try:
		repository = Path(root).resolve(strict=True)
		git_path = repository / ".git"
		try:
			metadata = git_path.lstat()
		except FileNotFoundError:
			metadata = None
		if metadata is None:
			identity_path = repository
		elif getattr(metadata, "st_file_attributes", 0) & getattr(
			stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
		):
			return None
		elif stat.S_ISDIR(metadata.st_mode):
			identity_path = git_path.resolve(strict=True)
		elif stat.S_ISREG(metadata.st_mode):
			identity_path = _repository_git_directory(repository, git_path)
		else:
			return None
		canonical = os.path.normpath(str(identity_path.resolve(strict=True)))
		if os.name == "nt":
			canonical = os.path.normcase(canonical).casefold()
		return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
	except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
		return None


def _repository_git_directory(repository: Path, git_path: Path) -> Path:
	pointer = _read(git_path, 4096).decode("utf-8").strip()
	key, separator, value = pointer.partition(":")
	if not separator or key.casefold() != "gitdir" or not value.strip():
		return repository
	directory = Path(value.strip())
	if not directory.is_absolute():
		directory = repository / directory
	directory = directory.resolve(strict=True)
	common_file = directory / "commondir"
	if not common_file.is_file():
		return directory
	common = Path(_read(common_file, 4096).decode("utf-8").strip())
	if not common.is_absolute():
		common = directory / common
	return common.resolve(strict=True)


def _engine_form(owner_root: Path) -> str | None:
	marker = owner_root / ".git"
	try:
		metadata = marker.lstat()
	except FileNotFoundError:
		return "wheel"
	except OSError:
		return None
	if getattr(metadata, "st_file_attributes", 0) & getattr(
		stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
	):
		return None
	if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
		return None
	if not (owner_root / "pyproject.toml").is_file():
		return "wheel"
	return "source"


def _read(path: Path, limit: int, *, require_single_link: bool = True) -> bytes:
	metadata = path.lstat()
	if (
		not stat.S_ISREG(metadata.st_mode)
		or require_single_link
		and metadata.st_nlink != 1
		or getattr(metadata, "st_file_attributes", 0)
		& getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
	):
		raise ValueError("invalid evidence file")
	with path.open("rb") as stream:
		content = stream.read(limit + 1)
	if len(content) > limit:
		raise ValueError("oversized evidence file")
	return content


def _write(path: Path, content: bytes) -> None:
	stream = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
	temporary = Path(stream.name)
	try:
		with stream:
			stream.write(content)
			stream.flush()
			temporary.chmod(0o600)
		os.replace(temporary, path)
	finally:
		temporary.unlink(missing_ok=True)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
	result: dict[str, object] = {}
	for key, value in pairs:
		if key in result:
			raise ValueError("duplicate evidence field")
		result[key] = value
	return result


def _is_record_path(path: Path) -> bool:
	return path.name.endswith(".json") and _SHA256.fullmatch(path.stem) is not None


def _safe_directory(path: Path) -> None:
	metadata = path.lstat()
	if (
		not stat.S_ISDIR(metadata.st_mode)
		or path.is_symlink()
		or getattr(metadata, "st_file_attributes", 0)
		& getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
	):
		raise ValueError("invalid evidence directory")


@dataclass(frozen=True, slots=True)
class EvidenceProducer:
	"""Provider-authored execution metadata attached after a completed check."""

	operation: str
	engine: EngineIdentity
	started_at: str
	completed_at: str
	duration_ms: int

	def __post_init__(self) -> None:
		if not isinstance(self.operation, str) or self.operation not in _OPERATIONS:
			raise ValueError("unsupported evidence producer")
		if not isinstance(self.engine, EngineIdentity):
			raise ValueError("invalid evidence engine")
		if not _is_utc_timestamp(self.started_at) or not _is_utc_timestamp(self.completed_at):
			raise ValueError("evidence timestamps must be UTC")
		if (
			type(self.duration_ms) is not int
			or self.duration_ms < 0
			or self.duration_ms > _MAX_DURATION_MS
		):
			raise ValueError("invalid evidence duration")

	def as_dict(self) -> dict[str, object]:
		return {
			"operation": self.operation,
			"engine": self.engine.as_dict(),
			"started_at": self.started_at,
			"completed_at": self.completed_at,
			"duration_ms": self.duration_ms,
		}


@dataclass(frozen=True, slots=True)
class EvidenceHit:
	"""One complete authenticated result returned by the evidence store."""

	identity_digest: str
	producer: EvidenceProducer
	scope: dict[str, Any]
	outcome: dict[str, Any]


def _is_utc_timestamp(value: object) -> bool:
	if not isinstance(value, str) or len(value) > _MAX_TIMESTAMP_LENGTH:
		return False
	try:
		parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
		return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)
	except ValueError:
		return False


def _producer(value: object) -> EvidenceProducer:
	if not isinstance(value, dict) or set(value) != {
		"operation",
		"engine",
		"started_at",
		"completed_at",
		"duration_ms",
	}:
		raise ValueError("invalid evidence producer")
	engine_value = value["engine"]
	if not isinstance(engine_value, dict) or set(engine_value) != {"form", "version", "digest"}:
		raise ValueError("invalid evidence engine")
	form, version, digest = (
		engine_value["form"],
		engine_value["version"],
		engine_value["digest"],
	)
	operation = value["operation"]
	started_at, completed_at, duration_ms = (
		value["started_at"],
		value["completed_at"],
		value["duration_ms"],
	)
	if (
		not isinstance(form, str)
		or not isinstance(version, str)
		or not isinstance(digest, str)
		or not isinstance(operation, str)
		or not isinstance(started_at, str)
		or not isinstance(completed_at, str)
		or type(duration_ms) is not int
	):
		raise ValueError("invalid evidence producer fields")
	engine = EngineIdentity(
		form=form,
		version=version,
		digest=digest,
	)
	return EvidenceProducer(
		operation=operation,
		engine=engine,
		started_at=started_at,
		completed_at=completed_at,
		duration_ms=duration_ms,
	)


class EvidenceStore:
	"""Optional complete-result cache; every unavailable or invalid entry is a miss."""

	def __init__(self, deadline: float, *, cache_root: Path | None = None) -> None:
		self.root = Path(cache_root or default_cache_root()) / _DIRECTORY
		self.deadline = deadline

	def _prepare(self) -> None:
		self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
		_safe_directory(self.root)

	def _key(self, *, create: bool = False) -> bytes:
		path = self.root / "key"
		if create and not path.exists():
			_write(path, os.urandom(_KEY_BYTES))
		key = _read(path, _KEY_BYTES)
		if len(key) != _KEY_BYTES:
			raise ValueError("invalid evidence key")
		return key

	def _load(
		self,
		path: Path,
		key: bytes,
		*,
		expected_digest: str,
		expected_identity: dict[str, Any] | None = None,
		expected_engine: EngineIdentity | None = None,
	) -> dict[str, Any] | None:
		if not _is_record_path(path):
			return None
		record = json.loads(_read(path, _MAX_RECORD_BYTES), object_pairs_hook=_unique_object)
		if not isinstance(record, dict) or set(record) != {"payload", "mac"}:
			return None
		payload, mac = record["payload"], record["mac"]
		if (
			not isinstance(payload, dict)
			or set(payload)
			!= {
				"schema",
				"version",
				"identity",
				"identity_sha256",
				"complete",
				"producer",
				"scope",
				"outcome",
			}
			or payload["schema"] != _SCHEMA
			or type(payload["version"]) is not int
			or payload["version"] != _VERSION
			or payload["complete"] is not True
			or payload["identity_sha256"] != expected_digest
			or not isinstance(payload["identity"], dict)
			or identity_digest(payload["identity"]) != expected_digest
			or not isinstance(mac, str)
			or not _SHA256.fullmatch(mac)
		):
			return None
		if not hmac.compare_digest(mac, hmac.digest(key, _canonical(payload), "sha256").hex()):
			return None
		identity = payload["identity"]
		producer = _producer(payload["producer"])
		scope, outcome = payload["scope"], payload["outcome"]
		if (
			not isinstance(identity.get("engine"), dict)
			or identity["engine"] != producer.engine.as_dict()
			or not isinstance(scope, dict)
			or identity.get("scope") != scope
			or not isinstance(outcome, dict)
			or not valid_evidence_outcome(identity, outcome)
			or expected_identity is not None
			and identity != expected_identity
			or expected_engine is not None
			and producer.engine != expected_engine
			or expected_engine is not None
			and identity.get("engine") != expected_engine.as_dict()
		):
			return None
		return payload

	def lookup(
		self, identity: dict[str, Any], *, expected_engine: EngineIdentity
	) -> EvidenceHit | None:
		"""Return a complete authenticated hit or miss without affecting the caller's result."""
		if time.monotonic() >= self.deadline:
			return None
		try:
			identity = json.loads(_canonical(identity), object_pairs_hook=_unique_object)
			digest = identity_digest(identity)
			if identity.get("engine") != expected_engine.as_dict():
				return None
			self._prepare()
			with locked(self.root / ".lock", 0):
				payload = self._load(
					self.root / f"{digest}.json",
					self._key(),
					expected_digest=digest,
					expected_identity=identity,
					expected_engine=expected_engine,
				)
			if payload is None:
				return None
			return EvidenceHit(
				identity_digest=digest,
				producer=_producer(payload["producer"]),
				scope=json.loads(_canonical(payload["scope"])),
				outcome=json.loads(_canonical(payload["outcome"])),
			)
		except (
			OSError,
			ValueError,
			TypeError,
			KeyError,
			RecursionError,
			DistributionError,
		):
			return None

	def _make_room(self, target: Path, key: bytes) -> bool:
		records: list[Path] = []
		with os.scandir(self.root) as directory:
			for entry in directory:
				path = Path(entry.path)
				if not _is_record_path(path):
					continue
				records.append(path)
				if len(records) > _MAX_RECORDS:
					return False
		if target in records or len(records) < _MAX_RECORDS:
			return True
		records.sort(key=lambda path: (path.lstat().st_mtime_ns, path.name))
		for path in records:
			try:
				payload = self._load(path, key, expected_digest=path.stem)
			except (OSError, ValueError, TypeError, KeyError, RecursionError):
				continue
			if payload is not None:
				path.unlink()
				return True
		return False

	def publish(
		self,
		identity: dict[str, Any],
		*,
		producer: EvidenceProducer,
		scope: dict[str, Any],
		outcome: dict[str, Any],
	) -> None:
		"""Publish one fully parsed provider result; unsafe or unavailable writes are skipped."""
		if time.monotonic() >= self.deadline:
			return
		try:
			if (
				not isinstance(identity, dict)
				or not isinstance(scope, dict)
				or not isinstance(outcome, dict)
				or not isinstance(producer, EvidenceProducer)
			):
				return
			identity = json.loads(_canonical(identity), object_pairs_hook=_unique_object)
			scope = json.loads(_canonical(scope), object_pairs_hook=_unique_object)
			outcome = json.loads(_canonical(outcome), object_pairs_hook=_unique_object)
			if (
				identity.get("engine") != producer.engine.as_dict()
				or identity.get("scope") != scope
				or not valid_evidence_outcome(identity, outcome)
			):
				return
			digest = identity_digest(identity)
			payload = {
				"schema": _SCHEMA,
				"version": _VERSION,
				"identity": identity,
				"identity_sha256": digest,
				"complete": True,
				"producer": producer.as_dict(),
				"scope": scope,
				"outcome": outcome,
			}
			payload_bytes = _canonical(payload)
			if len(payload_bytes) + 100 > _MAX_RECORD_BYTES:
				return
			self._prepare()
			with locked(self.root / ".lock", 0):
				key = self._key(create=True)
				if not self._make_room(self.root / f"{digest}.json", key):
					return
				record = _canonical(
					{
						"payload": payload,
						"mac": hmac.digest(key, payload_bytes, "sha256").hex(),
					}
				)
				if len(record) <= _MAX_RECORD_BYTES:
					_write(self.root / f"{digest}.json", record)
		except (
			OSError,
			ValueError,
			TypeError,
			KeyError,
			RecursionError,
			DistributionError,
		):
			return
