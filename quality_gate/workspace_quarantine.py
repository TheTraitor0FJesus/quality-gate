"""Authenticated quarantine and identity-bound workspace entry removal."""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from . import workspace_keys
from .path_safety import is_link_or_reparse
from .workspace_constants import (
	_CLEANUP_AREA_MARKER_NAME,
	_CLEANUP_AREA_NAME,
	_CLEANUP_RECORD_PREFIX,
	_CLEANUP_RETRY_PREFIX,
	_OWNERSHIP_RECORD_MAX_BYTES,
	_OWNERSHIP_RECORD_PREFIX,
	_QUARANTINE_JOURNAL_MAX_BYTES,
	_QUARANTINE_JOURNAL_SCHEMA,
	TEMP_DIRECTORY_NAME,
)
from .workspace_keys import (
	_RECOVERY_KEY_NAME,
	_current_user_id,
	_ensure_posix_private_directory,
	_optional_os_flag,
	_ownership_key,
	_plain_lock_file,
	_publish_new_file,
	_read_ownership_key,
	_same_file_identity,
	_signed_record,
	_valid_signed_record,
)
from .workspace_windows import _remove_by_windows_handle


def _check_ordinary_directory(path: Path, *, allow_missing: bool = False) -> os.stat_result | None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		if allow_missing:
			return None
		raise
	if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
		raise OSError(errno.ELOOP, "temporary workspace path is not an ordinary directory", path)
	return metadata


def _temporary_root_for_entry(path: Path) -> Path:
	if path.name.startswith(
		(_OWNERSHIP_RECORD_PREFIX, _CLEANUP_RECORD_PREFIX, _CLEANUP_RETRY_PREFIX)
	):
		return path.parent
	workspace_directories = [
		candidate for candidate in (path, *path.parents) if candidate.name.startswith("qg-")
	]
	managed_workspace = next(
		(
			candidate
			for candidate in workspace_directories
			if candidate.parent.name == TEMP_DIRECTORY_NAME
		),
		None,
	)
	if managed_workspace is not None:
		return managed_workspace.parent
	if workspace_directories:
		return workspace_directories[-1].parent
	raise OSError(f"temporary workspace entry has no managed root: {path}")


def _cleanup_area_payload(temp_root: Path, identity: os.stat_result) -> dict[str, object]:
	return {
		"device": identity.st_dev,
		"format": "quality-gate-temp-workspace-cleanup-area",
		"inode": identity.st_ino,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"schema": 1,
	}


def _cleanup_area_marker(temp_root: Path, identity: os.stat_result) -> bytes:
	marker = _signed_record(_cleanup_area_payload(temp_root, identity), create_key=True)
	if marker is None:
		raise OSError(f"temporary workspace signing key is unavailable: {temp_root}")
	return marker


def _open_cleanup_area(temp_root: Path) -> tuple[Path, int]:
	_check_ordinary_directory(temp_root)
	key = _ownership_key(create=True)
	if key is None:
		raise OSError(f"temporary workspace signing key is unavailable: {temp_root}")
	legacy = _open_cleanup_area_at(temp_root, temp_root / _CLEANUP_AREA_NAME, create=False)
	try:
		recovery_key = _read_ownership_key(
			workspace_keys._user_state_directory() / _RECOVERY_KEY_NAME
		)
	except OSError:
		recovery_key = None
	keys = [key]
	if recovery_key is not None and recovery_key not in keys:
		keys.append(recovery_key)
	selected = _open_existing_cleanup_areas(temp_root, keys)
	if selected is not None:
		if legacy is not None:
			os.close(legacy[1])
		return selected
	if legacy is not None:
		return legacy
	for area in _cleanup_area_candidates(temp_root, key):
		opened = _open_cleanup_area_at(temp_root, area, create=True)
		if opened is not None:
			return opened
	raise OSError(f"no safe temporary workspace cleanup area available: {temp_root}")


def _cleanup_area_candidates(temp_root: Path, key: bytes) -> Iterator[Path]:
	digest = hmac.new(key, b"quality-gate-cleanup-area", hashlib.sha256).hexdigest()[:32]
	for index in range(8):
		suffix = "" if index == 0 else f"-{index}"
		yield temp_root / f"{_CLEANUP_AREA_NAME}-{digest}{suffix}"


