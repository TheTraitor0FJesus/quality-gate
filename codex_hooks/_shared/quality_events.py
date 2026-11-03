"""Bounded, content-addressed attribution of completed Codex mutations.

This module identifies content only. Mechanism code owns checking and writing it.
The executable boundary accepts one Codex event and emits the version 1 handoff.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from time import time as wall_time
from typing import Any

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_completion import (
	shell_exit_code,
	shell_snapshot_finished,
	shell_snapshot_unrecorded,
)
from _shared.quality_diagnostics import (
	DiagnosticError,
	failure,
	plain,
	read_record,
	safe_diagnostic,
	scope,
)
from _shared.quality_storage import StorageBusyError, atomic_json, directory_transaction

SCHEMA = 1
PROJECT_MARKERS = ("quality-gate.toml", ".git")
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_EVENT_ID_CHARS = 256
MAX_FILES = 10
MAX_FILE_BYTES = 128 * 1024
MAX_BATCH_BYTES = 1024 * 1024
MAX_PATH_CHARS = 4096
MAX_SNAPSHOT_ENTRIES = 4096
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
SNAPSHOT_DEADLINE_SECONDS = 10.0
STALE_INCOMPLETE_SNAPSHOT_SECONDS = 30.0
SNAPSHOT_EXCLUDED_DIRS = {
	"__pycache__",
	".mypy_cache",
	".pytest_cache",
	".ruff_cache",
	".venv",
	".quality-gate-tmp",
}
EXECUTION_ENVELOPE = re.compile(
	r"\AExit code: (-?[0-9]{1,10})\r?\n"
	r"Wall time: [0-9]+(?:\.[0-9]+)? seconds\r?\nOutput:\r?\n"
)


class AttributionError(DiagnosticError):
	"""An event cannot establish complete, safe touched-content coverage."""


def project_root(directory: Path) -> Path:
	"""Use the nearest project marker, including a Git worktree's .git file."""
	directory = directory.resolve()
	for candidate in (directory, *directory.parents):
		if any((candidate / marker).exists() for marker in PROJECT_MARKERS):
			return candidate
	return directory


def is_junction(path: Path) -> bool:
	"""Check junctions when running on Python versions that expose the API."""
	check_junction = getattr(path, "is_junction", None)
	return bool(check_junction()) if callable(check_junction) else False


def safe_path(raw: str, cwd: Path) -> tuple[Path, Path]:
	"""Resolve explicit absolute paths while rejecting link traversal and escapes."""
	if not raw or "\x00" in raw or len(raw) > MAX_PATH_CHARS:
		raise AttributionError("unsafe_path")
	path = Path(raw)
	absolute = Path(os.path.abspath(path if path.is_absolute() else cwd / path))
	for parent in (absolute, *absolute.parents):
		if parent.is_symlink() or is_junction(parent):
			raise AttributionError("unsafe_path")
	absolute = absolute.resolve()
	root = project_root(cwd)
	if not absolute.is_relative_to(root):
		if not path.is_absolute():
			raise AttributionError("unsafe_path")
		root = project_root(absolute.parent)
	return absolute, root


def content_record(path: Path, root: Path, operation: str) -> dict[str, Any]:
	"""Hash a complete regular file, or record a confirmed deletion without reading."""
	identity: dict[str, Any] = {
		"root": str(root),
		"path": path.relative_to(root).as_posix(),
		"operation": operation,
		"sha256": None,
		"size": 0,
	}
	if operation == "deleted":
		if path.exists():
			raise AttributionError("mutation_not_confirmed")
		return identity
	if not path.is_file():
		raise AttributionError("unreadable_file")
	with path.open("rb") as source:
		content = source.read(MAX_FILE_BYTES + 1)
	if len(content) > MAX_FILE_BYTES:
		raise AttributionError(
			"file_size_limit",
			limit_dimension="file_bytes",
			observed=len(content),
			allowed=MAX_FILE_BYTES,
			root=str(root),
			input_paths=[path.relative_to(root).as_posix()],
		)
	identity.update(sha256=hashlib.sha256(content).hexdigest(), size=len(content))
	return identity


def text_execution_result(response: str) -> tuple[int, str] | None:
	"""Read the observed Codex envelope without interpreting its output as status."""
	match = EXECUTION_ENVELOPE.match(response)
	if match is None:
		return None
	return int(match[1]), response[match.end() :]


