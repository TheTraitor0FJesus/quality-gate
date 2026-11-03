"""Portable, hook-owned configuration with atomic one-generation rollback.

This boundary stores configuration only. Selecting a version here does not verify
or install that release, register a hook, or prove deployment readiness.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from typing import Any

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_events import is_junction
from _shared.quality_storage import StorageBusyError, atomic_json, directory_transaction

SCHEMA = 1
MANAGED_BY = "codex-python-quality-hooks"
MAX_SETTINGS_BYTES = 64 * 1024
MECHANISMS = frozenset({"ruff", "secrets", "integrity", "types"})
RELEASE = re.compile(r"^v\d+\.\d+\.\d+$")
MAX_PATH_CHARS = 4096
DEFAULT_BUDGET_SECONDS = 10.0
MIN_BUDGET_SECONDS = 0.1
MAX_BUDGET_SECONDS = 10
DEFAULT_INSTALL_LOCK_WAIT_SECONDS = 10.0
MIN_INSTALL_LOCK_WAIT_SECONDS = 0.1
MAX_INSTALL_LOCK_WAIT_SECONDS = 120.0


class SettingsError(ValueError):
	"""A settings operation cannot safely establish valid configuration."""


def default_settings_path() -> Path:
	"""Resolve the active host rather than the location of a repository backup."""
	root = os.environ.get("CODEX_HOME")
	if not root:
		profile = os.environ.get("USERPROFILE") if os.name == "nt" else os.environ.get("HOME")
		if not profile:
			raise SettingsError("codex_root_unavailable")
		root = str(Path(profile) / ".codex")
	return Path(root) / "hooks" / "_shared" / "python-quality.json"


def validate_selection(selection: object) -> dict[str, Any]:
	"""Validate configuration shape without claiming external release integrity."""
	keys = {"baseline_release", "provider_release", "enabled"}
	optional = {
		"baseline_python",
		"baseline_python_version",
		"budget_seconds",
		"install_lock_wait_seconds",
	}
	if (
		not isinstance(selection, dict)
		or not keys <= set(selection)
		or set(selection) - keys - optional
	):
		raise SettingsError("invalid_settings")
	for name in ("baseline_release", "provider_release"):
		release = selection[name]
		if release is not None and (not isinstance(release, str) or not RELEASE.fullmatch(release)):
			raise SettingsError("invalid_release_identity")
	enabled = selection["enabled"]
	if (
		not isinstance(enabled, list)
		or any(not isinstance(name, str) or name not in MECHANISMS for name in enabled)
		or len(set(enabled)) != len(enabled)
	):
		raise SettingsError("invalid_mechanism_selection")
	if enabled and (selection["baseline_release"] is None or selection["provider_release"] is None):
		raise SettingsError("release_selection_required")
	result: dict[str, Any] = {
		"baseline_release": selection["baseline_release"],
		"provider_release": selection["provider_release"],
		"enabled": list(enabled),
	}
	if "baseline_python" in selection or "baseline_python_version" in selection:
		python = selection.get("baseline_python")
		version = selection.get("baseline_python_version")
		if (
			not isinstance(python, str)
			or len(python) > MAX_PATH_CHARS
			or not Path(python).is_absolute()
			or not isinstance(version, str)
			or not re.fullmatch(r"\d+\.\d+", version)
		):
			raise SettingsError("invalid_baseline_runtime")
		result.update(baseline_python=python, baseline_python_version=version)
	if "budget_seconds" in selection:
		budget = selection["budget_seconds"]
		if (
			type(budget) not in {int, float}
			or not math.isfinite(budget)
			or not MIN_BUDGET_SECONDS <= budget <= MAX_BUDGET_SECONDS
		):
			raise SettingsError("invalid_edit_budget")
		result["budget_seconds"] = budget
	_install_lock_wait(selection, result)
	return result


def _install_lock_wait(selection: dict[str, Any], result: dict[str, Any]) -> None:
	if "install_lock_wait_seconds" not in selection:
		return
	wait_seconds = selection["install_lock_wait_seconds"]
	if (
		type(wait_seconds) not in {int, float}
		or isinstance(wait_seconds, float)
		and not math.isfinite(wait_seconds)
		or not MIN_INSTALL_LOCK_WAIT_SECONDS <= wait_seconds <= MAX_INSTALL_LOCK_WAIT_SECONDS
	):
		raise SettingsError("invalid_install_lock_wait")
	result["install_lock_wait_seconds"] = wait_seconds


def install_lock_wait_seconds(root: Path) -> float:
	"""Read the configured install transaction wait, using its compatible default."""
	path = root / "hooks" / "_shared" / "python-quality.json"
	try:
		path.stat()
	except FileNotFoundError:
		return DEFAULT_INSTALL_LOCK_WAIT_SECONDS
	return float(
		load_settings(path)["current"].get(
			"install_lock_wait_seconds", DEFAULT_INSTALL_LOCK_WAIT_SECONDS
		)
	)


@contextmanager
def install_transaction(root: Path) -> Iterator[None]:
	"""Serialize an install change using the root's configured bounded wait."""
	# ponytail: one installer per root; use finer locks if safe independent updates are needed.
	deadline = monotonic() + install_lock_wait_seconds(root)
	with directory_transaction(root, deadline=deadline):
		yield