def _open_existing_cleanup_areas(temp_root: Path, keys: list[bytes]) -> tuple[Path, int] | None:
	selected: tuple[Path, int] | None = None
	for candidate_key in keys:
		for area in _cleanup_area_candidates(temp_root, candidate_key):
			opened = _open_cleanup_area_at(temp_root, area, create=False)
			if opened is None:
				continue
			if candidate_key == keys[0] and selected is None:
				selected = opened
			else:
				os.close(opened[1])
	return selected


def _open_cleanup_area_at(temp_root: Path, area: Path, *, create: bool) -> tuple[Path, int] | None:
	created = False
	if create:
		try:
			area.mkdir(mode=0o700)
			created = True
		except FileExistsError:
			pass
	try:
		identity = (
			_ensure_posix_private_directory(area, restrict_existing=False)
			if create
			else area.lstat()
		)
		if (
			is_link_or_reparse(identity)
			or not stat.S_ISDIR(identity.st_mode)
			or identity.st_uid != _current_user_id()
			or stat.S_IMODE(identity.st_mode) & 0o077
		):
			return None
	except OSError:
		return None
	marker = area / _CLEANUP_AREA_MARKER_NAME
	if created:
		_publish_new_file(marker, _cleanup_area_marker(temp_root, identity))
	if not _cleanup_area_marker_is_current(temp_root, marker, identity):
		return None
	flags = os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW")
	descriptor = os.open(area, flags)
	opened_area = os.fstat(descriptor)
	current_area = area.lstat()
	if not _same_file_identity(identity, opened_area) or not _same_file_identity(
		identity, current_area
	):
		os.close(descriptor)
		raise OSError(f"temporary workspace cleanup area changed: {area}")
	os.fsync(descriptor)
	_recover_quarantined_entries(temp_root, descriptor)
	return area, descriptor


def _cleanup_area_marker_is_current(
	temp_root: Path, marker: Path, identity: os.stat_result
) -> bool:
	try:
		marker_identity = marker.lstat()
	except FileNotFoundError:
		return False
	if (
		not _plain_lock_file(marker, marker_identity)
		or marker_identity.st_size > _OWNERSHIP_RECORD_MAX_BYTES
	):
		return False
	try:
		with marker.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(marker_identity, opened) or not _plain_lock_file(
				marker, opened
			):
				return False
			content = stream.read(_OWNERSHIP_RECORD_MAX_BYTES + 1)
		current_marker = marker.lstat()
	except (FileNotFoundError, PermissionError):
		return False
	return _valid_signed_record(content) == _cleanup_area_payload(
		temp_root, identity
	) and _same_file_identity(marker_identity, current_marker)


def _recover_quarantined_entries(temp_root: Path, cleanup_descriptor: int) -> None:
	for name in os.listdir(cleanup_descriptor):
		if not name.startswith("pending-") or not name.endswith(".json"):
			continue
		_recover_quarantined_entry(temp_root, name, cleanup_descriptor)


def _recover_quarantined_entry(temp_root: Path, journal_name: str, cleanup_descriptor: int) -> None:
	journal_identity, payload = _read_quarantine_journal(cleanup_descriptor, journal_name)
	if payload is None or payload["root"] != os.path.normcase(os.path.abspath(temp_root)):
		return
	quarantine_name = payload["quarantine"]
	if not isinstance(quarantine_name, str):
		return
	try:
		metadata = os.stat(quarantine_name, dir_fd=cleanup_descriptor, follow_symlinks=False)
	except FileNotFoundError:
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)
		return
	expected_identity = (cast(int, payload["device"]), cast(int, payload["inode"]))
	if (metadata.st_dev, metadata.st_ino) != expected_identity:
		_remove_restored_quarantine_journal(
			temp_root, payload, journal_name, journal_identity, quarantine_name, cleanup_descriptor
		)
		return
	if not _quarantined_kind_matches(metadata, str(payload["kind"])):
		return
	try:
		_delete_quarantined_entry(quarantine_name, str(payload["kind"]), cleanup_descriptor)
	except OSError:
		_remove_restored_quarantine_journal(
			temp_root, payload, journal_name, journal_identity, quarantine_name, cleanup_descriptor
		)
		return
	os.fsync(cleanup_descriptor)
	_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)


