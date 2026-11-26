"""Bounded observations from the Gate's existing pytest process."""

from __future__ import annotations

import heapq
import json
import math
import os
import re
import sys
from contextlib import suppress
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from traceback import format_exception, format_exception_only
from typing import TYPE_CHECKING

from .output_redaction import redact

if TYPE_CHECKING:
	from collections.abc import Generator
	from types import TracebackType
	from typing import Protocol
	from warnings import WarningMessage

	from pytest import CollectReport, Config, ExceptionInfo, Session, TestReport

	class _CommandOutcome(Protocol):
		@property
		def excinfo(
			self,
		) -> tuple[type[BaseException], BaseException, TracebackType | None] | None: ...


_PLUGIN_PACKAGE = "_quality_gate_pytest_observer"
_PLUGIN = f"{_PLUGIN_PACKAGE}.runner_pytest"
_REPORT_ENV = "QUALITY_GATE_PYTEST_OBSERVATION"
_PROFILE_ENV = "QUALITY_GATE_PYTEST_PROFILE"
_DURATION_LIMIT_PER_PHASE = 10
_LABEL_LIMIT = 256
_REPORT_LIMIT = 40_000
_DURATION_FIELDS = 3
_STATUS_LIMIT = 1024
_FAILURE_LIMIT = 4
_FAILURE_CHARS = 2_000
_TEARDOWN_CHARS = 1_000
_DIAGNOSTIC_LIMIT = _FAILURE_LIMIT * (_FAILURE_CHARS + _TEARDOWN_CHARS) * 4 + 100
_EXCEPTION_LIMIT = _FAILURE_CHARS * 4
_EXCEPTION_REPORT = "exception.txt"
_COLLECTION_FAILURE_REPORT = "collection-failed"
_WARNING_CHARS = 500
_WARNING_LIMIT = _FAILURE_LIMIT * (_WARNING_CHARS + 2) * 4
_PHASES = {"setup", "call", "teardown"}
_COUNT = re.compile(
	r"[0-9]{1,12} (?:failed|passed|errors?|skipped|xfailed|xpassed|deselected|warnings?)"
)
_TRACEBACK_FRAME = re.compile(
	r'^  File ".+", line [0-9]+(?:, in [^\n]+)?\n(?:[ \t]+[^\n]*\n)*',
	re.MULTILINE,
)

# Pytest imports this module and its redactor from a private package in the component runtime.
_collection_failed = False
_durations: dict[str, list[tuple[float, str, str]]] = {phase: [] for phase in _PHASES}
_failures: list[str] = []
_warnings: list[str] = []
_failed_calls: dict[tuple[str, str], int] = {}
_profile = os.environ.get(_PROFILE_ENV) == "1"
_traceback_style = "auto"
_show_capture = "all"
_worker = False


def pytest_configure(config: Config) -> None:
	global _show_capture, _traceback_style, _worker
	_show_capture = config.getoption("showcapture", default="all")
	_traceback_style = config.getoption("tbstyle", default="auto")
	_worker = hasattr(config, "workerinput")


def pytest_internalerror(excrepr: object) -> None:
	_write_exception(str(excrepr))


def pytest_keyboard_interrupt(excinfo: ExceptionInfo[BaseException]) -> None:
	_write_exception(str(excinfo.getrepr(style="line")))


def _observe_command_error() -> Generator[None, _CommandOutcome, None]:
	outcome = yield
	if outcome.excinfo is not None:
		error_type, error, traceback = outcome.excinfo
		detail = (
			str(error)
			if isinstance(error, _pytest.UsageError)
			else _exception_with_traceback(error_type, error, traceback)
		)
		_write_exception(detail)


def _exception_with_traceback(
	error_type: type[BaseException], error: BaseException, traceback: TracebackType | None
) -> str:
	detail = redact("".join(format_exception(error_type, error, traceback)))
	if len(detail) <= _FAILURE_CHARS:
		return detail
	# A long final exception line can push its opening message out of the retained traceback.
	message = _bounded_diagnostic(
		"".join(format_exception_only(error_type, error)), _FAILURE_CHARS // 3
	)
	return _bounded_diagnostic(detail, _FAILURE_CHARS - len(message) - 1) + "\n" + message


def _write_exception(detail: str) -> None:
	target = os.environ.get(_REPORT_ENV)
	if target is None or _worker:
		return
	with suppress(OSError):
		Path(target).with_name(_EXCEPTION_REPORT).write_bytes(
			_bounded_diagnostic(detail, _FAILURE_CHARS).encode("utf-8", "replace")
		)


def pytest_collectreport(report: CollectReport) -> None:
	global _collection_failed
	if report.failed:
		_collection_failed = True
		target = os.environ.get(_REPORT_ENV)
		if target is not None and not _worker:
			with suppress(OSError):
				Path(target).with_name(_COLLECTION_FAILURE_REPORT).write_bytes(b"1")
		_record_failure(report, "collection")


