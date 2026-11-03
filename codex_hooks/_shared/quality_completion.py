"""Resolve shell completion from host-owned metadata without trusting stdout."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlsplit

MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
MAX_HISTORY_COMMANDS = 256
_HISTORY_OWNERS = (
	"WITH RECURSIVE owners(thread_id) AS (SELECT ? UNION "
	"SELECT json_extract(i.item_json, '$.agentThreadId') "
	"FROM owners o JOIN thread_items i ON i.thread_id=o.thread_id "
	"WHERE i.item_type='subAgentActivity' "
	"AND json_extract(i.item_json, '$.type')='subAgentActivity' "
	"AND json_extract(i.item_json, '$.kind')='started' "
	"AND json_type(i.item_json, '$.agentThreadId')='text' "
	"AND length(json_extract(i.item_json, '$.agentThreadId')) BETWEEN 1 AND 256 "
	"LIMIT 256) "
)


def canonical_path(path: Path) -> Path:
	value = str(path.resolve())
	if os.name == "nt" and value.startswith("\\\\?\\"):
		value = value.removeprefix("\\\\?\\")
		if value.startswith("UNC\\"):
			value = "\\\\" + value.removeprefix("UNC\\")
	return Path(os.path.normpath(value))


def transcript_path(payload: dict[str, Any]) -> Path | None:
	# Settings imports attribution; resolve host ownership only after module loading.
	from _shared.quality_events import MAX_EVENT_ID_CHARS, MAX_PATH_CHARS, is_junction, project_root
	from _shared.quality_settings import default_settings_path

	identities = [payload.get(name) for name in ("session_id", "turn_id", "tool_use_id")]
	if any(
		not isinstance(item, str) or not item or len(item) > MAX_EVENT_ID_CHARS
		for item in identities
	):
		return None
	raw = payload.get("transcript_path")
	if not isinstance(raw, str) or len(raw) > MAX_PATH_CHARS:
		return None
	path = Path(raw)
	if not path.is_absolute() or any(
		parent.is_symlink() or is_junction(parent) for parent in (path, *path.parents)
	):
		return None
	path = canonical_path(path)
	sessions = default_settings_path().parents[2] / "sessions"
	if (
		not path.is_relative_to(canonical_path(sessions))
		or path.is_relative_to(project_root(Path(payload["cwd"])))
		or not path.name.startswith("rollout-")
		or not path.name.endswith(f"-{payload['session_id']}.jsonl")
	):
		return None
	return path


def completion_record(record: object, call: str) -> dict[str, Any] | None:
	if not isinstance(record, dict) or record.get("type") != "event_msg":
		return None
	payload = record.get("payload")
	if not isinstance(payload, dict) or payload.get("type") != "item_completed":
		return None
	item = payload.get("item")
	if not isinstance(item, dict) or item.get("id") != call:
		return None
	return payload


def cwd_is_covered(directory: Path, payload: dict[str, Any]) -> bool:
	from _shared.quality_events import PROJECT_MARKERS, project_root

	directory = canonical_path(directory)
	workdir = payload["tool_input"].get("workdir")
	if workdir:
		return directory == canonical_path(Path(workdir))
	observed = canonical_path(project_root(Path(payload["cwd"])))
	if not directory.is_relative_to(observed):
		return False
	selected = canonical_path(project_root(directory))
	return selected == observed or not any(
		(selected / marker).exists() for marker in PROJECT_MARKERS
	)


def verified_code(record: dict[str, Any], payload: dict[str, Any]) -> int | None:
	if (
		record.get("thread_id") != payload["session_id"]
		or record.get("turn_id") != payload["turn_id"]
	):
		return None
	item = record["item"]
	code = item.get("exit_code")
	if (
		item.get("type") != "CommandExecution"
		or type(code) is not int
		or item.get("status") != ("completed" if code == 0 else "failed")
		or not isinstance(item.get("cwd"), str)
	):
		return None
	uri = urlsplit(item["cwd"])
	if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
		return None
	directory = unquote(uri.path)
	if os.name == "nt" and directory.startswith("/") and directory[2:3] == ":":
		directory = directory[1:]
	if not cwd_is_covered(Path(directory), payload):
		return None
	return code


def transcript_code(path: Path, payload: dict[str, Any], deadline: float | None) -> int | None:
	with path.open("rb") as stream:
		size = stream.seek(0, 2)
		stream.seek(max(0, size - MAX_TRANSCRIPT_BYTES))
		content = stream.read(MAX_TRANSCRIPT_BYTES)
	lines = content.splitlines()
	if size > MAX_TRANSCRIPT_BYTES:
		lines = lines[1:]
	codes: list[int] = []
	call = payload["tool_use_id"]
	for line in lines:
		if deadline is not None and monotonic() >= deadline:
			return None
		if call.encode("utf-8") not in line:
			continue
		try:
			record = completion_record(json.loads(line), call)
		except (ValueError, UnicodeError):
			continue
		if record is None:
			continue
		code = verified_code(record, payload)
		if code is None or codes:
			return None
		codes.append(code)
	return codes[0] if codes else None


def shell_exit_code(payload: dict[str, Any], *, deadline: float | None = None) -> int | None:
	"""Accept explicit structured status or one matching trusted transcript completion."""
	response = payload.get("tool_response")
	if isinstance(response, dict) and type(response.get("exit_code")) is int:
		return response["exit_code"]
	try:
		path = transcript_path(payload)
		code = transcript_code(path, payload, deadline) if path is not None else None
		if code is not None:
			return code
	except (OSError, ValueError, TypeError, KeyError):
		pass
	for call, turn, status, code, cwd, _turn_status, _unstarted in _history_commands(
		payload, deadline
	):
		if (
			call == payload.get("tool_use_id")
			and turn == payload.get("turn_id")
			and _finished_command(status, code)
			and isinstance(cwd, str)
			and cwd_is_covered(Path(cwd), payload)
		):
			return code
	return None


def _finished_command(status: object, code: object) -> bool:
	return type(code) is int and status == ("completed" if code == 0 else "failed")


def _history_commands(payload: dict[str, Any], deadline: float | None) -> list[tuple[Any, ...]]:
	"""Read bounded command identities from the installed host's optional history adapter."""
	return _history_query(
		payload,
		"SELECT i.item_id, i.turn_id, json_extract(i.item_json, '$.status'), "
		"json_extract(i.item_json, '$.exitCode'), json_extract(i.item_json, '$.cwd'), "
		"t.status, json_type(i.item_json, '$.processId')='null' "
		"FROM owners o JOIN thread_items i ON i.thread_id=o.thread_id "
		"JOIN thread_turns t "
		"ON t.thread_id=i.thread_id AND t.turn_id=i.turn_id "
		"WHERE i.item_type='commandExecution' "
		"AND json_extract(i.item_json, '$.type')='commandExecution' "
		"AND json_extract(i.item_json, '$.id')=i.item_id "
		"AND json_type(i.item_json, '$.exitCode')='integer' "
		"ORDER BY i.rollout_ordinal DESC LIMIT ?",
		(payload.get("session_id"), MAX_HISTORY_COMMANDS),
		deadline,
	)


