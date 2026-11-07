"""Reclaim only authenticated inactive workspaces."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import BinaryIO, cast

from .path_safety import is_link_or_reparse
from .workspace_constants import (
	_ACTIVE_LOCK_NAME,
	_CLEANUP_RECORD_PREFIX,
	_CLEANUP_RECORD_SCHEMA,
	_CLEANUP_RETRY_PREFIX,
	_OWNERSHIP_MARKER_NAME,
	_OWNERSHIP_RECORD_MAX_BYTES,
	_OWNERSHIP_RECORD_PREFIX,
	_OWNERSHIP_RECORD_SCHEMA,
)
from .workspace_keys import _plain_lock_file, _same_file_identity, _valid_signed_record
from .workspace_locks import _open_locked_file, _unlock
from .workspace_quarantine import _remove_file
from .workspace_records import (
	_cleanup_record_path,
	_cleanup_retry_path,
	_has_valid_cleanup_record,
	_has_valid_ownership_marker,
	_has_valid_ownership_record,
	_ownership_record_path,
	_remove_cleanup_record,
	_remove_ownership_record,
	_write_cleanup_record,
)
from .workspace_tree import _remove_entry


def _clean_stale_entries(temp_root: Path) -> None:
	seen: set[str] = set()
	for owner_record in tuple(temp_root.iterdir()):
		entry = _run_directory_from_owner_record(temp_root, owner_record)
		if entry is not None and entry.name not in seen:
			seen.add(entry.name)
			_reclaim_stale_workspace(temp_root, entry)


def _run_directory_from_owner_record(temp_root: Path, owner_record: Path) -> Path | None:
	prefix = next(
		(
			candidate
			for candidate in (
				_OWNERSHIP_RECORD_PREFIX,
				_CLEANUP_RECORD_PREFIX,
				_CLEANUP_RETRY_PREFIX,
			)
			if owner_record.name.startswith(candidate)
		),
		None,
	)
	if prefix is None:
		return None
	run_name = owner_record.name.removeprefix(prefix)
	if not run_name.startswith("qg-") or Path(run_name).name != run_name:
		return None
	return temp_root / run_name


def _reclaim_stale_workspace(temp_root: Path, entry: Path) -> None:
	try:
		metadata = entry.lstat()
	except FileNotFoundError:
		_remove_orphan_ownership_record(temp_root, entry)
		return
	if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
		return
	owner_valid, owner_identity = _has_valid_ownership_record(temp_root, entry, metadata)
	if not owner_valid or owner_identity is None:
		return
	cleanup_valid, cleanup_identity, active_identity, marker_identity = _has_valid_cleanup_record(
		temp_root, entry, metadata
	)
	if not cleanup_valid:
		cleanup_identity = None
	cleanup_controls = (
		(active_identity, marker_identity)
		if active_identity is not None and marker_identity is not None
		else None
	)
	can_reclaim, cleanup_identity, cleanup_controls = _can_reclaim_workspace(
		temp_root,
		entry,
		metadata,
		owner_identity,
		cleanup_identity=cleanup_identity,
		cleanup_controls=cleanup_controls,
	)
	if not can_reclaim or cleanup_identity is None or cleanup_controls is None:
		return
	try:
		_remove_entry(entry, expected_identity=metadata)
	except OSError:
		return
	_remove_cleanup_record(temp_root, entry, metadata, expected_record=cleanup_identity)
	_remove_ownership_record(temp_root, entry, metadata, expected_record=owner_identity)


def _remove_orphan_ownership_record(temp_root: Path, run_directory: Path) -> None:
	for path, expected_format, expected_schema in (
		(
			_ownership_record_path(temp_root, run_directory),
			"quality-gate-temp-workspace-owner",
			_OWNERSHIP_RECORD_SCHEMA,
		),
		(
			_cleanup_record_path(temp_root, run_directory),
			"quality-gate-temp-workspace-cleanup",
			_CLEANUP_RECORD_SCHEMA,
		),
		(
			_cleanup_retry_path(temp_root, run_directory),
			"quality-gate-temp-workspace-cleanup",
			_CLEANUP_RECORD_SCHEMA,
		),
	):
		try:
			metadata = path.lstat()
			if (
				not _plain_lock_file(path, metadata)
				or metadata.st_size > _OWNERSHIP_RECORD_MAX_BYTES
			):
				continue
			with path.open("rb") as stream:
				opened = os.fstat(stream.fileno())
				if not _same_file_identity(metadata, opened) or not _plain_lock_file(path, opened):
					continue
				content = stream.read(_OWNERSHIP_RECORD_MAX_BYTES + 1)
			current = path.lstat()
		except OSError:
			continue
		if (
			len(content) > _OWNERSHIP_RECORD_MAX_BYTES
			or not _same_file_identity(metadata, current)
			or not _plain_lock_file(path, current)
		):
			continue
		payload = _valid_signed_record(content)
		valid_fields = {"device", "format", "inode", "root", "run", "schema"}
		if expected_format == "quality-gate-temp-workspace-cleanup":
			valid_fields |= {
				"active_device",
				"active_inode",
				"marker_device",
				"marker_inode",
			}
		if (
			payload is None
			or set(payload) != valid_fields
			or type(payload["device"]) is not int
			or payload["device"] < 0
			or type(payload["inode"]) is not int
			or payload["inode"] < 0
			or type(payload["schema"]) is not int
			or payload["schema"] != expected_schema
			or payload["format"] != expected_format
			or payload["root"] != os.path.normcase(os.path.abspath(temp_root))
			or payload["run"] != run_directory.name
			or (
				expected_format == "quality-gate-temp-workspace-cleanup"
				and any(
					type(payload[field]) is not int or cast(int, payload[field]) < 0
					for field in (
						"active_device",
						"active_inode",
						"marker_device",
						"marker_inode",
					)
				)
			)
		):
			continue
		try:
			_remove_file(path, metadata)
		except OSError:
			continue


def _can_reclaim_workspace(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	*,
	cleanup_identity: os.stat_result | None,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None,
) -> tuple[
	bool,
	os.stat_result | None,
	tuple[tuple[int, int], tuple[int, int]] | None,
]:
	try:
		active_lock = _open_locked_file(entry / _ACTIVE_LOCK_NAME, blocking=False, create=False)
	except OSError:
		return False, cleanup_identity, cleanup_controls
	if active_lock is None:
		return _can_reclaim_without_active_lock(
			temp_root,
			entry,
			metadata,
			owner_identity,
			cleanup_identity,
			cleanup_controls,
		)
	try:
		if cleanup_identity is None:
			return _begin_workspace_cleanup(temp_root, entry, metadata, owner_identity, active_lock)
		return _resume_workspace_cleanup(
			temp_root,
			entry,
			metadata,
			owner_identity,
			cleanup_identity,
			cleanup_controls,
		)
	finally:
		_unlock(active_lock)


def _can_reclaim_without_active_lock(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	cleanup_identity: os.stat_result | None,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None,
) -> tuple[bool, os.stat_result | None, tuple[tuple[int, int], tuple[int, int]] | None]:
	if cleanup_identity is None or cleanup_controls is None:
		return False, cleanup_identity, cleanup_controls
	try:
		(entry / _ACTIVE_LOCK_NAME).lstat()
	except FileNotFoundError:
		return (
			_workspace_ownership_is_current(
				temp_root,
				entry,
				metadata,
				owner_identity,
				cleanup_identity=cleanup_identity,
				cleanup_controls=cleanup_controls,
				allow_missing_marker=True,
			),
			cleanup_identity,
			cleanup_controls,
		)
	return False, cleanup_identity, cleanup_controls


def _begin_workspace_cleanup(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	active_lock: BinaryIO,
) -> tuple[bool, os.stat_result | None, tuple[tuple[int, int], tuple[int, int]] | None]:
	active_file_identity = os.fstat(active_lock.fileno())
	try:
		marker_file_identity = (entry / _OWNERSHIP_MARKER_NAME).lstat()
	except FileNotFoundError:
		return False, None, None
	if not _workspace_ownership_is_current(temp_root, entry, metadata, owner_identity):
		return False, None, None
	try:
		active_current = (entry / _ACTIVE_LOCK_NAME).lstat()
		marker_current = (entry / _OWNERSHIP_MARKER_NAME).lstat()
	except FileNotFoundError:
		return False, None, None
	if not _same_file_identity(active_file_identity, active_current) or not _same_file_identity(
		marker_file_identity, marker_current
	):
		return False, None, None
	cleanup_identity = _write_cleanup_record(
		temp_root, entry, metadata, active_file_identity, marker_file_identity
	)
	if cleanup_identity is None:
		return False, None, None
	cleanup_controls = (
		(active_file_identity.st_dev, active_file_identity.st_ino),
		(marker_file_identity.st_dev, marker_file_identity.st_ino),
	)
	return (
		_workspace_ownership_is_current(
			temp_root,
			entry,
			metadata,
			owner_identity,
			cleanup_identity=cleanup_identity,
			cleanup_controls=cleanup_controls,
		),
		cleanup_identity,
		cleanup_controls,
	)


def _resume_workspace_cleanup(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	cleanup_identity: os.stat_result,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None,
) -> tuple[bool, os.stat_result | None, tuple[tuple[int, int], tuple[int, int]] | None]:
	if cleanup_controls is None:
		return False, cleanup_identity, cleanup_controls
	return (
		_workspace_ownership_is_current(
			temp_root,
			entry,
			metadata,
			owner_identity,
			cleanup_identity=cleanup_identity,
			cleanup_controls=cleanup_controls,
			allow_missing_marker=True,
		),
		cleanup_identity,
		cleanup_controls,
	)


def _workspace_ownership_is_current(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
	*,
	cleanup_identity: os.stat_result | None = None,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None = None,
	allow_missing_marker: bool = False,
) -> bool:
	if not _workspace_owner_is_current(temp_root, entry, metadata, owner_identity):
		return False
	if cleanup_identity is None:
		return _has_valid_ownership_marker(temp_root, entry, expected_directory=metadata)
	return _cleanup_controls_are_current(
		temp_root,
		entry,
		metadata,
		cleanup_identity,
		cleanup_controls,
		allow_missing_marker=allow_missing_marker,
	)


def _workspace_owner_is_current(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	owner_identity: os.stat_result,
) -> bool:
	try:
		current = entry.lstat()
	except OSError:
		return False
	if (
		is_link_or_reparse(current)
		or not stat.S_ISDIR(current.st_mode)
		or not _same_file_identity(metadata, current)
		or not _has_valid_ownership_record(
			temp_root, entry, metadata, expected_record=owner_identity
		)[0]
	):
		return False
	return True


def _cleanup_controls_are_current(
	temp_root: Path,
	entry: Path,
	metadata: os.stat_result,
	cleanup_identity: os.stat_result,
	cleanup_controls: tuple[tuple[int, int], tuple[int, int]] | None,
	*,
	allow_missing_marker: bool,
) -> bool:
	cleanup_controls_from_record: tuple[tuple[int, int], tuple[int, int]] | None = None
	(
		cleanup_valid,
		_cleanup_record_identity,
		active_identity,
		marker_identity,
	) = _has_valid_cleanup_record(temp_root, entry, metadata, expected_record=cleanup_identity)
	if not cleanup_valid or active_identity is None or marker_identity is None:
		return False
	cleanup_controls_from_record = (active_identity, marker_identity)
	if cleanup_controls != cleanup_controls_from_record:
		return False
	active_path = entry / _ACTIVE_LOCK_NAME
	try:
		active_metadata = active_path.lstat()
	except FileNotFoundError:
		if not allow_missing_marker:
			return False
	else:
		if (active_metadata.st_dev, active_metadata.st_ino) != active_identity:
			return False
	marker_path = entry / _OWNERSHIP_MARKER_NAME
	try:
		marker_metadata = marker_path.lstat()
	except FileNotFoundError:
		return allow_missing_marker
	if (marker_metadata.st_dev, marker_metadata.st_ino) != cleanup_controls_from_record[1]:
		return False
	return _has_valid_ownership_marker(temp_root, entry, expected_directory=metadata)