def pytest_warning_recorded(warning_message: WarningMessage) -> None:
	if len(_warnings) < _FAILURE_LIMIT:
		path = Path(warning_message.filename)
		root = Path.cwd()
		label = path.relative_to(root).as_posix() if path.is_relative_to(root) else path.name
		location = f"{label}:{warning_message.lineno}"
		_warnings.append(
			_bounded_diagnostic(f"{location}: {warning_message.message}", _WARNING_CHARS)
		)


def pytest_runtest_logreport(report: TestReport) -> None:
	if report.failed:
		if (
			report.when == "call"
			and _traceback_style not in {"no", "line"}
			and len(_failures) < _FAILURE_LIMIT
		):
			_failed_calls[_test_report_key(report)] = len(_failures)
		_record_failure(report, report.when)
	if report.when == "teardown":
		_record_teardown(report)
	if not _profile or report.when not in _PHASES:
		return
	row = (report.duration, report.when, _test_label(report.nodeid))
	phase_durations = _durations[report.when]
	if len(phase_durations) < _DURATION_LIMIT_PER_PHASE:
		heapq.heappush(phase_durations, row)
	elif row > phase_durations[0]:
		heapq.heapreplace(phase_durations, row)


def _test_label(node_id: str) -> str:
	path, separator, name = node_id.partition("::")
	label = redact(path + separator + name.partition("[")[0])
	return "".join(char if char.isprintable() else " " for char in label[:_LABEL_LIMIT])


