"""Prepare, activate and roll back managed hook registration without trust edits."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from time import monotonic
from typing import Any

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_events import is_junction
from _shared.quality_settings import (
	MANAGED_BY,
	SettingsError,
	default_settings_path,
	install_transaction,
	load_settings,
	settings_operation,
)
from _shared.quality_storage import atomic_json

BUNDLE = (
	"_shared/__init__.py",
	"_shared/codex_hook_adapter.py",
	"_shared/command_safety.py",
	"_shared/github_safety.py",
	"_shared/hook_registration.py",
	"python_quality/__init__.py",
	"python_quality/check.py",
	"python_quality/install_post_edit_hook.py",
	"python_quality/post_edit_checks.py",
	"python_quality/run_post_edit_hook.cmd",
	"secret_scan/__init__.py",
	"secret_scan/check.py",
	"secret_scan/feedback.py",
	"secret_scan/inputs.py",
	"secret_scan/provider_bridge.py",
	"secret_scan/results.py",
	"file_integrity/__init__.py",
	"file_integrity/check.py",
	"file_integrity/feedback.py",
	"file_integrity/inputs.py",
	"file_integrity/provider_bridge.py",
	"file_integrity/results.py",
	"python_types/__init__.py",
	"python_types/check.py",
	"python_types/coverage.py",
	"python_types/feedback.py",
	"python_types/freshness.py",
	"python_types/inputs.py",
	"python_types/provider_bridge.py",
	"python_types/results.py",
	"python_types/selections.py",
	"_shared/quality_coordinator.py",
	"_shared/quality_diagnostics.py",
	"_shared/quality_completion.py",
	"_shared/quality_events.py",
	"_shared/quality_install_probe.py",
	"_shared/quality_feedback.py",
	"_shared/quality_install.py",
	"_shared/quality_legacy_security.py",
	"_shared/native_quality_gate.py",
	"_shared/quality_process.py",
	"_shared/quality_background.py",
	"_shared/quality_background_state.py",
	"_shared/quality_background_worker.py",
	"python_quality/provider_bridge.py",
	"_shared/quality_settings.py",
	"_shared/quality_storage.py",
	"_shared/quality_windows_jobs.py",
)
POST_EDIT_TIMEOUT_SECONDS = 120


class InstallError(ValueError):
	"""A fixed recovery reason that never includes user configuration contents."""


def receipt_path(root: Path) -> Path:
	return root / "hooks" / "_shared" / "python-quality-install.json"


def read_object(path: Path) -> dict[str, Any]:
	with path.open("rb") as stream:
		content = stream.read(256 * 1024 + 1)
	if len(content) > 256 * 1024:
		raise InstallError("configuration_limit")
	try:
		result = json.loads(content)
	except (UnicodeError, json.JSONDecodeError) as error:
		raise InstallError("invalid_configuration") from error
	if not isinstance(result, dict):
		raise InstallError("invalid_configuration")
	return result


def load_receipt(root: Path) -> dict[str, Any]:
	result = read_object(receipt_path(root))
	if (
		result.get("managed_by") != MANAGED_BY
		or type(result.get("schema")) is not int
		or result["schema"] != 1
	):
		raise InstallError("unmanaged_install_receipt")
	return result


def source_root(path: Path | None = None) -> Path:
	"""Resolve the checked-out hook source, independent of CODEX_HOME state."""
	return (path or Path(__file__).resolve().parents[1]).expanduser().resolve()


def bundle_identity(source: Path | None = None) -> dict[str, str]:
	source = source_root(source)
	result = {}
	for name in BUNDLE:
		path = source / name
		if path.is_symlink() or is_junction(path) or not path.is_file():
			raise InstallError("source_bundle_missing")
		result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
	return result


def registration_groups(root: Path, source: Path | None = None) -> dict[str, dict[str, Any]]:
	source = source_root(source)
	arguments = [
		sys.executable,
		"-B",
		str(source / "_shared" / "quality_coordinator.py"),
		"--settings",
		str(root / "hooks" / "_shared" / "python-quality.json"),
		"--registered",
	]
	handler = {
		"type": "command",
		"command": shlex.join(arguments),
		"commandWindows": subprocess.list2cmdline(arguments),
		# Preserve the work deadline plus bounded cleanup and startup/delivery.
		"timeout": 15,
		"statusMessage": "Python edit checks...",
	}
	groups = {
		event: {"matcher": "^Bash$", "hooks": [dict(handler)]}
		for event in ("PreToolUse", "PostToolUse")
	}
	groups["Stop"] = {
		"hooks": [{**handler, "timeout": 45, "statusMessage": "Completing Python type checks..."}]
	}
	groups["SubagentStop"] = groups["Stop"]
	groups["SessionEnd"] = {
		"hooks": [{**handler, "timeout": 3, "statusMessage": "Stopping Python type checks..."}]
	}
	groups["Interrupt"] = {
		"hooks": [
			{**handler, "timeout": 3, "statusMessage": "Cleaning interrupted Python checks..."}
		]
	}
	return groups


def bindings_present(configuration: dict[str, Any], groups: dict[str, Any]) -> bool:
	hooks = configuration.get("hooks")
	if not isinstance(hooks, dict):
		raise InstallError("invalid_configuration")
	for event, group in groups.items():
		entries = hooks.get(event)
		if not isinstance(entries, list) or entries.count(group) != 1:
			return False
	return True


def append_bindings(configuration: dict[str, Any], groups: dict[str, Any]) -> None:
	hooks = configuration.setdefault("hooks", {})
	if not isinstance(hooks, dict):
		raise InstallError("invalid_configuration")
	for event, group in groups.items():
		entries = hooks.setdefault(event, [])
		if not isinstance(entries, list):
			raise InstallError("invalid_configuration")
		if group in entries:
			raise InstallError("unmanaged_registration")
		entries.append(group)


def _prepare_locked(root: Path, source: Path | None = None) -> dict[str, Any]:
	source = source_root(source)
	configuration_path = root / "hooks.json"
	bundle = bundle_identity(source)
	configuration = read_object(configuration_path)
	path = receipt_path(root)
	paused = False
	if path.exists():
		previous = load_receipt(root)
		paused = previous.get("paused") is True
		if previous.get("state") in {"prepared", "active"}:
			if previous.get("bundle") != bundle or not bindings_present(
				configuration, previous["groups"]
			):
				raise InstallError("registration_conflict")
			return {"state": previous["state"], "trust_required": True}
		if previous.get("state") != "rolled_back":
			raise InstallError("incomplete_installation")
	settings_operation(root / "hooks" / "_shared" / "python-quality.json", "init")
	groups = registration_groups(root, source)
	created_events = [event for event in groups if event not in configuration.get("hooks", {})]
	append_bindings(configuration, groups)
	receipt = {
		"managed_by": MANAGED_BY,
		"schema": 1,
		"state": "preparing",
		**({"paused": True} if paused else {}),
		"groups": groups,
		"created_events": created_events,
		"bundle": bundle,
		"source_root": str(source),
	}
	atomic_json(path, receipt)
	atomic_json(configuration_path, configuration)
	receipt["state"] = "prepared"
	atomic_json(path, receipt)
	return {"state": "prepared", "trust_required": True}


def prepare(root: Path, source: Path | None = None) -> dict[str, Any]:
	with install_transaction(root):
		return _prepare_locked(root, source)


def _activate_locked(
	root: Path,
	*,
	typing_probe_root: Path | None = None,
	source: Path | None = None,
) -> dict[str, Any]:
	source = source_root(source)
	receipt = load_receipt(root)
	if receipt.get("state") != "prepared" or receipt.get("bundle") != bundle_identity(source):
		raise InstallError("installation_not_prepared")
	configuration = read_object(root / "hooks.json")
	if not bindings_present(configuration, receipt["groups"]) or receipt.get(
		"groups"
	) != registration_groups(root, source):
		raise InstallError("registration_conflict")
	settings_path = root / "hooks" / "_shared" / "python-quality.json"
	settings = load_settings(settings_path)["current"]
	from _shared import quality_install_probe

	probe_failure = quality_install_probe.probe(settings, typing_probe_root=typing_probe_root)
	if probe_failure is not None:
		raise InstallError(probe_failure)
	settings_operation(
		settings_path,
		"update",
		{**settings, "enabled": list(settings["enabled"])},
	)
	receipt["state"] = "active"
	atomic_json(receipt_path(root), receipt)
	return {"state": "active", "trust_required": True, "live_acceptance_required": True}


def activate(
	root: Path,
	*,
	typing_probe_root: Path | None = None,
	source: Path | None = None,
) -> dict[str, Any]:
	with install_transaction(root):
		return _activate_locked(root, typing_probe_root=typing_probe_root, source=source)


def _replace_group(
	configuration: dict[str, Any], event: str, old: dict[str, Any], new: dict[str, Any]
) -> None:
	entries = configuration["hooks"].get(event)
	if not isinstance(entries, list):
		raise InstallError("registration_conflict")
	old_count, new_count = entries.count(old), entries.count(new)
	if old == new and old_count == 1:
		return
	if old_count == 1 and new_count == 0:
		entries[entries.index(old)] = new
		return
	if old_count == 0 and new_count == 1:
		return
	raise InstallError("registration_conflict")


def _relocate_group(
	configuration: dict[str, Any], event: str, old: dict[str, Any], new: dict[str, Any]
) -> None:
	hooks = configuration.get("hooks")
	if not isinstance(hooks, dict):
		raise InstallError("invalid_configuration")
	entries = hooks.get(event)
	if entries is None:
		entries = []
		hooks[event] = entries
	if not isinstance(entries, list):
		raise InstallError("registration_conflict")
	old_count, new_count = entries.count(old), entries.count(new)
	if old == new and old_count == 1:
		return
	if old_count == 1 and new_count == 0:
		entries[entries.index(old)] = new
		return
	if old_count == 0 and new_count == 1:
		return
	if old_count == 0 and new_count == 0:
		managed = [
			entry
			for entry in entries
			if isinstance(entry, dict)
			and entry.get("matcher") == old.get("matcher")
			and isinstance(entry.get("hooks"), list)
			and any(
				isinstance(handler, dict)
				and "quality_coordinator.py"
				in " ".join(str(handler.get(key, "")) for key in ("command", "commandWindows"))
				for handler in entry["hooks"]
			)
		]
		if managed:
			raise InstallError("registration_conflict")
		entries.append(new)
		return
	raise InstallError("registration_conflict")


def _post_edit_group(configuration: dict[str, Any]) -> tuple[dict[str, Any], int]:
	entries = configuration.get("hooks", {}).get("PostToolUse")
	if not isinstance(entries, list):
		raise InstallError("registration_conflict")
	owned = []
	for group in entries:
		if not isinstance(group, dict) or group.get("matcher") != "apply_patch":
			continue
		handlers = group.get("hooks")
		if isinstance(handlers, list) and any(
			_is_post_edit_handler(handler) for handler in handlers
		):
			owned.append(group)
	if len(owned) != 1:
		raise InstallError("registration_conflict")
	return owned[0], entries.index(owned[0])


def _is_post_edit_handler(handler: object) -> bool:
	if not isinstance(handler, dict):
		return False
	command = " ".join(str(handler.get(key, "")) for key in ("command", "commandWindows"))
	if "python_quality" in command:
		return True
	windows_command = handler.get("commandWindows")
	return (
		"post_edit_checks.py" in str(handler.get("command", ""))
		and handler.get("statusMessage") == "Post-edit checks..."
		and handler.get("timeout") == POST_EDIT_TIMEOUT_SECONDS
		and isinstance(windows_command, str)
		and windows_command.casefold().startswith("powershell.exe ")
	)


def _same_group_except_timeout(current: object, expected: object) -> bool:
	if not isinstance(current, dict) or not isinstance(expected, dict):
		return False
	if current.keys() != expected.keys() or current.get("matcher") != expected.get("matcher"):
		return False
	current_handlers, expected_handlers = current.get("hooks"), expected.get("hooks")
	if not isinstance(current_handlers, list) or not isinstance(expected_handlers, list):
		return False
	if len(current_handlers) != len(expected_handlers):
		return False
	for current_handler, expected_handler in zip(current_handlers, expected_handlers, strict=True):
		if not isinstance(current_handler, dict) or not isinstance(expected_handler, dict):
			return False
		if current_handler.keys() != expected_handler.keys():
			return False
		if any(
			current_handler[key] != expected_handler[key]
			for key in current_handler.keys() - {"timeout"}
		):
			return False
		if type(current_handler.get("timeout")) is not int:
			return False
	return True


def _relocate_locked(root: Path, source: Path | None = None) -> dict[str, Any]:
	"""Point existing managed Codex registrations at this source checkout."""
	source = source_root(source)
	receipt = load_receipt(root)
	if receipt.get("state") not in {"prepared", "active"}:
		raise InstallError("installation_not_prepared")
	configuration = read_object(root / "hooks.json")
	old_groups = receipt.get("groups")
	if not isinstance(old_groups, dict):
		raise InstallError("invalid_configuration")
	new_groups = registration_groups(root, source)
	if old_groups.keys() != new_groups.keys():
		raise InstallError("invalid_configuration")
	for event, group in old_groups.items():
		entries = configuration.get("hooks", {}).get(event)
		if isinstance(entries, list) and entries.count(group) == 0:
			managed = [
				entry
				for entry in entries
				if isinstance(entry, dict)
				and entry.get("matcher") == group.get("matcher")
				and isinstance(entry.get("hooks"), list)
				and any(
					isinstance(handler, dict)
					and "quality_coordinator.py"
					in " ".join(str(handler.get(key, "")) for key in ("command", "commandWindows"))
					for handler in entry["hooks"]
				)
			]
			if len(managed) == 1 and _same_group_except_timeout(managed[0], group):
				old_groups[event] = managed[0]
				group = managed[0]
		_relocate_group(configuration, event, group, new_groups[event])

	from python_quality.install_post_edit_hook import group as post_edit_registration

	new_post_edit = post_edit_registration(root, source)
	old_post_edit, _ = _post_edit_group(configuration)
	_replace_group(configuration, "PostToolUse", old_post_edit, new_post_edit)

	if configuration != read_object(root / "hooks.json"):
		atomic_json(root / "hooks.json", configuration)
	receipt["groups"] = new_groups
	receipt["bundle"] = bundle_identity(source)
	receipt["source_root"] = str(source)
	receipt.pop("legacy_before", None)
	atomic_json(receipt_path(root), receipt)
	return {"state": receipt["state"], "source_root": str(source), "relocated": True}


def relocate(root: Path, source: Path | None = None) -> dict[str, Any]:
	with install_transaction(root):
		return _relocate_locked(root, source)


def remove_bindings(configuration: dict[str, Any], receipt: dict[str, Any]) -> None:
	created = receipt.get("created_events", [])
	if not isinstance(created, list) or any(
		not isinstance(event, str) or event not in receipt["groups"] for event in created
	):
		raise InstallError("invalid_configuration")
	for event, group in receipt["groups"].items():
		configuration["hooks"][event].remove(group)
	for event in created:
		if not configuration["hooks"][event]:
			del configuration["hooks"][event]


def _rollback_locked(root: Path) -> dict[str, Any]:
	receipt = load_receipt(root)
	if receipt.get("state") == "rolled_back":
		return {"state": "rolled_back"}
	configuration_path = root / "hooks.json"
	configuration = read_object(configuration_path)
	if not bindings_present(configuration, receipt["groups"]):
		raise InstallError("registration_conflict")
	settings_path = root / "hooks" / "_shared" / "python-quality.json"
	settings = load_settings(settings_path)["current"]
	from _shared.quality_background import cancel_all
	from _shared.quality_background_state import CLEANUP_SECONDS
	from _shared.quality_feedback import runtime_directory

	cancel_all(
		settings_path=settings_path,
		state_dir=runtime_directory() / "events",
		deadline=monotonic() + CLEANUP_SECONDS,
	)
	receipt["state"] = "prepared"
	atomic_json(receipt_path(root), receipt)
	settings_operation(settings_path, "update", {**settings, "enabled": []})
	remove_bindings(configuration, receipt)
	atomic_json(configuration_path, configuration)
	receipt["state"] = "rolled_back"
	atomic_json(receipt_path(root), receipt)
	return {"state": "rolled_back"}


def rollback(root: Path) -> dict[str, Any]:
	with install_transaction(root):
		return _rollback_locked(root)


def route_post_edit(root: Path) -> None:
	"""Keep legacy protection until the complete activation marker is committed."""
	try:
		receipt = load_receipt(root)
		if receipt.get("paused") is True:
			return
		active = receipt.get("state") == "active"
	except (OSError, ValueError):
		active = False
	if active:
		from _shared.quality_coordinator import main as coordinated_main

		coordinated_main(preserve_legacy_security=True)
		return
	from _shared.codex_hook_adapter import load_payload, run_post_edit

	run_post_edit(load_payload())


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--root", type=Path)
	parser.add_argument("--typing-probe-root", type=Path)
	parser.add_argument("action", choices=("prepare", "activate", "rollback", "status", "relocate"))
	parser.add_argument("--source-root", type=Path)
	arguments = parser.parse_args()
	try:
		root = Path(os.path.abspath(arguments.root or default_settings_path().parents[2]))
		source = source_root(arguments.source_root)
		if any(parent.is_symlink() or is_junction(parent) for parent in (root, *root.parents)):
			raise InstallError("unsafe_host_root")
		if arguments.action == "status":
			with install_transaction(root):
				result = {"state": load_receipt(root)["state"], "live_acceptance_required": True}
		elif arguments.action == "relocate":
			result = relocate(root, source)
		elif arguments.action == "prepare":
			result = prepare(root, source)
		elif arguments.action == "activate":
			result = activate(root, typing_probe_root=arguments.typing_probe_root, source=source)
		else:
			result = rollback(root)
	except (InstallError, SettingsError) as error:
		sys.stdout.write(json.dumps({"status": "unchecked", "reason": str(error)}) + "\n")
		return 2
	except (OSError, ValueError, KeyError, TypeError):
		sys.stdout.write(
			json.dumps({"status": "unchecked", "reason": "installation_unavailable"}) + "\n"
		)
		return 2
	sys.stdout.write(json.dumps(result) + "\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
