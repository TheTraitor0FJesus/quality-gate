"""Bound exact touched content before and after a read-only secret scan."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from time import monotonic
from typing import Any

from _shared.quality_events import (
	MAX_BATCH_BYTES,
	MAX_FILE_BYTES,
	MAX_FILES,
	AttributionError,
	safe_path,
)
from _shared.quality_feedback import safe_relative

DIGEST = re.compile(r"^[0-9a-f]{64}$")


class SecretInputError(ValueError):
	"""Only fixed recovery identifiers cross the scanner input boundary."""


def input_identities(files: object) -> dict[str, str]:
	if not isinstance(files, list) or len(files) > MAX_FILES:
		raise SecretInputError("limits")
	identities: dict[str, str] = {}
	for item in files:
		if not isinstance(item, dict):
			raise SecretInputError("invalid_request")
		path, digest = item.get("path"), item.get("sha256")
		if not isinstance(path, str) or safe_relative(path) != path or path in {".", ""}:
			raise SecretInputError("unsafe_path")
		if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
			raise SecretInputError("invalid_request")
		if path in identities and identities[path] != digest:
			raise SecretInputError("invalid_request")
		identities[path] = digest
	return identities


def snapshot_files(root: Path, files: object, deadline: float) -> list[dict[str, Any]]:
	"""Validate bounded raw bytes; decoding and detector policy belong to Gate."""
	identities = input_identities(files)
	if not root.is_absolute() or not root.is_dir():
		raise SecretInputError("unsafe_path")
	total = 0
	for path, digest in identities.items():
		if monotonic() >= deadline:
			raise SecretInputError("deadline")
		try:
			full, _ = safe_path(path, root)
			if not full.is_relative_to(root.resolve()) or not full.is_file():
				raise SecretInputError("io")
			with full.open("rb") as stream:
				content = stream.read(MAX_FILE_BYTES + 1)
		except AttributionError as error:
			raise SecretInputError("unsafe_path") from error
		except OSError as error:
			raise SecretInputError("io") from error
		total += len(content)
		if len(content) > MAX_FILE_BYTES or total > MAX_BATCH_BYTES:
			raise SecretInputError("limits")
		if hashlib.sha256(content).hexdigest() != digest:
			raise SecretInputError("stale_input")
	if monotonic() >= deadline:
		raise SecretInputError("deadline")
	return [{"path": path, "sha256": digest} for path, digest in identities.items()]