def response_text(response: object) -> str:
	"""Read only documented text wrappers, never stringify arbitrary tool output."""
	if isinstance(response, str):
		if (completed := text_execution_result(response)) is not None:
			return completed[1] if completed[0] == 0 else ""
		return response
	if isinstance(response, dict):
		if response.get("isError") or response.get("exit_code", 0) != 0:
			return ""
		for key in ("output", "text"):
			if isinstance(response.get(key), str):
				return response[key]
		content = response.get("content")
		if isinstance(content, list):
			return "\n".join(response_text(item) for item in content)
	return ""


def patch_paths(command: str) -> list[tuple[str, str]]:
	"""Read patch metadata; source lines prefixed with +/- cannot become paths."""
	lines = command.strip().splitlines()
	if not lines or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
		raise AttributionError("invalid_patch")
	paths: list[tuple[str, str]] = []
	for line in lines[1:-1]:
		for prefix, operation in (
			("*** Add File: ", "added"),
			("*** Update File: ", "modified"),
			("*** Delete File: ", "deleted"),
		):
			if line.startswith(prefix):
				paths.append((line[len(prefix) :], operation))
		if line.startswith("*** Move to: "):
			if not paths or paths[-1][1] != "modified":
				raise AttributionError("invalid_patch")
			paths[-1] = (line[len("*** Move to: ") :], "renamed")
	if not paths:
		raise AttributionError("invalid_patch")
	return paths


def attribute_patch(payload: dict[str, Any]) -> list[dict[str, Any]]:
	tool_input = payload.get("tool_input")
	if not isinstance(tool_input, dict) or not isinstance(tool_input.get("command"), str):
		raise AttributionError("invalid_patch")
	text = response_text(payload.get("tool_response"))
	if not text.startswith("Success. Updated the following files:"):
		raise AttributionError("mutation_not_confirmed")
	cwd = Path(tool_input.get("workdir") or payload["cwd"]).resolve()
	confirmed = set(text.splitlines()[1:])
	files: dict[str, dict[str, Any]] = {}
	for raw, operation in patch_paths(tool_input["command"]):
		letter = {"added": "A", "modified": "M", "renamed": "M", "deleted": "D"}[operation]
		if f"{letter} {raw}" not in confirmed:
			raise AttributionError("mutation_not_confirmed")
		path, root = safe_path(raw, cwd)
		key = os.path.normcase(str(path))
		if key not in files:
			files[key] = content_record(path, root, operation)
		if len(files) > MAX_FILES:
			raise AttributionError(
				"file_count_limit",
				limit_dimension="file_count",
				observed=len(files),
				allowed=MAX_FILES,
			)
	if sum(item["size"] for item in files.values()) > MAX_BATCH_BYTES:
		raise AttributionError(
			"batch_size_limit",
			limit_dimension="batch_bytes",
			observed=sum(item["size"] for item in files.values()),
			allowed=MAX_BATCH_BYTES,
		)
	return list(files.values())


def snapshot_digest(path: Path, deadline: float, available_bytes: int) -> tuple[str, int]:
	"""Hash one file within the remaining snapshot byte and time budgets."""
	digest = hashlib.sha256()
	size = 0
	with path.open("rb") as source:
		while block := source.read(128 * 1024):
			size += len(block)
			if size > available_bytes:
				raise AttributionError(
					"snapshot_limit",
					limit_dimension="snapshot_bytes",
					observed=MAX_SNAPSHOT_BYTES - available_bytes + size,
					allowed=MAX_SNAPSHOT_BYTES,
				)
			if monotonic() >= deadline:
				raise AttributionError("snapshot_limit", limit_dimension="snapshot_seconds")
			digest.update(block)
	return digest.hexdigest(), size


def snapshot_entry_limit(entries: int, deadline: float) -> None:
	if entries > MAX_SNAPSHOT_ENTRIES:
		raise AttributionError(
			"snapshot_limit",
			limit_dimension="snapshot_entries",
			observed=entries,
			allowed=MAX_SNAPSHOT_ENTRIES,
		)
	if monotonic() >= deadline:
		raise AttributionError("snapshot_limit", limit_dimension="snapshot_seconds")


