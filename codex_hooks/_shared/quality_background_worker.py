"""One-shot supervisor with an exclusive root lease and owned process cleanup."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from time import monotonic, sleep, time

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
	__package__ = "_shared"

from python_types.freshness import FreshnessError, cancellation_scope, check_deadline, digest
from python_types.results import envelope, unchecked, validate_result
from python_types.selections import release_inventory

from . import quality_background_state as state
from .quality_diagnostics import failure
from .quality_legacy_security import run_scanner
from .quality_process import ProcessError, execute
from .quality_settings import DEFAULT_BUDGET_SECONDS, load_settings
from .quality_storage import StorageBusyError, directory_transaction


def _failure_reason(error: Exception, fallback: str) -> str:
	return failure(error, "background_worker", fallback)["reason"]


def incomplete(error: Exception, fallback: str = "tool_output") -> dict:
	return {
		**unchecked(_failure_reason(error, fallback)),
		"diagnostic": {
			**failure(error, "background_worker", fallback)["diagnostic"],
			"phase": "background",
		},
	}


def work(directory: Path, event_id: str) -> None:
	path = directory / "latest.json"
	document = state.read(path)
	request = document["request"]
	if request["event_id"] != event_id or document["status"] != "pending":
		return
	deadline = monotonic() + max(0, request["deadline_unix_ms"] / 1000 - time())
	with state.reader_lease(
		directory, min(deadline, monotonic() + state.CLEANUP_SECONDS + 0.5)
	) as acquired:
		if not acquired:
			raise ValueError("background_busy")
		current = state.read(path)
		if current["request"]["event_id"] != event_id or current["status"] != "pending":
			return
		run(document, directory, deadline)


def run(document: dict, directory: Path, deadline: float) -> None:
	request = document["request"]
	cancel, finished = threading.Event(), threading.Event()

	def monitor() -> None:
		while not finished.wait(state.POLL_SECONDS):
			try:
				if (
					Path(request["cancel_path"]).exists()
					or load_settings(Path(document["settings_path"]))["current"]
					!= document["settings"]
				):
					cancel.set()
					return
			except (OSError, ValueError):
				cancel.set()
				return

	observer = threading.Thread(target=monitor)
	observer.start()
	wire = None
	engine_identity = None
	try:
		if Path(request["cancel_path"]).exists():
			cancel.set()
		with cancellation_scope(cancel):
			report, wire, engine_identity = analyze(document, deadline, cancel)
	except FreshnessError as error:
		report, wire = incomplete(error, "release"), None
	except (OSError, ValueError, KeyError) as error:
		report, wire = incomplete(error, "release"), None
	except ProcessError as error:
		report = incomplete(error)
	finally:
		finished.set()
		observer.join()
	if cancel.is_set():
		report, wire = unchecked("cancelled"), None
	persist_result(
		directory,
		request["event_id"],
		{
			"status": "completed",
			"wire": wire,
			"report": report if wire is None else None,
			"engine_identity": engine_identity,
		},
		deadline,
	)


def persist_result(directory: Path, event_id: str, outcome: dict, deadline: float) -> None:
	"""Contention is temporary; publication still shares the original work deadline."""
	while True:
		check_deadline(deadline)
		try:
			with directory_transaction(directory):
				check_deadline(deadline)
				latest = state.read(directory / "latest.json")
				if latest["request"]["event_id"] == event_id:
					latest.update(outcome)
					state.write(directory / "latest.json", latest)
				return
		except StorageBusyError:
			sleep(state.POLL_SECONDS)


def analyze(
	document: dict, deadline: float, cancel: threading.Event
) -> tuple[dict, dict | None, str]:
	request = document["request"]
	version = document["settings"]["provider_release"]
	engine_identity = digest(release_inventory(version, request, deadline))
	checks = document.get("checks", ["types"])
	results: dict[str, dict[str, object]] = {}
	files = [item for item in request["files"] if item["sha256"] is not None]
	if "ruff" in checks:
		results["ruff"] = _analyze_ruff(document, files, deadline, cancel)
		if cancel.is_set():
			return unchecked("cancelled"), None, engine_identity
	if "secrets" in checks:
		results.update(_analyze_secrets(document, files, deadline, cancel))
		if cancel.is_set():
			return unchecked("cancelled"), None, engine_identity
	if "types" in checks:
		results["types"] = _analyze_types(request, version, deadline, cancel)
		if cancel.is_set():
			return unchecked("cancelled"), None, engine_identity
	if "legacy_security" in checks:
		results["legacy_security"] = {
			"report": run_scanner(
				Path(request["root"]), files, deadline, cancel_event=cancel, heavy=True
			)
		}
		if cancel.is_set():
			return unchecked("cancelled"), None, engine_identity
	if digest(release_inventory(version, request, deadline)) != engine_identity:
		return unchecked("release"), None, engine_identity
	return (
		{"check": "background", "status": "completed"},
		{"schema": 1, "results": results},
		engine_identity,
	)


def _analyze_ruff(
	document: dict, files: list[dict], deadline: float, cancel: threading.Event
) -> dict[str, object]:
	try:
		from python_quality.check import check_files

		python_files = [
			item for item in files if Path(item["path"]).suffix.lower() in {".py", ".pyi"}
		]
		report = check_files(
			Path(document["request"]["root"]),
			python_files,
			document["settings"],
			deadline,
			phase="validate",
			cancel_event=cancel,
		)
	except (ImportError, OSError, TypeError, ValueError, KeyError, ProcessError) as error:
		report = {
			**_unchecked_check("ruff", _failure_reason(error, "tool_output")),
			"diagnostic": incomplete(error)["diagnostic"],
		}
	return {"report": report}


def _analyze_secrets(
	document: dict, files: list[dict], deadline: float, cancel: threading.Event
) -> dict[str, dict[str, object]]:
	root = Path(document["request"]["root"])
	settings = document["settings"]
	try:
		from secret_scan.check import check_files

		reader_deadline = min(
			deadline,
			monotonic() + settings.get("budget_seconds", DEFAULT_BUDGET_SECONDS),
		)
		secret_report = check_files(root, files, settings, reader_deadline, cancel_event=cancel)
	except (ImportError, OSError, TypeError, ValueError, KeyError, ProcessError) as error:
		secret_report = {
			**_unchecked_check("secrets", _failure_reason(error, "tool_output")),
			"diagnostic": incomplete(error)["diagnostic"],
		}
	try:
		builtin_report = run_scanner(root, files, deadline, builtin_only=True, cancel_event=cancel)
	except (OSError, TypeError, ValueError, KeyError, ProcessError):
		builtin_report = {
			"check": "builtin_secrets",
			"status": "unchecked",
			"reason": "security_unavailable",
			"files": [],
		}
	return {
		"secrets": {"report": secret_report},
		"builtin_secrets": {"report": builtin_report},
	}


def _analyze_types(
	request: dict, version: str, deadline: float, cancel: threading.Event
) -> dict[str, object]:
	try:
		process = execute(
			[
				sys.executable,
				"-B",
				str(Path(__file__).parents[1] / "python_types/provider_bridge.py"),
				"--release",
				version,
			],
			deadline=deadline,
			input_text=json.dumps(request),
			cancel_event=cancel,
		)
		report = (
			unchecked("tool_output")
			if process.stderr
			else validate_result(process.stdout, process.returncode, request, version, deadline)
		)
		wire = envelope(process.stdout, process.returncode, version)
	except ProcessError as error:
		report, wire = incomplete(error), None
	except (OSError, TypeError, ValueError, KeyError) as error:
		report, wire = incomplete(error), None
	return {
		"wire": wire if report["status"] != "unchecked" else None,
		"report": report if report["status"] == "unchecked" else None,
	}


def _unchecked_check(name: str, reason: str) -> dict[str, object]:
	if name == "ruff":
		from python_quality.check import unchecked as ruff_unchecked

		return ruff_unchecked(reason, phase="validate")
	if name == "secrets":
		from secret_scan.results import unchecked as secret_unchecked

		return secret_unchecked(reason)
	return unchecked(reason)


def main() -> int:
	directory, event_id = Path(sys.argv[1]), sys.argv[2]
	failure_exit = 0
	try:
		work(directory, event_id)
	except (OSError, ValueError, TypeError, KeyError) as error:
		try:
			with directory_transaction(directory):
				document = state.read(directory / "latest.json")
				if document["request"]["event_id"] == event_id:
					reason = _failure_reason(
						error, "io" if isinstance(error, OSError) else "invalid_background_state"
					)
					document.update(
						status="completed",
						wire=None,
						report=incomplete(error, reason),
						failure_errno=getattr(error, "errno", None),
						failure_winerror=getattr(error, "winerror", None),
					)
					state.write(directory / "latest.json", document)
		except (OSError, ValueError, TypeError, KeyError):
			# The pending record remains fail-closed; expose that recovery could not publish.
			failure_exit = 2
	try:
		state.cancellation_path(event_id).unlink(missing_ok=True)
	except (OSError, ValueError):
		return 2
	return failure_exit


if __name__ == "__main__":
	raise SystemExit(main())
