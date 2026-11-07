"""Read and maintain authenticated workspace ownership records."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from .path_safety import is_link_or_reparse
from .workspace_constants import (
	_CLEANUP_RECORD_PREFIX,
	_CLEANUP_RECORD_SCHEMA,
	_CLEANUP_RETRY_PREFIX,
	_OWNERSHIP_MARKER_MAX_BYTES,
	_OWNERSHIP_MARKER_NAME,
	_OWNERSHIP_RECORD_MAX_BYTES,
	_OWNERSHIP_RECORD_PREFIX,
	_OWNERSHIP_RECORD_SCHEMA,
)
from .workspace_keys import (
	_canonical_json,
	_plain_lock_file,
	_publish_new_file,
	_same_file_identity,
	_signed_record,
	_valid_signed_record,
)
from .workspace_quarantine import _remove_file


def _ownership_marker(temp_root: Path, run_directory: Path) -> bytes:
	return (
		json.dumps(
			{
				"format": "quality-gate-temp-workspace",
				"root": os.path.normcase(os.path.abspath(temp_root)),
				"run": run_directory.name,
				"schema": 1,
			},
			sort_keys=True,
			separators=(",", ":"),
		)
		+ "\n"
	).encode("utf-8")


def _ownership_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	create_key: bool = True,
) -> bytes:
	payload: dict[str, object] = {
		"device": directory_identity.st_dev,
		"format": "quality-gate-temp-workspace-owner",
		"inode": directory_identity.st_ino,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"run": run_directory.name,
		"schema": _OWNERSHIP_RECORD_SCHEMA,
	}
	record = _signed_record(payload, create_key=create_key)
	if record is None:
		raise OSError(f"temporary workspace ownership key is unavailable: {run_directory}")
	return record


def _cleanup_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	active_lock_identity: os.stat_result,
	marker_identity: os.stat_result,
	*,
	create_key: bool = True,
) -> bytes:
	payload: dict[str, object] = {
		"active_device": active_lock_identity.st_dev,
		"active_inode": active_lock_identity.st_ino,
		"device": directory_identity.st_dev,
		"format": "quality-gate-temp-workspace-cleanup",
		"inode": directory_identity.st_ino,
		"marker_device": marker_identity.st_dev,
		"marker_inode": marker_identity.st_ino,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"run": run_directory.name,
		"schema": _CLEANUP_RECORD_SCHEMA,
	}
	record = _signed_record(payload, create_key=create_key)
	if record is None:
		raise OSError(f"temporary workspace cleanup key is unavailable: {run_directory}")
	return record


def _ownership_record_path(temp_root: Path, run_directory: Path) -> Path:
	return temp_root / f"{_OWNERSHIP_RECORD_PREFIX}{run_directory.name}"


def _cleanup_record_path(temp_root: Path, run_directory: Path) -> Path:
	return temp_root / f"{_CLEANUP_RECORD_PREFIX}{run_directory.name}"


def _cleanup_retry_path(temp_root: Path, run_directory: Path) -> Path:
	return temp_root / f"{_CLEANUP_RETRY_PREFIX}{run_directory.name}"


def _has_valid_ownership_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None = None,
	verify_directory: bool = True,
) -> tuple[bool, os.stat_result | None]:
	path = _ownership_record_path(temp_root, run_directory)
	try:
		metadata = path.lstat()
		if (
			not _plain_lock_file(path, metadata)
			or metadata.st_size > _OWNERSHIP_RECORD_MAX_BYTES
			or (expected_record is not None and not _same_file_identity(expected_record, metadata))
		):
			return False, metadata
		with path.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened) or not _plain_lock_file(path, opened):
				return False, metadata
			content = stream.read(_OWNERSHIP_RECORD_MAX_BYTES + 1)
		current = path.lstat()
	except OSError:
		return False, None
	if not _same_file_identity(metadata, current):
		return False, metadata
	if verify_directory:
		try:
			current_directory = run_directory.lstat()
		except OSError:
			return False, metadata
		if not _same_file_identity(directory_identity, current_directory):
			return False, metadata
	payload = _valid_signed_record(content)
	expected: dict[str, object] = {
		"device": directory_identity.st_dev,
		"format": "quality-gate-temp-workspace-owner",
		"inode": directory_identity.st_ino,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"run": run_directory.name,
		"schema": _OWNERSHIP_RECORD_SCHEMA,
	}
	return payload is not None and _canonical_json(payload) == _canonical_json(expected), metadata


def _has_valid_cleanup_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None = None,
	verify_directory: bool = True,
) -> tuple[
	bool,
	os.stat_result | None,
	tuple[int, int] | None,
	tuple[int, int] | None,
]:
	last_identity: os.stat_result | None = None
	for path in (
		_cleanup_record_path(temp_root, run_directory),
		_cleanup_retry_path(temp_root, run_directory),
	):
		result = _read_cleanup_record(
			path,
			temp_root,
			run_directory,
			directory_identity,
			expected_record=expected_record,
			verify_directory=verify_directory,
		)
		if result[0]:
			return result
		if result[1] is not None:
			last_identity = result[1]
	return False, last_identity, None, None


def _read_cleanup_record(
	path: Path,
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None,
	verify_directory: bool,
) -> tuple[bool, os.stat_result | None, tuple[int, int] | None, tuple[int, int] | None]:
	try:
		metadata = path.lstat()
		if (
			not _plain_lock_file(path, metadata)
			or metadata.st_size > _OWNERSHIP_RECORD_MAX_BYTES
			or (expected_record is not None and not _same_file_identity(expected_record, metadata))
		):
			return False, metadata, None, None
		with path.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened) or not _plain_lock_file(path, opened):
				return False, metadata, None, None
			content = stream.read(_OWNERSHIP_RECORD_MAX_BYTES + 1)
		current = path.lstat()
	except OSError:
		return False, None, None, None
	if not _same_file_identity(metadata, current):
		return False, metadata, None, None
	if verify_directory:
		try:
			current_directory = run_directory.lstat()
		except OSError:
			return False, metadata, None, None
		if not _same_file_identity(directory_identity, current_directory):
			return False, metadata, None, None
	payload = _valid_signed_record(content)
	if (
		payload is None
		or set(payload)
		!= {
			"active_device",
			"active_inode",
			"device",
			"format",
			"inode",
			"marker_device",
			"marker_inode",
			"root",
			"run",
			"schema",
		}
		or type(payload["active_device"]) is not int
		or payload["active_device"] < 0
		or type(payload["active_inode"]) is not int
		or payload["active_inode"] < 0
		or type(payload["device"]) is not int
		or payload["device"] != directory_identity.st_dev
		or type(payload["inode"]) is not int
		or payload["inode"] != directory_identity.st_ino
		or type(payload["marker_device"]) is not int
		or payload["marker_device"] < 0
		or type(payload["marker_inode"]) is not int
		or payload["marker_inode"] < 0
		or type(payload["schema"]) is not int
		or payload["schema"] != _CLEANUP_RECORD_SCHEMA
		or payload["format"] != "quality-gate-temp-workspace-cleanup"
		or payload["root"] != os.path.normcase(os.path.abspath(temp_root))
		or payload["run"] != run_directory.name
	):
		return False, metadata, None, None
	return (
		True,
		metadata,
		(payload["active_device"], payload["active_inode"]),
		(payload["marker_device"], payload["marker_inode"]),
	)


def _write_ownership_record(
	temp_root: Path, run_directory: Path, directory_identity: os.stat_result
) -> os.stat_result:
	path = _ownership_record_path(temp_root, run_directory)
	created_identity: os.stat_result | None = None
	try:
		with path.open("xb") as stream:
			created_identity = os.fstat(stream.fileno())
			stream.write(_ownership_record(temp_root, run_directory, directory_identity))
			stream.flush()
			os.fsync(stream.fileno())
		valid, identity = _has_valid_ownership_record(temp_root, run_directory, directory_identity)
		if (
			not valid
			or identity is None
			or created_identity is None
			or not _same_file_identity(created_identity, identity)
		):
			raise OSError(f"temporary workspace ownership could not be recorded: {run_directory}")
		return identity
	except OSError:
		if created_identity is not None:
			try:
				_remove_file(path, created_identity)
			except OSError:
				pass
		raise


def _write_cleanup_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	active_lock_identity: os.stat_result,
	marker_identity: os.stat_result,
) -> os.stat_result | None:
	path = _cleanup_record_path(temp_root, run_directory)
	try:
		path.lstat()
	except FileNotFoundError:
		pass
	except OSError:
		return None
	else:
		valid, identity, active_identity, marker_file = _has_valid_cleanup_record(
			temp_root, run_directory, directory_identity
		)
		if valid:
			return (
				identity
				if active_identity == (active_lock_identity.st_dev, active_lock_identity.st_ino)
				and marker_file == (marker_identity.st_dev, marker_identity.st_ino)
				else None
			)
		path = _cleanup_retry_path(temp_root, run_directory)
	try:
		_publish_new_file(
			path,
			_cleanup_record(
				temp_root, run_directory, directory_identity, active_lock_identity, marker_identity
			),
		)
		valid, identity, active_identity, marker_file = _has_valid_cleanup_record(
			temp_root, run_directory, directory_identity
		)
		if (
			not valid
			or identity is None
			or (active_identity != (active_lock_identity.st_dev, active_lock_identity.st_ino))
			or (marker_file != (marker_identity.st_dev, marker_identity.st_ino))
		):
			raise OSError(f"temporary workspace cleanup could not be recorded: {run_directory}")
		return identity
	except FileExistsError:
		valid, identity, active_identity, marker_file = _has_valid_cleanup_record(
			temp_root, run_directory, directory_identity
		)
		return (
			identity
			if valid
			and active_identity == (active_lock_identity.st_dev, active_lock_identity.st_ino)
			and marker_file == (marker_identity.st_dev, marker_identity.st_ino)
			else None
		)
	except OSError:
		return None


def _remove_ownership_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None = None,
) -> None:
	valid, identity = _has_valid_ownership_record(
		temp_root,
		run_directory,
		directory_identity,
		expected_record=expected_record,
		verify_directory=False,
	)
	if valid and identity is not None:
		_remove_file(_ownership_record_path(temp_root, run_directory), identity)


def _remove_cleanup_record(
	temp_root: Path,
	run_directory: Path,
	directory_identity: os.stat_result,
	*,
	expected_record: os.stat_result | None = None,
) -> None:
	valid, identity, _active_identity, _marker_identity = _has_valid_cleanup_record(
		temp_root,
		run_directory,
		directory_identity,
		expected_record=expected_record,
		verify_directory=False,
	)
	if valid and identity is not None:
		for path in (
			_cleanup_record_path(temp_root, run_directory),
			_cleanup_retry_path(temp_root, run_directory),
		):
			try:
				current = path.lstat()
			except FileNotFoundError:
				continue
			if _same_file_identity(identity, current):
				_remove_file(path, identity)
				break


def _has_valid_ownership_marker(
	temp_root: Path,
	run_directory: Path,
	*,
	expected_directory: os.stat_result | None = None,
) -> bool:
	if expected_directory is not None:
		try:
			current_directory = run_directory.lstat()
		except OSError:
			return False
		if (
			is_link_or_reparse(current_directory)
			or not stat.S_ISDIR(current_directory.st_mode)
			or not _same_file_identity(expected_directory, current_directory)
		):
			return False
	marker = run_directory / _OWNERSHIP_MARKER_NAME
	try:
		metadata = marker.lstat()
		if not _plain_lock_file(marker, metadata) or metadata.st_size > _OWNERSHIP_MARKER_MAX_BYTES:
			return False
		with marker.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened) or not _plain_lock_file(marker, opened):
				return False
			content = stream.read(_OWNERSHIP_MARKER_MAX_BYTES + 1)
	except OSError:
		return False
	if expected_directory is not None:
		try:
			current_directory = run_directory.lstat()
		except OSError:
			return False
		if (
			is_link_or_reparse(current_directory)
			or not stat.S_ISDIR(current_directory.st_mode)
			or not _same_file_identity(expected_directory, current_directory)
		):
			return False
	return content == _ownership_marker(temp_root, run_directory)