def snapshot_tree(root: Path, *, deadline: float | None = None) -> dict[str, dict[str, Any]]:
	"""Hash bounded project content, skipping Git metadata and generated runtimes/caches.

	ponytail: complete before/after tree hashing within 4096 entries/32 MiB;
	use a host-supported changed-path source if measured repositories exceed it.
	"""
	if deadline is None:
		deadline = monotonic() + SNAPSHOT_DEADLINE_SECONDS
	files: dict[str, dict[str, Any]] = {}
	pending = [root]
	entries = total = 0
	while pending:
		with os.scandir(pending.pop()) as children:
			for child in children:
				entries += 1
				snapshot_entry_limit(entries, deadline)
				path = Path(child.path)
				if path == root / ".git":
					continue
				if child.is_dir(follow_symlinks=False) and (
					path == root / "temp"
					or child.name in SNAPSHOT_EXCLUDED_DIRS
					or child.name.startswith(".quality-gate-edit-")
				):
					continue
				if child.is_symlink() or is_junction(path):
					files[path.relative_to(root).as_posix()] = {"link": True}
					continue
				if child.is_dir(follow_symlinks=False):
					pending.append(path)
					continue
				if not stat.S_ISREG(child.stat(follow_symlinks=False).st_mode):
					raise AttributionError("unsupported_file")
				digest, size = snapshot_digest(path, deadline, MAX_SNAPSHOT_BYTES - total)
				total += size
				files[path.relative_to(root).as_posix()] = {
					"sha256": digest,
					"size": size,
				}
	return files


@contextmanager
def state_transaction(state_dir: Path, *, deadline: float | None = None) -> Iterator[None]:
	"""Serialize only state transitions, with a bounded cross-process OS lock."""
	try:
		plain(state_dir / ".transaction.lock")
		with directory_transaction(state_dir, deadline=deadline):
			yield
	except StorageBusyError as error:
		raise AttributionError("snapshot_state_busy", operation="snapshot_store") from error


def clear_snapshot(
	state_dir: Path,
	group: Path,
	state_path: Path,
	*,
	deadline: float | None = None,
	expected_identity: os.stat_result | None = None,
) -> None:
	with state_transaction(state_dir, deadline=deadline):
		plain(state_path)
		if expected_identity is not None and not os.path.samestat(
			expected_identity, state_path.stat()
		):
			raise AttributionError("unsafe_path", operation="snapshot_cleanup")
		state_path.unlink(missing_ok=True)
		if not any(group.glob("*.json")):
			(group / ".conflict").unlink(missing_ok=True)
			group.rmdir()


def clear_session_snapshots(state_dir: Path, session_id: object) -> int:
	"""Clear snapshots owned by a session the host has ended.

	The host's terminal event permits cleanup of both complete and interrupted
	captures. Legacy snapshots without ownership metadata remain untouched.
	"""
	return _clear_owned_snapshots(state_dir, session_id)


def clear_turn_snapshots(state_dir: Path, session_id: object, turn_id: object) -> int:
	"""Discard only captures owned by the host's finished or interrupted turn."""
	if not isinstance(turn_id, str) or not 0 < len(turn_id) <= MAX_EVENT_ID_CHARS:
		return 0
	return _clear_owned_snapshots(state_dir, session_id, turn_id)


def _clear_owned_snapshots(state_dir: Path, session_id: object, turn_id: str | None = None) -> int:
	if not isinstance(session_id, str) or not 0 < len(session_id) <= MAX_EVENT_ID_CHARS:
		return 0
	removed = 0
	with state_transaction(state_dir):
		for group in state_dir.iterdir():
			if not group.is_dir() or re.fullmatch(r"[0-9a-f]{64}", group.name) is None:
				continue
			for state_path in group.glob("*.json"):
				try:
					snapshot = read_record(state_path, MAX_EVENT_BYTES)
				except (OSError, ValueError):
					continue
				if (
					isinstance(snapshot, dict)
					and snapshot.get("session_id") == session_id
					and (turn_id is None or snapshot.get("turn_id") == turn_id)
					and (
						turn_id is None
						or shell_snapshot_finished(snapshot, state_path.stem, terminal_turn=False)
					)
				):
					state_path.unlink(missing_ok=True)
					removed += 1
			if not any(group.glob("*.json")):
				(group / ".conflict").unlink(missing_ok=True)
				group.rmdir()
	return removed


def shell_event_key(payload: dict[str, Any]) -> str:
	"""Return a bounded key for one identifiable shell event pair."""
	identifiers = (payload.get("session_id"), payload.get("tool_use_id"))
	if any(
		not isinstance(value, str) or not value or len(value) > MAX_EVENT_ID_CHARS
		for value in identifiers
	):
		raise AttributionError("missing_event_identity")
	return hashlib.sha256(json.dumps(identifiers).encode("utf-8")).hexdigest()


