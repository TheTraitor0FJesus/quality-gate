"""Supervised protocol worker; all failures cross one source-free boundary."""

from __future__ import annotations

import sys
import time
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .distribution import DistributionError
from .edit_protocol import (
	DELIVERY_RESERVE_SECONDS,
	MAX_REQUEST_BYTES,
	EditError,
	background_identity,
	decode_request,
	encode,
)
from .runtime import RuntimeUnavailable


def _handlers(
	*, secrets: bool, types: bool
) -> tuple[
	Callable[[dict[str, Any], float, Path], dict[str, Any]], Callable[[str], dict[str, Any]]
]:
	if types:
		from .edit_types import execute
		from .edit_types import typing_unchecked as unchecked
	elif secrets:
		from .edit_secrets import execute
		from .edit_secrets import secret_unchecked as unchecked
	else:
		from .edit_check import execute
		from .edit_protocol import unchecked
	return execute, unchecked


def main(workspace: Path, *, secrets: bool = False, types: bool = False) -> None:
	execute, unchecked = _handlers(secrets=secrets, types=types)
	request = None
	background: dict[str, Any] = {}
	try:
		request = decode_request(
			sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1), secrets=secrets, types=types
		)
		deadline = (
			time.monotonic()
			+ request["deadline_unix_ms"] / 1000
			- time.time()
			- 2 * DELIVERY_RESERVE_SECONDS
		)
		result = execute(request, deadline, workspace)
	except EditError as error:
		result = unchecked(error.reason)
		background = error.background
		if not types:
			result["stage"] = error.stage
	except DistributionError:
		result = unchecked("release")
	except RuntimeUnavailable:
		result = unchecked("runtime")
	except (OSError, UnicodeError):
		result = unchecked("io")
	except (ValueError, TypeError, KeyError, zipfile.BadZipFile):
		result = unchecked("invalid_request")
	# This is a process protocol boundary. Unexpected implementation failures must
	# never leak an exception containing source, configuration or subprocess output.
	except Exception:
		result = unchecked("tool_output")
	if types:
		result.update(background_identity(request) | background)
	sys.stdout.buffer.write(encode(result))
	raise SystemExit({"unchecked": 2, "failed": 1}.get(result["status"], 0))