def _delete_quarantined_entry(name: str, kind: str, cleanup_descriptor: int) -> None:
	if kind == "directory":
		os.rmdir(name, dir_fd=cleanup_descriptor)
	else:
		os.unlink(name, dir_fd=cleanup_descriptor)


def _remove_restored_quarantine_journal(
	temp_root: Path,
	payload: dict[str, object],
	journal_name: str,
	journal_identity: os.stat_result | None,
	quarantine_name: str,
	cleanup_descriptor: int,
) -> None:
	if _restore_pending_quarantined_entry(temp_root, payload, quarantine_name, cleanup_descriptor):
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)


def _restore_pending_quarantined_entry(
	temp_root: Path,
	payload: dict[str, object],
	quarantine_name: str,
	cleanup_descriptor: int,
) -> bool:
	try:
		parent_relative = Path(str(payload["parent"]))
		parent_path = temp_root / parent_relative
		parent_identity = (
			cast(int, payload["parent_device"]),
			cast(int, payload["parent_inode"]),
		)
		parent = os.open(
			parent_path,
			os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW"),
		)
	except (OSError, TypeError, ValueError):
		return False
	try:
		opened_parent = os.fstat(parent)
		if (opened_parent.st_dev, opened_parent.st_ino) != parent_identity:
			return False
		_restore_quarantined_entry(
			Path(str(payload["name"])),
			quarantine_name,
			parent,
			cleanup_descriptor,
		)
		os.fsync(parent)
		os.fsync(cleanup_descriptor)
		return True
	except OSError:
		return False
	finally:
		os.close(parent)


def _remove_quarantine_journal(
	name: str, expected_identity: os.stat_result | None, cleanup_descriptor: int
) -> None:
	if expected_identity is None:
		return
	try:
		current = os.stat(name, dir_fd=cleanup_descriptor, follow_symlinks=False)
	except FileNotFoundError:
		return
	if _same_file_identity(expected_identity, current):
		os.unlink(name, dir_fd=cleanup_descriptor)
		os.fsync(cleanup_descriptor)


def _write_quarantine_journal(
	temp_root: Path,
	path: Path,
	expected_identity: os.stat_result,
	parent_identity: os.stat_result,
	kind: str,
	token: str,
	quarantine_name: str,
	cleanup_descriptor: int,
) -> tuple[str, os.stat_result]:
	journal_name = f"pending-{token}.json"
	parent_relative = path.parent.relative_to(temp_root).as_posix()
	payload: dict[str, object] = {
		"device": expected_identity.st_dev,
		"format": "quality-gate-temp-workspace-quarantine",
		"inode": expected_identity.st_ino,
		"kind": kind,
		"name": path.name,
		"parent": parent_relative,
		"parent_device": parent_identity.st_dev,
		"parent_inode": parent_identity.st_ino,
		"quarantine": quarantine_name,
		"root": os.path.normcase(os.path.abspath(temp_root)),
		"schema": 1,
		"token": token,
	}
	content = _signed_record(payload, create_key=False)
	if content is None:
		raise OSError(f"temporary workspace quarantine key is unavailable: {path}")
	flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _optional_os_flag("O_NOFOLLOW")
	descriptor = os.open(journal_name, flags, 0o600, dir_fd=cleanup_descriptor)
	created_identity = os.fstat(descriptor)
	try:
		with os.fdopen(descriptor, "wb") as stream:
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		os.fsync(cleanup_descriptor)
		return journal_name, created_identity
	except OSError:
		_remove_quarantine_journal(journal_name, created_identity, cleanup_descriptor)
		raise


def _quarantined_kind_matches(metadata: os.stat_result, kind: str) -> bool:
	if kind == "link":
		return is_link_or_reparse(metadata)
	if kind == "directory":
		return stat.S_ISDIR(metadata.st_mode) and not is_link_or_reparse(metadata)
	return (
		kind == "file" and not stat.S_ISDIR(metadata.st_mode) and not is_link_or_reparse(metadata)
	)


