"""Remove workspace trees while verifying each filesystem identity."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

from .path_safety import is_link_or_reparse
from .workspace_constants import _ROOT_CONTROL_FILE_NAMES
from .workspace_keys import _plain_lock_file, _same_file_identity
from .workspace_quarantine import _remove_directory, _remove_file, _remove_link

_DIRECTORY_REMOVAL_MAX_RESCANS = 16


def _remove_tree(path: Path, identity: os.stat_result) -> None:
	pending = [(path, identity)]
	rescans: dict[tuple[Path, int, int], int] = {}
	while pending:
		tree_path, tree_identity = pending.pop()
		_remove_tree_attempt(tree_path, tree_identity, pending, rescans)


def _remove_tree_attempt(
	tree_path: Path,
	tree_identity: os.stat_result,
	pending: list[tuple[Path, os.stat_result]],
	rescans: dict[tuple[Path, int, int], int],
) -> None:
	directories, deferred_root_controls = _scan_tree_for_removal(tree_path, tree_identity)
	for directory, expected in reversed(directories[1:]):
		if not _remove_collected_directory_with_retry(directory, expected, rescans):
			# Retry only the raced subtree; unrelated directories have separate budgets.
			pending.extend(((tree_path, tree_identity), (directory, expected)))
			return
	for child_path, child_identity in deferred_root_controls:
		_remove_scanned_child(
			tree_path,
			tree_identity,
			child_path,
			child_identity,
			[],
			root_control=True,
		)
	if directories:
		root_path, root_identity = directories[0]
		if not _remove_collected_directory_with_retry(root_path, root_identity, rescans):
			pending.append((root_path, root_identity))


def _remove_collected_directory_with_retry(
	path: Path,
	expected: os.stat_result,
	rescans: dict[tuple[Path, int, int], int],
) -> bool:
	key = (path, expected.st_dev, expected.st_ino)
	try:
		_remove_collected_directory(path, expected)
	except OSError as error:
		if not _can_rescan_directory(error, rescans.get(key, 0)):
			raise
		rescans[key] = rescans.get(key, 0) + 1
		return False
	rescans.pop(key, None)
	return True


def _can_rescan_directory(error: OSError, rescans: int) -> bool:
	return (
		error.errno in {errno.ENOTEMPTY, errno.EEXIST} and rescans < _DIRECTORY_REMOVAL_MAX_RESCANS
	)


def _scan_tree_for_removal(
	path: Path, identity: os.stat_result
) -> tuple[list[tuple[Path, os.stat_result]], list[tuple[Path, os.stat_result]]]:
	directories: list[tuple[Path, os.stat_result]] = []
	deferred_root_controls: list[tuple[Path, os.stat_result]] = []
	pending = [(path, identity)]
	while pending:
		directory, expected = pending.pop()
		entries = _directory_entries_for_removal(directory, expected)
		if entries is None:
			continue
		directories.append((directory, expected))
		for child_path, scanned_identity in entries:
			_remove_scanned_child(
				directory,
				expected,
				child_path,
				scanned_identity,
				pending,
				deferred_root_controls=(deferred_root_controls if directory == path else None),
			)
	return directories, deferred_root_controls


def _directory_entries_for_removal(
	directory: Path, expected: os.stat_result
) -> list[tuple[Path, os.stat_result]] | None:
	try:
		before = directory.lstat()
	except FileNotFoundError:
		return None
	if not _same_file_identity(expected, before):
		raise OSError(f"temporary workspace identity changed while removing it: {directory}")
	if is_link_or_reparse(before):
		_remove_link(directory, before)
		return None
	if not stat.S_ISDIR(before.st_mode):
		_remove_file(directory, before)
		return None
	try:
		with os.scandir(directory) as children:
			entries = [(Path(child.path), Path(child.path).lstat()) for child in children]
	except FileNotFoundError:
		return None
	after = directory.lstat()
	if is_link_or_reparse(after) or not _same_file_identity(before, after):
		raise OSError(f"temporary workspace directory changed while scanning it: {directory}")
	return entries


def _remove_scanned_child(
	parent: Path,
	parent_identity: os.stat_result,
	path: Path,
	scanned_identity: os.stat_result,
	pending: list[tuple[Path, os.stat_result]],
	*,
	deferred_root_controls: list[tuple[Path, os.stat_result]] | None = None,
	root_control: bool = False,
) -> None:
	parent_now = parent.lstat()
	if is_link_or_reparse(parent_now) or not _same_file_identity(parent_identity, parent_now):
		raise OSError(f"temporary workspace directory changed during removal: {parent}")
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return
	if not _same_file_identity(scanned_identity, metadata):
		raise OSError(f"temporary workspace entry changed during removal: {path}")
	if root_control and not _plain_lock_file(path, metadata):
		raise OSError(f"temporary workspace control file changed during removal: {path}")
	if deferred_root_controls is not None and path.name in _ROOT_CONTROL_FILE_NAMES:
		if not _plain_lock_file(path, metadata):
			raise OSError(f"temporary workspace control file changed during removal: {path}")
		deferred_root_controls.append((path, scanned_identity))
		return
	if is_link_or_reparse(metadata):
		_remove_link(path, metadata)
	elif stat.S_ISDIR(metadata.st_mode):
		pending.append((path, metadata))
	else:
		_remove_file(path, metadata)


def _remove_collected_directory(path: Path, expected: os.stat_result) -> None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return
	if not _same_file_identity(expected, metadata):
		raise OSError(f"temporary workspace identity changed before removal: {path}")
	if is_link_or_reparse(metadata):
		_remove_link(path, metadata)
	else:
		_remove_directory(path, expected_identity=expected)


def _remove_entry(path: Path, *, expected_identity: os.stat_result | None = None) -> None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return
	if expected_identity is not None and not _same_file_identity(expected_identity, metadata):
		raise OSError(f"temporary workspace identity changed before removal: {path}")
	if is_link_or_reparse(metadata):
		_remove_link(path, metadata)
	elif stat.S_ISDIR(metadata.st_mode):
		_remove_tree(path, metadata)
	else:
		_remove_file(path, metadata)
