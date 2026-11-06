"""Read-only machine-readable inspection of declared Python runtimes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from . import __version__
from .contract_models import Manifest, PythonComponent
from .contracts import ValidationError, load_manifest
from .distribution import DistributionError, PolicyCache, verify_release
from .runner_context import repository_root
from .runner_errors import QualityGateError
from .runtime import RuntimeManager, RuntimeUnavailable, runtime_identity


def _policy_record(cache: PolicyCache, version: str) -> dict[str, object]:
	"""Verify the exact cached policy without the quarantine side effect of select()."""
	if not version.startswith("v"):
		return {
			"release": version,
			"available": False,
			"reason_code": "invalid_release",
			"reason": "The manifest policy release is not a valid immutable release.",
		}
	try:
		manifest = verify_release(cache.releases / version)
	except DistributionError:
		return {
			"release": version,
			"available": False,
			"reason_code": "release_unavailable",
			"reason": "The exact policy release is missing or failed integrity verification.",
		}
	if manifest.version != version:
		return {
			"release": version,
			"available": False,
			"reason_code": "release_mismatch",
			"reason": "The cached policy release identity does not match the manifest.",
		}
	return {
		"release": version,
		"available": True,
		"reason_code": None,
		"reason": None,
	}


def _runtime_failure(reason: str) -> tuple[str, str]:
	if reason == "runtime is missing":
		return "runtime_missing", "The exact runtime for the current component inputs is missing."
	if reason == "runtime identity is stale":
		return "runtime_stale", "Runtime metadata does not match the current component inputs."
	if reason == "runtime metadata is corrupt":
		return "runtime_state_corrupt", "Runtime metadata is not valid JSON."
	if reason == "runtime state is unreadable":
		return "runtime_state_unreadable", "Runtime files cannot be read."
	return "runtime_unavailable", "The exact runtime could not be inspected."


def _component_record(
	root: Path,
	component: PythonComponent,
	manifest: Manifest,
	manager: RuntimeManager,
) -> tuple[dict[str, object], bool]:
	item = component
	base: dict[str, object] = {
		"name": item.name,
		"path": item.path,
		"python_version": item.python_version,
		"available": False,
		"python_executable": None,
		"reason_code": None,
		"reason": None,
	}
	try:
		identity = runtime_identity(root, manifest, item)
		inspection = manager.inspect(root, identity)
	except (OSError, RuntimeUnavailable):
		base["reason_code"] = "runtime_inputs_unavailable"
		base["reason"] = "One or more declared dependency inputs cannot be read."
		return base, False
	if not inspection.current or inspection.python is None:
		code, message = _runtime_failure(inspection.reason or "runtime is missing")
		base["reason_code"] = code
		base["reason"] = message
		return base, False
	try:
		probe = subprocess.run(
			[
				str(inspection.python),
				"-I",
				"-S",
				"-B",
				"-c",
				"import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
			],
			cwd=root,
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=10,
			check=False,
		)
	except (OSError, subprocess.TimeoutExpired):
		base["reason_code"] = "python_unavailable"
		base["reason"] = "The recorded Python executable cannot be started."
		return base, False
	if probe.returncode != 0:
		base["reason_code"] = "python_unavailable"
		base["reason"] = "The recorded Python executable cannot be started."
		return base, False
	if probe.stdout.strip() != item.python_version:
		base["reason_code"] = "python_version_mismatch"
		base["reason"] = "The runtime interpreter version does not match the manifest."
		return base, False
	base["available"] = True
	base["python_executable"] = str(inspection.python.resolve())
	return base, True


def _error_result(root: Path, code: str, message: str) -> dict[str, object]:
	return {
		"schema": 1,
		"engine_version": __version__,
		"root": str(root),
		"policy": None,
		"components": [],
		"error": {"reason_code": code, "reason": message},
	}


def runtimes(root: Path | None, cache_dir: Path | None) -> int:
	"""Emit one schema-1 inventory and return 0 only when all prerequisites are ready."""
	requested_root = Path.cwd().resolve()
	if root is not None:
		try:
			requested_root = root.expanduser().resolve()
		except (OSError, RuntimeError, ValueError):
			requested_root = root.expanduser().absolute()
			result = _error_result(
				requested_root,
				"root_not_found",
				"The requested repository root cannot be resolved.",
			)
			sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
			sys.stderr.write("runtimes: one or more declared prerequisites are unavailable\n")
			return 2
	try:
		actual_root = repository_root(requested_root if root is not None else None)
		if not actual_root.is_dir():
			result = _error_result(
				actual_root, "root_not_found", "The requested repository root does not exist."
			)
			ready = False
		else:
			manifest = load_manifest(actual_root)
			cache = PolicyCache(cache_dir)
			policy = _policy_record(cache, manifest.policy_release)
			manager = RuntimeManager(cache)
			components: list[dict[str, object]] = []
			all_available = bool(policy["available"])
			for component in manifest.python:
				record, available = _component_record(actual_root, component, manifest, manager)
				components.append(record)
				all_available &= available
			result = {
				"schema": 1,
				"engine_version": __version__,
				"root": str(actual_root),
				"policy": policy,
				"components": components,
				"error": None,
			}
			ready = all_available
	except ValidationError as error:
		result = _error_result(
			requested_root,
			"manifest_invalid",
			f"Manifest validation failed at {error.path}: {error.message}",
		)
		ready = False
	except QualityGateError:
		result = _error_result(
			requested_root,
			"repository_not_found",
			"Run from a Git repository or provide an existing --root path.",
		)
		ready = False
	except (OSError, DistributionError, RuntimeUnavailable, ValueError):
		result = _error_result(
			requested_root,
			"inspection_failed",
			"The repository runtime inventory could not be inspected.",
		)
		ready = False
	sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n")
	if not ready:
		sys.stderr.write("runtimes: one or more declared prerequisites are unavailable\n")
		return 2
	return 0
