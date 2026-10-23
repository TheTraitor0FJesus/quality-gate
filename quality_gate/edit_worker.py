"""Supervised protocol worker; all failures cross one source-free boundary."""

from __future__ import annotations

import sys
import time
import zipfile
from pathlib import Path

from .distribution import DistributionError
from .edit_protocol import (
	DELIVERY_RESERVE_SECONDS,
	MAX_REQUEST_BYTES,
	EditError,
	decode_request,
	encode,
)
from .runtime import RuntimeUnavailable


def main(workspace: Path, *, secrets: bool = False, integrity: bool = False) -> None:
	if secrets and integrity:
		raise ValueError("Choose one edit reader")
	if integrity:
		from .edit_integrity import execute
		from .edit_integrity import integrity_unchecked as unchecked
	elif secrets:
		from .edit_secrets import execute
		from .edit_secrets import secret_unchecked as unchecked
	else:
		from .edit_check import execute
		from .edit_protocol import unchecked
	try:
		request = decode_request(
			sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1),
			secrets=secrets,
			integrity=integrity,
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
	sys.stdout.buffer.write(encode(result))
	raise SystemExit({"unchecked": 2, "failed": 1}.get(result["status"], 0))