def shell_state_location(
	payload: dict[str, Any], state_dir: Path, key: str
) -> tuple[Path, Path, Path]:
	"""Resolve the project root and external snapshot location for one shell call."""
	tool_input = payload.get("tool_input")
	if not isinstance(tool_input, dict):
		raise AttributionError("invalid_event")
	cwd = Path(tool_input.get("workdir") or payload["cwd"]).resolve()
	root = project_root(cwd)
	if state_dir.resolve().is_relative_to(root):
		raise AttributionError("state_inside_project")
	group = state_dir / hashlib.sha256(str(root).encode("utf-8")).hexdigest()
	return root, group, group / f"{key}.json"


def discard_shell_snapshot(payload: dict[str, Any], state_dir: Path) -> None:
	"""Remove this call's capture when its PreToolUse response prevents execution."""
	key = shell_event_key(payload)
	_root, group, state_path = shell_state_location(payload, state_dir, key)
	clear_snapshot(state_dir, group, state_path)


def _recover_finished_snapshots(
	root: Path, group: Path, deadline: float | None = None
) -> list[object]:
	"""Reclaim incomplete captures or host-confirmed finished commands and turns."""
	now = wall_time()
	unconfirmed: list[object] = []
	for state_path in group.glob("*.json"):
		try:
			snapshot = read_record(state_path, MAX_EVENT_BYTES)
			modified = state_path.stat().st_mtime
		except (OSError, ValueError):
			continue
		if (
			isinstance(snapshot, dict)
			and snapshot.get("root") == str(root)
			and "before" in snapshot
			and snapshot["before"] is None
			and now - modified >= STALE_INCOMPLETE_SNAPSHOT_SECONDS
		):
			state_path.unlink(missing_ok=True)
		elif (
			isinstance(snapshot, dict)
			and snapshot.get("root") == str(root)
			and shell_snapshot_finished(
				snapshot, state_path.stem, terminal_turn=True, deadline=deadline
			)
		):
			state_path.unlink(missing_ok=True)
		elif (
			isinstance(snapshot, dict)
			and snapshot.get("root") == str(root)
			and shell_snapshot_unrecorded(snapshot, state_path.stem, deadline=deadline)
		):
			archive = group.parent / "unconfirmed" / group.name / state_path.name
			atomic_json(archive, {"reason": "abandoned_shell_call", "snapshot": snapshot})
			state_path.unlink(missing_ok=True)
			unconfirmed.append(snapshot.get("before"))
	if not any(group.glob("*.json")):
		(group / ".conflict").unlink(missing_ok=True)
	return unconfirmed


def capture_shell_snapshot(
	root: Path,
	state_dir: Path,
	group: Path,
	state_path: Path,
	*,
	session_id: str,
	turn_id: object = None,
	tool_use_id: object = None,
	deadline: float | None = None,
) -> bool:
	"""Keep independent call observations; overlapping observations never own writes."""
	owner = {"root": str(root), "session_id": session_id}
	for name, value in (("turn_id", turn_id), ("tool_use_id", tool_use_id)):
		if isinstance(value, str) and 0 < len(value) <= MAX_EVENT_ID_CHARS:
			owner[name] = value
	plain(state_dir)
	plain(state_path)
	with state_transaction(state_dir, deadline=deadline):
		plain(state_path)
		group.mkdir(exist_ok=True)
		unconfirmed = _recover_finished_snapshots(root, group, deadline)
		if state_path.exists():
			raise AttributionError("duplicate_pre_event")
		overlapping = False
		for other_path in group.glob("*.json"):
			overlapping = True
			try:
				plain(other_path)
				other = read_record(other_path, MAX_EVENT_BYTES)
				if isinstance(other, dict) and other.get("root") == str(root):
					publish_snapshot(other_path, {**other, "concurrent": True})
			except (OSError, ValueError):
				# Preserve unreadable owners; this call remains read-only regardless.
				overlapping = True
		publish_snapshot(state_path, {**owner, "before": None, "concurrent": overlapping})
	try:
		before = snapshot_tree(root, deadline=deadline)
		with state_transaction(state_dir, deadline=deadline):
			current = read_record(state_path, MAX_EVENT_BYTES)
			validate_snapshot_owner(current, root, owner)
			publish_snapshot(state_path, {**current, "before": before})
		return any(previous != before for previous in unconfirmed)
	except (AttributionError, OSError) as error:
		try:
			with state_transaction(state_dir, deadline=deadline):
				plain(state_path)
				current = read_record(state_path)
				if all(current.get(name) == value for name, value in owner.items()):
					publish_snapshot(
						state_path, {**current, "failure": failure(error, "snapshot_pre", "io")}
					)
		except (OSError, ValueError):
			pass  # Preserve ambiguous ownership and the originating failure.
		raise