def load_settings(path: Path) -> dict[str, Any]:
	"""Read one bounded managed envelope; preserve corrupt or unrelated files."""
	try:
		path.stat()
	except FileNotFoundError as error:
		raise SettingsError("settings_missing") from error
	if not path.is_file():
		raise SettingsError("invalid_settings")
	with path.open("rb") as source:
		content = source.read(MAX_SETTINGS_BYTES + 1)
	if len(content) > MAX_SETTINGS_BYTES:
		raise SettingsError("settings_size_limit")
	try:
		document = json.loads(content)
	except (UnicodeError, json.JSONDecodeError) as error:
		raise SettingsError("invalid_settings_json") from error
	if not isinstance(document, dict) or document.get("managed_by") != MANAGED_BY:
		raise SettingsError("unmanaged_settings")
	if (
		set(document) != {"managed_by", "schema", "current", "previous"}
		or type(document["schema"]) is not int
		or document["schema"] != SCHEMA
	):
		raise SettingsError("unsupported_settings_schema")
	document["current"] = validate_selection(document["current"])
	if document["previous"] is not None:
		document["previous"] = validate_selection(document["previous"])
	return document


def settings_operation(path: Path, action: str, selection: object = None) -> dict[str, Any]:
	"""Serialize settings changes and preserve both selections in one atomic file."""
	path = Path(os.path.abspath(path))
	if any(parent.is_symlink() or is_junction(parent) for parent in (path, *path.parents)):
		raise SettingsError("unsafe_settings_path")
	with directory_transaction(path.parent):
		if action == "init" and not path.exists():
			document = {
				"managed_by": MANAGED_BY,
				"schema": SCHEMA,
				"current": {
					"baseline_release": None,
					"provider_release": None,
					"enabled": [],
					"install_lock_wait_seconds": DEFAULT_INSTALL_LOCK_WAIT_SECONDS,
				},
				"previous": None,
			}
			atomic_json(path, document)
			return document
		document = load_settings(path)
		if action in {"show", "init"}:
			return document
		if action == "rollback":
			if document["previous"] is None:
				raise SettingsError("previous_settings_missing")
			updated = document["previous"]
		elif action == "update":
			updated = validate_selection(selection)
		else:
			raise SettingsError("unsupported_settings_operation")
		if updated == document["current"]:
			return document
		document["previous"], document["current"] = document["current"], updated
		atomic_json(path, document)
		return document


def read_selection() -> object:
	content = sys.stdin.buffer.read(MAX_SETTINGS_BYTES + 1)
	if len(content) > MAX_SETTINGS_BYTES:
		raise SettingsError("settings_size_limit")
	try:
		return json.loads(content)
	except (UnicodeError, json.JSONDecodeError) as error:
		raise SettingsError("invalid_settings_json") from error


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--file", type=Path)
	parser.add_argument("action", choices=("init", "show", "update", "rollback"))
	arguments = parser.parse_args()
	try:
		path = arguments.file or default_settings_path()
		selection = read_selection() if arguments.action == "update" else None
		result = settings_operation(path, arguments.action, selection)
	except SettingsError as error:
		result = {"schema": SCHEMA, "status": "unchecked", "reason": str(error)}
		sys.stdout.write(json.dumps(result) + "\n")
		return 2
	except StorageBusyError:
		sys.stdout.write(
			json.dumps({"schema": SCHEMA, "status": "unchecked", "reason": "settings_busy"}) + "\n"
		)
		return 2
	except OSError:
		sys.stdout.write(
			json.dumps(
				{"schema": SCHEMA, "status": "unchecked", "reason": "settings_io_unavailable"}
			)
			+ "\n"
		)
		return 2
	sys.stdout.write(json.dumps(result, ensure_ascii=True) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