def _read_quarantine_journal(
	cleanup_descriptor: int, name: str
) -> tuple[os.stat_result | None, dict[str, object] | None]:
	try:
		metadata = os.stat(name, dir_fd=cleanup_descriptor, follow_symlinks=False)
		if (
			not stat.S_ISREG(metadata.st_mode)
			or metadata.st_nlink != 1
			or metadata.st_size > _QUARANTINE_JOURNAL_MAX_BYTES
		):
			return metadata, None
		descriptor = os.open(
			name,
			os.O_RDONLY | _optional_os_flag("O_NOFOLLOW"),
			dir_fd=cleanup_descriptor,
		)
		with os.fdopen(descriptor, "rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened):
				return metadata, None
			content = stream.read(_QUARANTINE_JOURNAL_MAX_BYTES + 1)
			current = os.stat(name, dir_fd=cleanup_descriptor, follow_symlinks=False)
	except OSError:
		return None, None
	if len(content) > _QUARANTINE_JOURNAL_MAX_BYTES or not _same_file_identity(metadata, current):
		return metadata, None
	payload = _valid_signed_record(content)
	if payload is None or set(payload) != {
		"device",
		"format",
		"inode",
		"kind",
		"name",
		"parent",
		"parent_device",
		"parent_inode",
		"quarantine",
		"root",
		"schema",
		"token",
	}:
		return metadata, None
	token = name.removeprefix("pending-").removesuffix(".json")
	if (
		payload["format"] != "quality-gate-temp-workspace-quarantine"
		or payload["schema"] != _QUARANTINE_JOURNAL_SCHEMA
		or payload["token"] != token
		or type(payload["device"]) is not int
		or cast(int, payload["device"]) < 0
		or type(payload["inode"]) is not int
		or cast(int, payload["inode"]) < 0
		or type(payload["parent_device"]) is not int
		or cast(int, payload["parent_device"]) < 0
		or type(payload["parent_inode"]) is not int
		or cast(int, payload["parent_inode"]) < 0
		or type(payload["schema"]) is not int
		or cast(int, payload["schema"]) != _QUARANTINE_JOURNAL_SCHEMA
		or not isinstance(payload["kind"], str)
		or payload["kind"] not in {"file", "link", "directory"}
		or not isinstance(payload["name"], str)
		or not payload["name"]
		or Path(payload["name"]).name != payload["name"]
		or not isinstance(payload["parent"], str)
		or Path(payload["parent"]).is_absolute()
		or ".." in Path(payload["parent"]).parts
		or not isinstance(payload["root"], str)
		or not isinstance(payload["quarantine"], str)
	):
		return metadata, None
	expected_quarantine_name = (
		f"entry-{cast(int, payload['device']):x}-{cast(int, payload['inode']):x}-{token}"
	)
	if payload["quarantine"] != expected_quarantine_name:
		return metadata, None
	return metadata, payload


def _rename_entry_to_cleanup_area(
	path: Path,
	quarantine_name: str,
	parent_descriptor: int,
	cleanup_descriptor: int,
) -> None:
	try:
		os.rename(
			path.name,
			quarantine_name,
			src_dir_fd=parent_descriptor,
			dst_dir_fd=cleanup_descriptor,
		)
	except PermissionError:
		parent_mode = os.fstat(parent_descriptor).st_mode
		fchmod_name = "fchmod"
		fchmod = cast(Any, getattr(os, fchmod_name))
		fchmod(parent_descriptor, stat.S_IMODE(parent_mode) | stat.S_IWUSR | stat.S_IXUSR)
		os.rename(
			path.name,
			quarantine_name,
			src_dir_fd=parent_descriptor,
			dst_dir_fd=cleanup_descriptor,
		)


def _restore_quarantined_entry(
	path: Path,
	quarantine_name: str,
	parent_descriptor: int,
	cleanup_descriptor: int,
) -> None:
	workspace_keys._rename_without_replacement(
		quarantine_name, path.name, cleanup_descriptor, parent_descriptor
	)


def _remove_by_quarantine(
	path: Path,
	expected_identity: os.stat_result,
	*,
	kind: str,
) -> None:
	if os.name == "nt":
		_remove_by_windows_handle(path, expected_identity, kind=kind)
		return
	_remove_by_posix_quarantine(path, expected_identity, kind=kind)


def _remove_by_posix_quarantine(
	path: Path,
	expected_identity: os.stat_result,
	*,
	kind: str,
) -> None:
	root = _temporary_root_for_entry(path)
	parent_identity = path.parent.lstat()
	if is_link_or_reparse(parent_identity) or not stat.S_ISDIR(parent_identity.st_mode):
		raise OSError(f"temporary workspace parent changed before removal: {path.parent}")
	flags = os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW")
	parent_descriptor = os.open(path.parent, flags)
	try:
		if not _same_file_identity(parent_identity, os.fstat(parent_descriptor)):
			raise OSError(f"temporary workspace parent changed before removal: {path.parent}")
		current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
		if not _same_file_identity(expected_identity, current):
			raise OSError(f"temporary workspace identity changed before removal: {path}")
		_area, cleanup_descriptor = _open_cleanup_area(root)
		try:
			_remove_quarantined_entry(
				root,
				path,
				expected_identity,
				parent_identity,
				kind,
				parent_descriptor,
				cleanup_descriptor,
			)
		finally:
			os.close(cleanup_descriptor)
	finally:
		os.close(parent_descriptor)


def _remove_quarantined_entry(
	temp_root: Path,
	path: Path,
	expected_identity: os.stat_result,
	parent_identity: os.stat_result,
	kind: str,
	parent_descriptor: int,
	cleanup_descriptor: int,
) -> None:
	token = os.urandom(16).hex()
	quarantine_name = f"entry-{expected_identity.st_dev:x}-{expected_identity.st_ino:x}-{token}"
	journal_name, journal_identity = _write_quarantine_journal(
		temp_root,
		path,
		expected_identity,
		parent_identity,
		kind,
		token,
		quarantine_name,
		cleanup_descriptor,
	)
	try:
		_rename_entry_to_cleanup_area(path, quarantine_name, parent_descriptor, cleanup_descriptor)
	except OSError:
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)
		raise
	moved = os.stat(quarantine_name, dir_fd=cleanup_descriptor, follow_symlinks=False)
	if not _same_file_identity(expected_identity, moved) or not _quarantined_kind_matches(
		moved, kind
	):
		_restore_quarantined_entry(path, quarantine_name, parent_descriptor, cleanup_descriptor)
		os.fsync(parent_descriptor)
		os.fsync(cleanup_descriptor)
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)
		raise OSError(f"temporary workspace entry changed during removal: {path}")
	try:
		_delete_quarantined_entry(quarantine_name, kind, cleanup_descriptor)
		os.fsync(cleanup_descriptor)
	except OSError:
		_restore_failed_quarantine(path, quarantine_name, parent_descriptor, cleanup_descriptor)
		_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)
		raise
	_remove_quarantine_journal(journal_name, journal_identity, cleanup_descriptor)


def _restore_failed_quarantine(
	path: Path,
	quarantine_name: str,
	parent_descriptor: int,
	cleanup_descriptor: int,
) -> None:
	try:
		_restore_quarantined_entry(path, quarantine_name, parent_descriptor, cleanup_descriptor)
		os.fsync(parent_descriptor)
		os.fsync(cleanup_descriptor)
	except OSError as restore_error:
		raise OSError(
			f"temporary workspace entry could not be restored from cleanup area: {path}"
		) from restore_error


def _remove_link(path: Path, metadata: os.stat_result) -> None:
	_remove_by_quarantine(path, metadata, kind="link")


def _remove_file(path: Path, expected_identity: os.stat_result) -> None:
	_remove_by_quarantine(path, expected_identity, kind="file")


def _remove_directory(path: Path, *, expected_identity: os.stat_result) -> None:
	_remove_by_quarantine(path, expected_identity, kind="directory")