def publish_snapshot(path: Path, document: dict[str, Any]) -> None:
	"""Keep the storage boundary when atomic publication itself fails."""
	try:
		atomic_json(path, document)
	except OSError as error:
		raise AttributionError(
			"io", **failure(error, "snapshot_store", "io")["diagnostic"]
		) from error


def validate_snapshot_owner(
	snapshot: dict[str, Any], root: Path, expected_owner: dict[str, Any] | None
) -> None:
	if not isinstance(snapshot, dict) or snapshot.get("root") != str(root):
		raise AttributionError("snapshot_root_changed")
	if expected_owner and any(
		snapshot.get(name) != value for name, value in expected_owner.items()
	):
		raise AttributionError("invalid_snapshot")
	if "failure" in snapshot and (
		not isinstance(snapshot["failure"], dict)
		or snapshot["failure"].get("status") != "unchecked"
	):
		raise AttributionError("invalid_snapshot")


def read_shell_snapshots(
	root: Path,
	state_dir: Path,
	group: Path,
	state_path: Path,
	*,
	deadline: float | None = None,
	expected_owner: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], bool]:
	"""Read and consume one valid pre-event snapshot and its matching post-event tree."""
	plain(state_path)
	if not state_path.is_file():
		raise AttributionError("snapshot_missing", operation="snapshot_read")
	consumable = False
	before_identity = state_path.stat()
	try:
		if state_path.stat().st_size > MAX_EVENT_BYTES:
			raise AttributionError("invalid_snapshot")
		snapshot = read_record(state_path, MAX_EVENT_BYTES)
		if not os.path.samestat(before_identity, state_path.stat()):
			raise AttributionError("unsafe_path")
		validate_snapshot_owner(snapshot, root, expected_owner)
		consumable = True
		if isinstance(snapshot.get("failure"), dict):
			stored = snapshot["failure"]
			raise AttributionError(
				stored.get("reason", "invalid_snapshot"),
				**safe_diagnostic(stored.get("diagnostic")),
			)
		before = snapshot["before"]
		if not isinstance(before, dict):
			raise AttributionError("invalid_snapshot")
		after = snapshot_tree(root, deadline=deadline)
		with state_transaction(state_dir, deadline=deadline):
			current = read_record(state_path, MAX_EVENT_BYTES)
			concurrent = current.get("concurrent", False) or (group / ".conflict").exists()
		return before, after, bool(concurrent)
	finally:
		primary = sys.exception()
		if consumable:
			try:
				clear_snapshot(
					state_dir,
					group,
					state_path,
					deadline=deadline,
					expected_identity=before_identity,
				)
			except (OSError, ValueError):
				if primary is None:
					raise


