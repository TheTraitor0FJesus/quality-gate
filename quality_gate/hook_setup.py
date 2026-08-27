"""Safe installation of the two managed native-Git wrappers."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path


class HookIntegrationError(RuntimeError):
	"""Native hook setup would overwrite or hide an existing integration."""


MANAGED_HOOKS = frozenset({"pre-commit", "pre-push"})
_LOGGER = logging.getLogger(__name__)


def _validate_wrappers(wrappers: Mapping[str, str]) -> None:
	unknown = set(wrappers) - MANAGED_HOOKS
	if unknown:
		raise HookIntegrationError(f"unknown managed hook: {sorted(unknown)[0]}")


def _validate_existing(paths: Mapping[str, Path], wrappers: Mapping[str, str]) -> None:
	for name, path in paths.items():
		if path.is_symlink() or (path.exists() and not path.is_file()):
			raise HookIntegrationError(f"managed hook is not a regular file: {path}")
		if path.exists() and path.read_text(encoding="utf-8") != wrappers[name]:
			raise HookIntegrationError(
				f"managed hook already exists with different content: {path}"
			)


def _write_missing(paths: Mapping[str, Path], wrappers: Mapping[str, str]) -> tuple[str, ...]:
	created: list[str] = []
	temporary_paths: list[Path] = []
	try:
		for name, content in wrappers.items():
			path = paths[name]
			if path.exists():
				continue
			with tempfile.NamedTemporaryFile(
				mode="w", encoding="utf-8", newline="\n", dir=path.parent, delete=False
			) as temporary:
				temporary.write(content)
				temporary_paths.append(Path(temporary.name))
			os.replace(temporary.name, path)
			created.append(name)
	except OSError:
		for name in created:
			try:
				paths[name].unlink()
			except OSError:
				_LOGGER.warning("failed to roll back native hook: %s", paths[name], exc_info=True)
		raise
	finally:
		for temporary_path in temporary_paths:
			try:
				temporary_path.unlink()
			except FileNotFoundError:
				pass
	return tuple(created)


def install_hooks(hooks_dir: Path, wrappers: Mapping[str, str]) -> tuple[str, ...]:
	"""Install only missing managed wrappers and refuse conflicting files."""
	_validate_wrappers(wrappers)
	hooks_dir = hooks_dir.resolve()
	if hooks_dir.exists() and not hooks_dir.is_dir():
		raise HookIntegrationError(f"hooks directory is not a directory: {hooks_dir}")
	hooks_dir.mkdir(parents=True, exist_ok=True)
	paths = {name: hooks_dir / name for name in wrappers}
	_validate_existing(paths, wrappers)
	return _write_missing(paths, wrappers)
