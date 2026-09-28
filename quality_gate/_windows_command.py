"""Gated child-process launcher for the bounded Windows command supervisor."""

from __future__ import annotations

import os
import subprocess
import sys

_MARKER = "--quality-gate-bounded-child-v1"
_READY = b"\x00quality-gate-ready-v2\n"
_GATE = b"\x00quality-gate-run-v1\n"
_UNAVAILABLE = b"\x00quality-gate-command-unavailable-v1\n"
_START_FAILURE = 125
_UNAVAILABLE_EXIT = 127
_PROCESS_ID_BYTES = 4
_MINIMUM_ARGUMENTS = 2


def _write_all(file_descriptor: int, content: bytes) -> bool:
	while content:
		written = os.write(file_descriptor, content)
		if written == 0:
			return False
		content = content[written:]
	return True


def main(arguments: list[str] | None = None) -> int:
	"""Wait for parent containment, then run the exact requested command."""
	arguments = list(sys.argv[1:] if arguments is None else arguments)
	if len(arguments) < _MINIMUM_ARGUMENTS or arguments[0] != _MARKER:
		return _START_FAILURE
	command = arguments[1:]
	try:
		frame = _READY + os.getpid().to_bytes(_PROCESS_ID_BYTES, "little")
		if not _write_all(sys.stdout.fileno(), frame):
			return _START_FAILURE
		if sys.stdin.buffer.read(len(_GATE) + 1) != _GATE:
			return _START_FAILURE
		child = subprocess.Popen(
			command,
			stdin=subprocess.DEVNULL,
			stdout=None,
			stderr=None,
			close_fds=True,
		)
	except OSError:
		_write_all(sys.stderr.fileno(), _UNAVAILABLE)
		return _UNAVAILABLE_EXIT
	return child.wait()


if __name__ == "__main__":
	raise SystemExit(main())
