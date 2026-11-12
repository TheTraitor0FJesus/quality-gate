"""Resolve repository components, execution environments, and deadlines."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .admission import child_admission_environment
from .contracts import ValidationError, load_manifest
from .launcher import PreparedEnvironment
from .runner_errors import QualityGateError

_LOGGER = logging.getLogger(__name__)
EXIT_UNCHECKED = 2
RUFF_THREADS_ENV = "QUALITY_GATE_RUFF_THREADS"
DEFAULT_RUFF_THREADS = 4
MAX_RUFF_THREADS = 64


@dataclass(frozen=True)
class PythonComponent:
	path: Path
	tests: Path | None
	requirements: Path | None
	typecheck: bool
	test_paths: tuple[Path, ...] = ()
	timeout_seconds: int = 300
	missing_test_paths: tuple[Path, ...] = ()
	path_exists: bool = True
	dependency_inputs: tuple[Path, ...] = ()


def _manifest_error(error: ValidationError) -> QualityGateError:
	return QualityGateError(
		f"{error.path}: {error.message}",
		check_id="manifest.contract",
		exit_code=2,
		recovery_action=f"correct {error.path} and run validate",
	)


def repository_root(root: Path | None) -> Path:
	if root is not None:
		return root.resolve()
	result = subprocess.run(
		["git", "rev-parse", "--show-toplevel"],
		capture_output=True,
		text=True,
		encoding="utf-8",
		check=False,
	)
	if result.returncode:
		raise QualityGateError(
			"Run quality-gate inside a Git repository or pass --root.",
			check_id="runtime.repository",
			exit_code=2,
			recovery_action="run the command inside a Git repository or pass --root",
		)
	return Path(result.stdout.strip()).resolve()


def relative_path(root: Path, value: object, field: str) -> Path:
	if not isinstance(value, str) or not value:
		raise QualityGateError(
			f"{field} must be a non-empty relative path.",
			check_id="python.contract",
			exit_code=2,
			recovery_action=f"correct {field} and run validate",
		)
	resolved_root = root.resolve()
	path = (resolved_root / value).resolve()
	if resolved_root not in path.parents and path != resolved_root:
		raise QualityGateError(
			f"{field} must stay inside the repository.",
			check_id="python.contract",
			exit_code=2,
			recovery_action=f"correct {field} and run validate",
		)
	return path


def load_components(root: Path) -> list[PythonComponent]:
	try:
		manifest = load_manifest(root)
	except ValidationError as error:
		raise _manifest_error(error) from error
	components: list[PythonComponent] = []
	for index, item in enumerate(manifest.python, start=1):
		path = relative_path(root, item.path, f"python entry {index}.path")
		test_paths = tuple(
			relative_path(root, test_path, f"python entry {index}.test_paths[{test_index}]")
			for test_index, test_path in enumerate(item.test_paths)
		)
		missing_tests = [test_path for test_path in test_paths if not test_path.exists()]
		existing_tests = tuple(test_path for test_path in test_paths if test_path.exists())
		components.append(
			PythonComponent(
				path=path,
				tests=test_paths[0] if test_paths else None,
				requirements=None,
				typecheck=item.typecheck,
				dependency_inputs=tuple(
					relative_path(
						root,
						dependency_input,
						f"python entry {index}.dependency_inputs[{dependency_index}]",
					)
					for dependency_index, dependency_input in enumerate(item.dependency_inputs)
				),
				test_paths=existing_tests,
				timeout_seconds=item.timeout_seconds,
				missing_test_paths=tuple(missing_tests),
				path_exists=path.is_dir(),
			)
		)
	return components


def _remaining(deadline: float, timeout: int) -> float:
	remaining = min(float(timeout), deadline - time.monotonic())
	if remaining <= 0:
		raise QualityGateError(
			"quality gate exceeded its overall time budget",
			check_id="runtime.gate_timeout",
			exit_code=2,
			recovery_action="inspect slow checks and retry within the gate time budget",
		)
	return remaining


def _ci_history_refs(base: str | None, head: str | None) -> tuple[str | None, str]:
	"""Resolve pull-request commit SHAs from GitHub event data when available."""
	event_path = os.environ.get("GITHUB_EVENT_PATH")
	event_base: str | None = None
	event_head: str | None = None
	if event_path:
		try:
			event = json.loads(Path(event_path).read_text(encoding="utf-8"))
			pull_request = event.get("pull_request", {})
			base_data = pull_request.get("base", {})
			head_data = pull_request.get("head", {})
			if isinstance(base_data, dict) and isinstance(base_data.get("sha"), str):
				event_base = base_data["sha"]
			if isinstance(head_data, dict) and isinstance(head_data.get("sha"), str):
				event_head = head_data["sha"]
		except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
			_LOGGER.debug("GitHub event payload is unavailable", exc_info=True)
	return (
		base or event_base or os.environ.get("GITHUB_BASE_REF"),
		head or event_head or os.environ.get("GITHUB_SHA") or "HEAD",
	)


def _safe_environment(temporary_path: str) -> dict[str, str]:
	allowed = {
		"LANG",
		"LC_ALL",
		"PATHEXT",
		"PATH",
		"QUALITY_GATE_QUEUE_TIMEOUT_SECONDS",
		"QUALITY_GATE_RUFF_THREADS",
		"SYSTEMROOT",
		"WINDIR",
	}
	environment = {key: value for key, value in os.environ.items() if key in allowed}
	environment.update(
		{
			"HOME": temporary_path,
			"TMP": temporary_path,
			"TEMP": temporary_path,
			"PYTHONIOENCODING": "utf-8",
			"PYTHONUTF8": "1",
			"PYTHONDONTWRITEBYTECODE": "1",
			"RUFF_CACHE_DIR": temporary_path,
			"MYPY_CACHE_DIR": temporary_path,
			"COVERAGE_FILE": str(Path(temporary_path) / ".coverage"),
		}
	)
	if os.name == "nt":
		environment["USERPROFILE"] = temporary_path
	environment.update(child_admission_environment())
	return environment


def _ruff_environment(environment: dict[str, str]) -> dict[str, str]:
	raw = os.environ.get(RUFF_THREADS_ENV)
	if raw is None:
		threads = DEFAULT_RUFF_THREADS
	elif not re.fullmatch(r"[0-9]{1,2}", raw) or not 1 <= int(raw) <= MAX_RUFF_THREADS:
		raise QualityGateError(
			f"{RUFF_THREADS_ENV} must be an integer from 1 through {MAX_RUFF_THREADS}",
			check_id="runtime.ruff_threads",
			exit_code=EXIT_UNCHECKED,
			recovery_action=(
				f"set {RUFF_THREADS_ENV} to a whole number from 1 through {MAX_RUFF_THREADS}"
			),
		)
	else:
		threads = int(raw)
	return environment | {"RAYON_NUM_THREADS": str(threads)}


def _runtime_python(prepared: PreparedEnvironment, component_index: int) -> Path:
	try:
		inspection = prepared.runtimes[component_index - 1]
	except IndexError as error:
		raise QualityGateError(
			f"runtime is unavailable for Python component {component_index}",
			check_id=f"python.component_{component_index}.runtime",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run setup and retry the quality gate",
		) from error
	if not inspection.current or inspection.python is None:
		raise QualityGateError(
			f"runtime is unavailable for Python component {component_index}: "
			f"{inspection.reason or 'runtime is stale'}",
			check_id=f"python.component_{component_index}.runtime",
			exit_code=EXIT_UNCHECKED,
			recovery_action="run setup and retry the quality gate",
		)
	return inspection.python
