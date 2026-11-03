"""Bind typing requests to exact post-writer bytes, including confirmed deletions."""

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


class TypingInputError(ValueError):
	"""A fixed recovery identifier; never source text or an exception payload."""


def input_identities(files: object) -> dict[str, str | None]:
	if not isinstance(files, list) or len(files) > MAX_FILES:
		raise TypingInputError("limits")
	identities: dict[str, str | None] = {}
	for item in files:
		if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
			raise TypingInputError("invalid_request")
		path, digest = item["path"], item["sha256"]
		if not isinstance(path, str) or path in {".", ""} or safe_relative(path) != path:
			raise TypingInputError("unsafe_path")
		if digest is not None and (not isinstance(digest, str) or DIGEST.fullmatch(digest) is None):
			raise TypingInputError("invalid_request")
		if path in identities and identities[path] != digest:
			raise TypingInputError("invalid_request")
		identities[path] = digest
	return identities


def verify_input(root: Path, path: str, digest: str | None) -> int:
	"""Return verified size; confirmed absence contributes no readable bytes."""
	try:
		full, _ = safe_path(path, root)
		if not full.is_relative_to(root.resolve()):
			raise TypingInputError("unsafe_path")
		if digest is None:
			if full.exists():
				raise TypingInputError("stale_input")
			return 0
		if not full.is_file() or full.stat().st_nlink != 1:
			raise TypingInputError("io")
		with full.open("rb") as stream:
			content = stream.read(MAX_FILE_BYTES + 1)
	except AttributionError as error:
		raise TypingInputError("unsafe_path") from error
	except OSError as error:
		raise TypingInputError("io") from error
	if len(content) > MAX_FILE_BYTES:
		raise TypingInputError("limits")
	if hashlib.sha256(content).hexdigest() != digest:
		raise TypingInputError("stale_input")
	return len(content)


def snapshot_inputs(root: Path, files: object, deadline: float) -> list[dict[str, Any]]:
	"""Verify complete attributed inputs without rewriting or recreating deleted paths."""
	identities = input_identities(files)
	if not root.is_absolute() or not root.is_dir():
		raise TypingInputError("unsafe_path")
	total = 0
	for path, digest in identities.items():
		if monotonic() >= deadline:
			raise TypingInputError("deadline")
		total += verify_input(root, path, digest)
		if total > MAX_BATCH_BYTES:
			raise TypingInputError("limits")
	if monotonic() >= deadline:
		raise TypingInputError("deadline")
	return [{"path": path, "sha256": digest} for path, digest in identities.items()]