def _history_query(
	payload: dict[str, Any], query: str, parameters: tuple[Any, ...], deadline: float | None
) -> list[tuple[Any, ...]]:
	"""Keep optional host reads read-only, bounded and outside the captured project."""
	from python_types.freshness import plain_path

	from _shared.quality_events import MAX_EVENT_ID_CHARS, project_root
	from _shared.quality_settings import default_settings_path

	session = payload.get("session_id")
	if not isinstance(session, str) or not 0 < len(session) <= MAX_EVENT_ID_CHARS:
		return []
	path = default_settings_path().parents[2] / "thread_history_1.sqlite"
	try:
		plain_path(path)
		path = canonical_path(path)
		cwd = payload.get("cwd", payload.get("root"))
		if not isinstance(cwd, str) or path.is_relative_to(project_root(Path(cwd))):
			return []
		if not path.is_file() or path.stat().st_nlink != 1:
			return []
		limit = min(deadline, monotonic() + 0.1) if deadline is not None else monotonic() + 0.1
		if monotonic() >= limit:
			return []
		with closing(
			sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.05)
		) as database:
			database.set_progress_handler(lambda: int(monotonic() >= limit), 100)
			return database.execute(_HISTORY_OWNERS + query, parameters).fetchall()
	except (OSError, ValueError, sqlite3.Error):
		return []


def shell_snapshot_unrecorded(
	snapshot: dict[str, Any], key: str, *, deadline: float | None = None
) -> bool:
	"""Identify a terminal owner turn lacking this call, without claiming completion."""
	from _shared.quality_events import MAX_EVENT_ID_CHARS

	identities = [snapshot.get(name) for name in ("session_id", "turn_id", "tool_use_id")]
	if any(
		not isinstance(item, str) or not 0 < len(item) <= MAX_EVENT_ID_CHARS for item in identities
	):
		return False
	session, turn, call = identities
	if hashlib.sha256(json.dumps([session, call]).encode()).hexdigest() != key:
		return False
	rows = _history_query(
		snapshot,
		"SELECT t.status, EXISTS (SELECT 1 FROM thread_items i "
		"WHERE i.thread_id=t.thread_id AND i.turn_id=t.turn_id AND i.item_id=?) "
		"FROM owners o JOIN thread_turns t ON t.thread_id=o.thread_id "
		"WHERE t.turn_id=? LIMIT 2",
		(session, call, turn),
		deadline,
	)
	return (
		len(rows) == 1 and rows[0][0] in {"completed", "interrupted", "failed"} and rows[0][1] == 0
	)


def shell_snapshot_finished(
	snapshot: dict[str, Any], key: str, *, terminal_turn: bool, deadline: float | None = None
) -> bool:
	"""Retire host-terminal calls by identity, independently of mutation coverage."""
	root = snapshot.get("root")
	if not isinstance(root, str) or not Path(root).is_absolute():
		return False
	for call, turn, status, code, cwd, turn_status, unstarted in _history_commands(
		snapshot, deadline
	):
		if (
			not isinstance(call, str)
			or not isinstance(cwd, str)
			or not Path(cwd).is_absolute()
			or not _finished_command(status, code)
		):
			continue
		identity = hashlib.sha256(json.dumps([snapshot["session_id"], call]).encode()).hexdigest()
		# A failed launch can retain the session cwd in Pre while history records
		# another execution cwd. That prevents coverage, not retirement of this call.
		if identity != key:
			continue
		if snapshot.get("turn_id", turn) != turn or snapshot.get("tool_use_id", call) != call:
			continue
		# The host's failed-launch sentinel with explicit null processId means no
		# exec session started. It cannot mutate later, even in the current turn.
		return (
			not terminal_turn
			or turn_status in {"completed", "interrupted", "failed"}
			or (code == -1 and unstarted == 1)
		)
	return False
