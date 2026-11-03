"""Atomic JSON persistence and bounded OS locks for hook-owned state."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from typing import Any


class StorageBusyError(OSError):
	"""Another process retained a state lock beyond the allowed deadline."""


def atomic_bytes(path: Path, content: bytes) -> None:
	"""Replace one complete file without exposing a partial write."""
	path.parent.mkdir(parents=True, exist_ok=True)
	with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as target:
		staged = Path(target.name)
		try:
			target.write(content)
		except (OSError, TypeError, ValueError):
			target.close()
			staged.unlink(missing_ok=True)
			raise
	try:
		staged.replace(path)
	finally:
		staged.unlink(missing_ok=True)


def atomic_json(path: Path, document: dict[str, Any]) -> None:
	"""Replace one complete JSON document without exposing partial writes."""
	atomic_bytes(path, json.dumps(document, ensure_ascii=True).encode("utf-8"))


@contextmanager
def directory_transaction(directory: Path, *, deadline: float | None = None) -> Iterator[None]:
	"""Acquire within the caller deadline, or the default 100 ms for short transitions."""
	directory.mkdir(parents=True, exist_ok=True)
	with (directory / ".transaction.lock").open("a+b") as handle:
		if handle.tell() == 0:
			handle.write(b"0")
			handle.flush()
		lock_deadline = monotonic() + 0.1 if deadline is None else deadline
		while True:
			try:
				handle.seek(0)
				if os.name == "nt":
					import msvcrt

					msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
				else:
					import fcntl

					fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
				break
			except OSError:
				if monotonic() >= lock_deadline:
					raise StorageBusyError("state_busy") from None
				sleep(min(0.005, max(0.0, lock_deadline - monotonic())))
		try:
			yield
		finally:
			handle.seek(0)
			if os.name == "nt":
				msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
			else:
				fcntl.flock(handle, fcntl.LOCK_UN)  # type: ignore[attr-defined]
