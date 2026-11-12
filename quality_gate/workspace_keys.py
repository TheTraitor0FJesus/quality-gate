"""Protect and recover temporary-workspace ownership keys."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import hmac
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from .path_safety import is_link_or_reparse, is_reparse_point
from .workspace_windows import _ensure_windows_private_directory

_OWNERSHIP_KEY_NAME = "temp-workspace.key"


_RECOVERY_KEY_NAME = "temp-workspace-recovery.key"


_OWNERSHIP_KEY_BYTES = 32


_SIGNATURE_HEX_LENGTH = 64


_OWNERSHIP_KEY_MODE = 0o600


_PUBLICATION_LINK_SETTLE_ATTEMPTS = 100


_PUBLICATION_LINK_SETTLE_DELAY_SECONDS = 0.001


_SINGLE_LINK_COUNT = 1


_PUBLICATION_TRANSITION_LINK_COUNT = 2


_PRIVATE_DIRECTORY_OWNER_ACCESS = 0o300


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
	return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _optional_os_flag(name: str) -> int:
	return cast(int, getattr(os, name, 0))


def _current_user_id() -> int:
	get_uid_name = "getuid"
	get_uid = cast(Any, getattr(os, get_uid_name))
	return cast(int, get_uid())


def _canonical_json(value: dict[str, object]) -> bytes:
	return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _user_state_directory() -> Path:
	if os.name == "nt":
		base = os.environ.get("LOCALAPPDATA")
		if not base:
			base = str(Path.home() / "AppData" / "Local")
	else:
		base = os.environ.get("XDG_STATE_HOME")
		if not base or not Path(base).is_absolute():
			base = str(Path.home() / ".local" / "state")
	return Path(base) / "quality-gate" / "temp-workspace"


def _ensure_private_directory(path: Path, *, restrict_existing: bool = False) -> os.stat_result:
	path.parent.mkdir(parents=True, exist_ok=True)
	if os.name == "nt":
		return _ensure_windows_private_directory(path)
	return _ensure_posix_private_directory(path, restrict_existing=restrict_existing)


def _ensure_posix_private_directory(path: Path, *, restrict_existing: bool) -> os.stat_result:
	try:
		path.mkdir(mode=0o700)
	except FileExistsError:
		pass
	metadata = path.lstat()
	if is_link_or_reparse(metadata) or not stat.S_ISDIR(metadata.st_mode):
		raise OSError(f"private temporary directory is unsafe: {path}")
	if metadata.st_uid != _current_user_id():
		raise OSError(f"private temporary directory has a foreign owner: {path}")
	if stat.S_IMODE(metadata.st_mode) & 0o077 and restrict_existing:
		os.chmod(path, 0o700)
		metadata = path.lstat()
	if stat.S_IMODE(metadata.st_mode) & 0o077:
		raise OSError(f"private temporary directory permissions are unsafe: {path}")
	return metadata


def _ownership_key(*, create: bool) -> bytes | None:
	path = _user_state_directory() / _OWNERSHIP_KEY_NAME
	if create:
		_ensure_private_directory(path.parent, restrict_existing=True)
	try:
		key = _read_ownership_key(path)
	except OSError:
		if not _incomplete_ownership_key(path):
			raise
		path = path.with_name(_RECOVERY_KEY_NAME)
		key = _read_ownership_key(path)
	if key is None and path.name == _OWNERSHIP_KEY_NAME:
		recovery = path.with_name(_RECOVERY_KEY_NAME)
		recovery_key = _read_ownership_key(recovery)
		if recovery_key is not None:
			return recovery_key
	if key is not None or not create:
		return key
	_create_ownership_key_file(path)
	return _read_ownership_key(path)


def _incomplete_ownership_key(path: Path) -> bool:
	try:
		metadata = path.lstat()
	except OSError:
		return False
	return (
		_plain_lock_file(path, metadata)
		and metadata.st_size < _OWNERSHIP_KEY_BYTES
		and (os.name == "nt" or metadata.st_uid == _current_user_id())
		and (os.name == "nt" or stat.S_IMODE(metadata.st_mode) == _OWNERSHIP_KEY_MODE)
	)


def _published_name_matches(
	parent: int,
	target_name: str,
	identity: os.stat_result,
	*,
	link_count: int,
	content_size: int,
) -> bool:
	try:
		current = os.stat(target_name, dir_fd=parent, follow_symlinks=False)
	except FileNotFoundError:
		return False
	return (
		_same_file_identity(identity, current)
		and stat.S_ISREG(current.st_mode)
		and current.st_uid == identity.st_uid
		and stat.S_IMODE(current.st_mode) == stat.S_IMODE(identity.st_mode)
		and current.st_nlink == link_count
		and current.st_size == content_size
	)


def _remove_published_staging_link(
	parent: int,
	temporary_name: str,
	target_name: str,
	identity: os.stat_result,
	content_size: int,
) -> None:
	try:
		current = os.stat(temporary_name, dir_fd=parent, follow_symlinks=False)
	except FileNotFoundError:
		return
	if not _same_file_identity(identity, current):
		return
	try:
		current = os.stat(temporary_name, dir_fd=parent, follow_symlinks=False)
	except FileNotFoundError:
		return
	if not _same_file_identity(identity, current):
		return
	if _published_name_matches(
		parent,
		target_name,
		identity,
		link_count=_PUBLICATION_TRANSITION_LINK_COUNT,
		content_size=content_size,
	):
		os.unlink(temporary_name, dir_fd=parent)
		return
	if not _published_name_matches(
		parent,
		target_name,
		identity,
		link_count=_SINGLE_LINK_COUNT,
		content_size=content_size,
	):
		raise OSError(f"temporary publication target changed before staging removal: {target_name}")


def _remove_published_temporary(
	parent: int,
	temporary_name: str,
	target_name: str,
	identity: os.stat_result,
	content_size: int,
) -> None:
	try:
		_remove_published_staging_link(
			parent,
			temporary_name,
			target_name,
			identity,
			content_size,
		)
	except FileNotFoundError as error:
		if _published_name_matches(
			parent,
			target_name,
			identity,
			link_count=_SINGLE_LINK_COUNT,
			content_size=content_size,
		):
			return
		raise error
	if not _published_name_matches(
		parent,
		target_name,
		identity,
		link_count=_SINGLE_LINK_COUNT,
		content_size=content_size,
	):
		raise OSError(f"temporary publication target changed during staging removal: {target_name}")


def _publish_hard_link(
	parent: int,
	temporary_name: str,
	target_name: str,
	identity: os.stat_result,
	content_size: int,
) -> None:
	os.link(temporary_name, target_name, src_dir_fd=parent, dst_dir_fd=parent)
	_remove_published_temporary(
		parent,
		temporary_name,
		target_name,
		identity,
		content_size,
	)


def _publish_new_file(path: Path, content: bytes) -> None:
	"""Publish complete bytes without replacing an existing name."""
	descriptor, temporary_name = tempfile.mkstemp(prefix=".quality-gate-pending-", dir=path.parent)
	temporary = Path(temporary_name)
	identity = os.fstat(descriptor)
	try:
		with os.fdopen(descriptor, "wb") as stream:
			stream.write(content)
			stream.flush()
			os.fsync(stream.fileno())
		if os.name == "nt":
			os.rename(temporary, path)
		else:
			flags = os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW")
			parent = os.open(path.parent, flags)
			try:
				if not _same_file_identity(os.fstat(parent), path.parent.lstat()):
					raise OSError(f"temporary publication directory changed: {path.parent}")
				use_hard_link = False
				try:
					_rename_without_replacement(temporary.name, path.name, parent, parent)
				except AttributeError:
					use_hard_link = True
				except OSError as error:
					if error.errno not in (None, errno.ENOSYS, errno.EINVAL):
						raise
					use_hard_link = True
				if use_hard_link:
					_publish_hard_link(parent, temporary.name, path.name, identity, len(content))
				os.fsync(parent)
			finally:
				os.close(parent)
	finally:
		try:
			current = temporary.lstat()
			if _same_file_identity(identity, current):
				temporary.unlink()
		except FileNotFoundError:
			pass


def _create_ownership_key_file(path: Path) -> None:
	try:
		_publish_new_file(path, os.urandom(_OWNERSHIP_KEY_BYTES))
	except FileExistsError:
		pass


def _private_state_directory_fd(path: Path) -> int | None:
	flags = os.O_RDONLY | _optional_os_flag("O_DIRECTORY") | _optional_os_flag("O_NOFOLLOW")
	try:
		parent = os.open(path.parent, flags)
	except OSError:
		return None
	try:
		identity = os.fstat(parent)
		path_identity = path.parent.lstat()
	except OSError:
		os.close(parent)
		return None
	if (
		not stat.S_ISDIR(identity.st_mode)
		or not _same_file_identity(identity, path_identity)
		or identity.st_uid != _current_user_id()
		or stat.S_IMODE(identity.st_mode) & 0o077
		or (stat.S_IMODE(identity.st_mode) & _PRIVATE_DIRECTORY_OWNER_ACCESS)
		!= _PRIVATE_DIRECTORY_OWNER_ACCESS
	):
		os.close(parent)
		return None
	return parent


def _matching_key_staging_name(parent: int, target: os.stat_result) -> str | None:
	matches: list[str] = []
	for name in os.listdir(parent):
		if not name.startswith(".quality-gate-pending-"):
			continue
		try:
			candidate = os.stat(name, dir_fd=parent, follow_symlinks=False)
		except FileNotFoundError:
			continue
		if (
			_same_file_identity(target, candidate)
			and stat.S_ISREG(candidate.st_mode)
			and candidate.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT
			and candidate.st_uid == _current_user_id()
			and stat.S_IMODE(candidate.st_mode) == _OWNERSHIP_KEY_MODE
		):
			matches.append(name)
	return matches[0] if len(matches) == 1 else None


def _remove_key_staging_link(parent: int, path: Path, expected: os.stat_result, name: str) -> None:
	try:
		descriptor = os.open(
			name,
			os.O_RDONLY | _optional_os_flag("O_NOFOLLOW") | _optional_os_flag("O_NONBLOCK"),
			dir_fd=parent,
		)
	except OSError as error:
		if isinstance(error, FileNotFoundError):
			return
		if _published_name_matches(
			parent,
			path.name,
			expected,
			link_count=_SINGLE_LINK_COUNT,
			content_size=expected.st_size,
		):
			return
		raise
	try:
		staging_identity = os.fstat(descriptor)
		try:
			current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
			staging = os.stat(name, dir_fd=parent, follow_symlinks=False)
		except FileNotFoundError:
			return
		if (
			_same_file_identity(expected, current)
			and _same_file_identity(current, staging_identity)
			and _same_file_identity(staging_identity, staging)
			and current.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT
			and staging.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT
			and stat.S_ISREG(staging_identity.st_mode)
		):
			try:
				current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
				staging = os.stat(name, dir_fd=parent, follow_symlinks=False)
			except FileNotFoundError:
				return
			if (
				_same_file_identity(expected, current)
				and _same_file_identity(current, staging_identity)
				and _same_file_identity(staging_identity, staging)
				and current.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT
				and staging.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT
				and stat.S_ISREG(staging.st_mode)
			):
				try:
					os.unlink(name, dir_fd=parent)
				except FileNotFoundError as error:
					if not _published_name_matches(
						parent,
						path.name,
						expected,
						link_count=_SINGLE_LINK_COUNT,
						content_size=expected.st_size,
					):
						raise error
				os.fsync(parent)
	finally:
		os.close(descriptor)


def _recover_interrupted_key_publication(
	path: Path, metadata: os.stat_result
) -> os.stat_result | None:
	"""Remove only a matching staging link left by a stopped key publisher."""
	if (
		os.name == "nt"
		or path.name not in {_OWNERSHIP_KEY_NAME, _RECOVERY_KEY_NAME}
		or metadata.st_nlink != _PUBLICATION_TRANSITION_LINK_COUNT
		or not stat.S_ISREG(metadata.st_mode)
		or metadata.st_uid != _current_user_id()
		or stat.S_IMODE(metadata.st_mode) != _OWNERSHIP_KEY_MODE
	):
		return None
	parent = _private_state_directory_fd(path)
	if parent is None:
		return None
	try:
		try:
			current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
		except FileNotFoundError:
			return None
		if (
			not _same_file_identity(metadata, current)
			or current.st_nlink != _PUBLICATION_TRANSITION_LINK_COUNT
			or not stat.S_ISREG(current.st_mode)
			or current.st_uid != _current_user_id()
			or stat.S_IMODE(current.st_mode) != _OWNERSHIP_KEY_MODE
		):
			return None
		staging_name = _matching_key_staging_name(parent, current)
		if staging_name is None:
			return None
		_remove_key_staging_link(parent, path, current, staging_name)
		try:
			current = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
		except FileNotFoundError:
			return None
		if (
			_same_file_identity(metadata, current)
			and current.st_nlink == 1
			and stat.S_ISREG(current.st_mode)
			and current.st_uid == _current_user_id()
			and stat.S_IMODE(current.st_mode) == _OWNERSHIP_KEY_MODE
		):
			return current
		return None
	finally:
		os.close(parent)


def _read_ownership_key(path: Path) -> bytes | None:
	try:
		metadata = path.lstat()
	except FileNotFoundError:
		return None
	if metadata.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT:
		recovered = _recover_interrupted_key_publication(path, metadata)
		if recovered is not None:
			metadata = recovered
	if not _plain_lock_file(path, metadata) or metadata.st_size != _OWNERSHIP_KEY_BYTES:
		raise OSError(f"temporary workspace ownership key is unsafe: {path}")
	metadata = _check_ownership_key_permissions(path, metadata)
	try:
		with path.open("rb") as stream:
			opened = os.fstat(stream.fileno())
			if not _same_file_identity(metadata, opened) or not _plain_lock_file(path, opened):
				return None
			key = stream.read(_OWNERSHIP_KEY_BYTES + 1)
		current = path.lstat()
	except OSError:
		return None
	if (
		len(key) != _OWNERSHIP_KEY_BYTES
		or not _same_file_identity(metadata, current)
		or not _plain_lock_file(path, current)
	):
		return None
	return key


def _check_ownership_key_permissions(path: Path, metadata: os.stat_result) -> os.stat_result:
	if os.name != "nt":
		if metadata.st_uid != _current_user_id():
			raise OSError(f"temporary workspace ownership key has a foreign owner: {path}")
		if stat.S_IMODE(metadata.st_mode) != _OWNERSHIP_KEY_MODE:
			os.chmod(path, _OWNERSHIP_KEY_MODE)
			metadata = path.lstat()
			if stat.S_IMODE(metadata.st_mode) != _OWNERSHIP_KEY_MODE:
				raise OSError(f"temporary workspace ownership key permissions are unsafe: {path}")
	return metadata


def _signed_record(payload: dict[str, object], *, create_key: bool) -> bytes | None:
	key = _ownership_key(create=create_key)
	if key is None:
		return None
	signature = hmac.new(key, _canonical_json(payload), hashlib.sha256).hexdigest()
	return _canonical_json({**payload, "signature": signature})


def _valid_signed_record(content: bytes) -> dict[str, object] | None:
	try:
		record = json.loads(content)
	except (json.JSONDecodeError, UnicodeDecodeError):
		return None
	if not isinstance(record, dict):
		return None
	signature = record.get("signature")
	if (
		not isinstance(signature, str)
		or len(signature) != _SIGNATURE_HEX_LENGTH
		or any(character not in "0123456789abcdef" for character in signature)
	):
		return None
	if content != _canonical_json(record):
		return None
	payload = {key: value for key, value in record.items() if key != "signature"}
	key = _ownership_key(create=False)
	keys = [key] if key is not None else []
	try:
		recovery_key = _read_ownership_key(_user_state_directory() / _RECOVERY_KEY_NAME)
	except OSError:
		recovery_key = None
	if recovery_key is not None and recovery_key not in keys:
		keys.append(recovery_key)
	for candidate in keys:
		expected = hmac.new(candidate, _canonical_json(payload), hashlib.sha256).hexdigest()
		if hmac.compare_digest(signature, expected):
			return payload
	return None


def _plain_lock_file(path: Path, metadata: os.stat_result) -> bool:
	if metadata.st_nlink == _PUBLICATION_TRANSITION_LINK_COUNT:
		for _ in range(_PUBLICATION_LINK_SETTLE_ATTEMPTS):
			time.sleep(_PUBLICATION_LINK_SETTLE_DELAY_SECONDS)
			try:
				current = path.lstat()
			except OSError:
				return False
			if not _same_file_identity(metadata, current):
				return False
			if current.st_nlink != _PUBLICATION_TRANSITION_LINK_COUNT:
				metadata = current
				break
		else:
			return False
	return (
		stat.S_ISREG(metadata.st_mode)
		and not path.is_symlink()
		and not is_reparse_point(metadata)
		and metadata.st_nlink == 1
	)


def _rename_without_replacement(
	source_name: str,
	destination_name: str,
	source_descriptor: int,
	destination_descriptor: int,
) -> None:
	result: int
	if sys.platform.startswith("linux"):
		libc = cast(Any, ctypes.CDLL(None, use_errno=True))
		renameat2 = cast(Any, libc.renameat2)
		result = renameat2(
			source_descriptor,
			os.fsencode(source_name),
			destination_descriptor,
			os.fsencode(destination_name),
			1,
		)
	elif sys.platform == "darwin":
		libc = cast(Any, ctypes.CDLL(None, use_errno=True))
		renameatx = cast(Any, libc.renameatx_np)
		result = renameatx(
			source_descriptor,
			os.fsencode(source_name),
			destination_descriptor,
			os.fsencode(destination_name),
			0x00000004,
		)
	else:
		raise OSError("safe recovery of a replaced temporary entry is unavailable on this platform")
	if result != 0:
		error = ctypes.get_errno()
		raise OSError(error, os.strerror(error), destination_name)