def changed_shell_files(
	root: Path,
	before: dict[str, dict[str, Any]],
	after: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
	"""Build complete content records for the bounded set of changed paths."""
	changed = sorted(
		name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
	)
	if len(changed) > MAX_FILES:
		raise AttributionError(
			"file_count_limit",
			limit_dimension="file_count",
			observed=len(changed),
			allowed=MAX_FILES,
		)
	files = []
	for name in changed:
		path, _ = safe_path(name, root)
		operation = (
			"deleted" if name not in after else "added" if name not in before else "modified"
		)
		files.append(content_record(path, root, operation))
	if sum(item["size"] for item in files) > MAX_BATCH_BYTES:
		raise AttributionError(
			"batch_size_limit",
			limit_dimension="batch_bytes",
			observed=sum(item["size"] for item in files),
			allowed=MAX_BATCH_BYTES,
		)
	return files


def classify_shell_result(code: int | None, files: list[dict[str, Any]]) -> dict[str, Any]:
	"""Report attributed changes only when the shell call completed successfully."""
	diagnostic = {"operation": "shell_completion", "phase": "post"}
	if code is None:
		return {
			"status": "unchecked",
			"reason": "mutation_not_confirmed",
			"files": files,
			"diagnostic": diagnostic,
		}
	if code != 0:
		if not files:
			return {"status": "not_applicable", "reason": "no_changes", "files": []}
		return {
			"status": "unchecked",
			"reason": "mutation_failed",
			"files": files,
			"diagnostic": diagnostic,
		}
	return {"status": "attributed", "files": files}


def attribute_shell(
	payload: dict[str, Any], state_dir: Path, *, deadline: float | None = None
) -> dict[str, Any]:
	"""Pair shell events by session/call identity, independent of command syntax."""
	key = shell_event_key(payload)
	root, group, state_path = shell_state_location(payload, state_dir, key)
	event = payload.get("hook_event_name")
	if event == "PreToolUse":
		unconfirmed = capture_shell_snapshot(
			root,
			state_dir,
			group,
			state_path,
			session_id=payload["session_id"],
			turn_id=payload.get("turn_id"),
			tool_use_id=payload.get("tool_use_id"),
			deadline=deadline,
		)
		return {
			"status": "pending",
			"files": [],
			"warnings": (
				[{"check": "attribution", "status": "unchecked", "reason": "abandoned_shell_call"}]
				if unconfirmed
				else []
			),
		}
	if event != "PostToolUse":
		return {"status": "not_applicable", "reason": "unsupported_event", "files": []}
	before, after, concurrent = read_shell_snapshots(
		root,
		state_dir,
		group,
		state_path,
		deadline=deadline,
		expected_owner={
			name: payload[name]
			for name in ("session_id", "turn_id", "tool_use_id")
			if name in payload
		},
	)
	files = changed_shell_files(root, before, after)
	return {
		**classify_shell_result(shell_exit_code(payload, deadline=deadline), files),
		"write_allowed": not concurrent,
	}


def attribute(
	payload: object, *, state_dir: Path | None = None, deadline: float | None = None
) -> dict[str, Any]:
	"""Return a complete handoff or an explicit incomplete/inapplicable status."""
	result: dict[str, Any] = {
		"schema": SCHEMA,
		"status": "unchecked",
		"files": [],
		**scope(payload),
	}
	if not isinstance(payload, dict) or not isinstance(payload.get("cwd"), str):
		return {**result, "reason": "invalid_event"}
	result["event"] = {
		"turn_id": payload.get("turn_id"),
		"session_id": payload.get("session_id"),
		"tool_use_id": payload.get("tool_use_id"),
		"tool_name": payload.get("tool_name"),
		"hook_event_name": payload.get("hook_event_name"),
	}
	if payload.get("tool_name") == "Bash":
		try:
			if state_dir is None:
				raise AttributionError("snapshot_not_configured")
			result["root"] = str(project_root(Path(payload["cwd"])))
			return {**result, **attribute_shell(payload, state_dir, deadline=deadline)}
		except AttributionError as error:
			return {
				**result,
				**failure(
					error,
					"snapshot_pre"
					if payload.get("hook_event_name") == "PreToolUse"
					else "snapshot_post",
				),
			}
		except (OSError, ValueError, KeyError, TypeError) as error:
			return {
				**result,
				**failure(
					error,
					"snapshot_pre"
					if payload.get("hook_event_name") == "PreToolUse"
					else "snapshot_post",
					"unreadable_event_or_file",
				),
			}
	if payload.get("tool_name") not in {"apply_patch", "Edit", "Write"}:
		return {**result, "status": "not_applicable", "reason": "unsupported_tool"}
	if payload.get("hook_event_name") != "PostToolUse":
		return {**result, "status": "not_applicable", "reason": "pre_edit"}
	try:
		files = attribute_patch(payload)
	except AttributionError as error:
		return {**result, **failure(error, "patch_inputs")}
	except (OSError, ValueError, KeyError, TypeError) as error:
		return {**result, **failure(error, "patch_inputs", "unreadable_event_or_file")}
	return {**result, "status": "attributed", "files": files}


def read_event() -> object:
	"""Bound ingress before JSON parsing; errors contain no source fragments."""
	content = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
	if len(content) > MAX_EVENT_BYTES:
		raise AttributionError(
			"event_size_limit",
			limit_dimension="event_bytes",
			observed=len(content),
			allowed=MAX_EVENT_BYTES,
		)
	try:
		return json.loads(content)
	except (json.JSONDecodeError, UnicodeError) as error:
		raise AttributionError("invalid_json") from error


def main() -> None:
	parser = argparse.ArgumentParser()
	parser.add_argument("--state-dir", type=Path)
	arguments = parser.parse_args()
	try:
		result = attribute(read_event(), state_dir=arguments.state_dir)
	except AttributionError as error:
		result = {"schema": SCHEMA, "status": "unchecked", "reason": str(error), "files": []}
	sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")


if __name__ == "__main__":
	main()
