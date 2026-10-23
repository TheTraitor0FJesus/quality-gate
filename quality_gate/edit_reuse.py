"""Bounded authenticated storage for complete provider-owned edit results."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import tempfile
import time
from pathlib import Path
from typing import Any

from .distribution import DistributionError, default_cache_root, locked
from .edit_protocol import MAX_RESULT_BYTES

_SLOTS = 16
_KEY_BYTES = 32
_RECORD_BYTES = MAX_RESULT_BYTES + 512


def identity_digest(identity: dict[str, Any]) -> str:
	"""Hash identities, never source or configuration contents retained by the cache."""
	return hashlib.sha256(_json(identity)).hexdigest()


def _json(value: object) -> bytes:
	return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()


def _read(path: Path, limit: int) -> bytes:
	metadata = path.lstat()
	if (
		not stat.S_ISREG(metadata.st_mode)
		or metadata.st_nlink != 1
		or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
	):
		raise ValueError("invalid cache file")
	with path.open("rb") as stream:
		content = stream.read(limit + 1)
	if len(content) > limit:
		raise ValueError("oversized cache file")
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


class ResultReuse:
	"""Optional optimization: any unavailable or invalid record takes the normal path."""

	def __init__(self, deadline: float) -> None:
		self.root = default_cache_root() / "edit-results-v1"
		self.deadline = deadline

	def _prepare(self) -> None:
		self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
		metadata = self.root.lstat()
		if (
			self.root.is_symlink()
			or getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
		):
			raise ValueError("invalid cache directory")

	def _slot(self, identity: str) -> Path:
		# ponytail: 16 direct-mapped slots bound storage without discovery; collisions
		# only lose reuse. Increase or replace after measured eviction pressure.
		return self.root / f"{int(identity[:8], 16) % _SLOTS:02d}.json"

	def _key(self, *, create: bool = False) -> bytes:
		path = self.root / "key"
		if create and not path.exists():
			_write(path, secrets.token_bytes(_KEY_BYTES))
		key = _read(path, _KEY_BYTES)
		if len(key) != _KEY_BYTES:
			raise ValueError("invalid cache key")
		return key

	def lookup(self, identity: str | None) -> dict[str, Any] | None:
		if identity is None or time.monotonic() >= self.deadline:
			return None
		try:
			self._prepare()
			with locked(self.root / ".lock", 0):
				key = self._key()
				record = json.loads(_read(self._slot(identity), _RECORD_BYTES))
				payload = record["payload"]
				if not hmac.compare_digest(
					record["mac"], hmac.digest(key, _json(payload), "sha256").hex()
				):
					return None
				result = payload["result"]
				if payload["identity"] != identity or result["status"] not in {"passed", "failed"}:
					return None
				return result if isinstance(result, dict) else None
		except (OSError, ValueError, TypeError, KeyError, RecursionError, DistributionError):
			# This optional cache is not a prerequisite; corrupt records never attest a verdict.
			return None

	def remember(self, identity: str | None, result: dict[str, Any]) -> None:
		if (
			identity is None
			or result["status"] not in {"passed", "failed"}
			or time.monotonic() >= self.deadline
		):
			return
		try:
			self._prepare()
			with locked(self.root / ".lock", 0):
				key = self._key(create=True)
				payload = {"identity": identity, "result": result}
				record = _json(
					{"payload": payload, "mac": hmac.digest(key, _json(payload), "sha256").hex()}
				)
				if len(record) <= _RECORD_BYTES:
					_write(self._slot(identity), record)
		except (OSError, ValueError, TypeError, RecursionError, DistributionError):
			# Completed checks remain valid when optional cache persistence is unavailable.
			return