def _record_failure(report: CollectReport | TestReport, phase: str) -> None:
	if len(_failures) >= _FAILURE_LIMIT:
		return
	label = f"{phase}: {_test_label(report.nodeid)}"
	if _traceback_style == "no":
		_failures.append(label)
		return
	capture = _captured_output(report)
	remaining = _FAILURE_CHARS - len(label) - 1
	if capture:
		capture = "\n" + _bounded_diagnostic(capture, remaining // 2 - 1)
	traceback = _bounded_diagnostic(report.longreprtext, remaining - len(capture))
	_failures.append(f"{label}\n{traceback}{capture}")


def _test_report_key(report: TestReport) -> tuple[str, str]:
	return report.nodeid, getattr(report, "worker_id", "")


def _record_teardown(report: TestReport) -> None:
	index = _failed_calls.pop(_test_report_key(report), None)
	if index is None or report.failed:
		return
	capture = _captured_output(report, teardown_only=True)
	if capture:
		_failures[index] += "\n" + _bounded_diagnostic(capture, _TEARDOWN_CHARS - 1)


def _captured_output(report: CollectReport | TestReport, *, teardown_only: bool = False) -> str:
	if _show_capture == "no":
		return ""
	return "\n".join(
		f"{heading}:\n{content}"
		for heading, content in report.sections
		if (not teardown_only or heading.endswith(" teardown"))
		and (_show_capture == "all" or heading.startswith(f"Captured {_show_capture} "))
	)


def _bounded_diagnostic(detail: str, limit: int) -> str:
	detail = redact(detail)
	if len(detail) <= limit:
		return detail
	marker = "\n[pytest detail truncated]\n"
	available = limit - len(marker)
	prefix = available // 2
	return detail[:prefix] + marker + detail[-(available - prefix) :]


def _session_totals(session: Session) -> str | None:
	terminal = session.config.pluginmanager.get_plugin("terminalreporter")
	build_parts = getattr(terminal, "build_summary_stats_line", None)
	if not callable(build_parts):
		return None
	parts, _ = build_parts()
	counts = [part for part, _ in parts if _COUNT.fullmatch(part)]
	if counts:
		return ", ".join(counts)
	return "0 tests ran" if any(part == "no tests ran" for part, _ in parts) else None


def _observe_session_finish(session: Session) -> Generator[None, _CommandOutcome, None]:
	outcome = yield
	_write_details("failures.txt", _failures)
	_write_details("warnings.txt", _warnings)
	if outcome.excinfo is not None:
		return
	# Observe totals and warnings after pytest's terminal summary hooks have run.
	target = os.environ.get(_REPORT_ENV)
	if target is None or hasattr(session.config, "workerinput"):
		return
	report = Path(target)
	# Optional details have independent bounds and cannot invalidate the collection status.
	_write_json(
		report,
		{"collection_failed": _collection_failed, "totals": _session_totals(session)},
		_STATUS_LIMIT,
	)
	if _profile:
		rows = [row for phase_durations in _durations.values() for row in phase_durations]
		_write_json(report.with_name("durations.json"), rows, _REPORT_LIMIT)


if __name__ == _PLUGIN:
	# This copy is loaded as a plugin by the component's already-running pytest.
	_pytest = sys.modules["pytest"]
	pytest_load_initial_conftests = _pytest.hookimpl(hookwrapper=True, tryfirst=True)(
		_observe_command_error
	)
	pytest_cmdline_main = _pytest.hookimpl(hookwrapper=True, tryfirst=True)(_observe_command_error)
	pytest_sessionfinish = _pytest.hookimpl(hookwrapper=True, tryfirst=True)(
		_observe_session_finish
	)


def _write_details(name: str, details: list[str]) -> None:
	target = os.environ.get(_REPORT_ENV)
	if details and target is not None and not _worker:
		with suppress(OSError):
			Path(target).with_name(name).write_bytes(
				"\n\n".join(details).encode("utf-8", "replace")
			)


def _write_json(path: Path, data: object, limit: int) -> None:
	payload = json.dumps(data, ensure_ascii=False).encode("utf-8", "replace")
	if len(payload) <= limit:
		with suppress(OSError):
			path.write_bytes(payload)


@dataclass(frozen=True, slots=True)
class PytestObservation:
	collection_failed: bool
	totals: str | None
	durations: tuple[str, ...]
	diagnostics: str


def prepare_observation(
	directory: Path, environment: dict[str, str], *, profile: bool
) -> tuple[Path, dict[str, str], tuple[str, str]]:
	package = directory / _PLUGIN_PACKAGE
	package.mkdir()
	(package / "__init__.py").write_bytes(b"")
	resources = files("quality_gate")
	for name in ("runner_pytest.py", "output_redaction.py"):
		(package / name).write_bytes(resources.joinpath(name).read_bytes())
	report = directory / "observation.json"
	import_path = environment.get("PYTHONPATH")
	return (
		report,
		{
			**environment,
			"PYTHONPATH": os.pathsep.join((str(directory), import_path))
			if import_path
			else str(directory),
			_REPORT_ENV: str(report),
			_PROFILE_ENV: "1" if profile else "0",
		},
		("-p", _PLUGIN),
	)


def _read_file(path: Path, limit: int) -> bytes | None:
	try:
		if path.is_symlink() or not path.is_file():
			return None
		with path.open("rb") as stream:
			payload = stream.read(limit + 1)
		return payload if len(payload) <= limit else None
	except OSError:
		return None


def _read_json(path: Path, limit: int) -> object:
	payload = _read_file(path, limit)
	if payload is None:
		return None
	try:
		data = json.loads(payload)
	except (ValueError, RecursionError):
		return None
	return data


def read_observation(report: Path) -> PytestObservation | None:
	data = _read_json(report, _STATUS_LIMIT)
	if not isinstance(data, dict) or type(data.get("collection_failed")) is not bool:
		return None
	totals = data.get("totals")
	if not isinstance(totals, str) or (
		totals != "0 tests ran" and not all(_COUNT.fullmatch(part) for part in totals.split(", "))
	):
		totals = None
	return PytestObservation(
		data["collection_failed"],
		totals,
		_read_durations(report.with_name("durations.json")),
		read_failure_details(report),
	)


def read_collection_failure(report: Path) -> bool:
	return _read_file(report.with_name(_COLLECTION_FAILURE_REPORT), 1) == b"1"


def read_failure_details(report: Path) -> str:
	payload = _read_file(report.with_name("failures.txt"), _DIAGNOSTIC_LIMIT) or b""
	return payload.decode("utf-8", "replace")


def read_warning_details(report: Path) -> str:
	payload = _read_file(report.with_name("warnings.txt"), _WARNING_LIMIT) or b""
	return payload.decode("utf-8", "replace")


def read_exception(report: Path) -> str:
	payload = _read_file(report.with_name(_EXCEPTION_REPORT), _EXCEPTION_LIMIT) or b""
	return payload.decode("utf-8", "replace")


def startup_exception(stderr: str) -> str:
	"""Keep the final exception block from Python's pre-observer traceback."""
	text = stderr.replace("\r\n", "\n").rstrip()
	# Complete tail lines may survive even if the traceback header was clipped.
	frames = tuple(_TRACEBACK_FRAME.finditer(text))
	if not frames:
		return ""
	detail = text[frames[-1].end() :]
	return detail if re.match(r"[\w.]+(?::|\n|$)", detail) else ""


def _read_durations(path: Path) -> tuple[str, ...]:
	rows = _read_json(path, _REPORT_LIMIT)
	if not isinstance(rows, list) or len(rows) > _DURATION_LIMIT_PER_PHASE * len(_PHASES):
		return ()
	durations: list[tuple[float, str, str]] = []
	counts = dict.fromkeys(_PHASES, 0)
	for row in rows:
		if not isinstance(row, list) or len(row) != _DURATION_FIELDS:
			return ()
		seconds, phase, label = row
		if (
			type(seconds) is not float
			or not math.isfinite(seconds)
			or seconds < 0
			or not isinstance(phase, str)
			or phase not in _PHASES
			or not isinstance(label, str)
			or len(label) > _LABEL_LIMIT
			or not label.isprintable()
		):
			return ()
		counts[phase] += 1
		if counts[phase] > _DURATION_LIMIT_PER_PHASE:
			return ()
		durations.append((seconds, phase, label))
	return tuple(
		f"{seconds:.2f}s {phase} {label}"
		for seconds, phase, label in sorted(durations, reverse=True)
	)
