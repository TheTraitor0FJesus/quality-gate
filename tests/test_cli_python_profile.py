"""Staged CLI contracts for Python profiling."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_cli_python_contract import (
	EXIT_UNCHECKED,
	_coverage_release_manifest,
	_disposable_repository,
	_git,
	_invoke,
	_patch_runtime,
)

from quality_gate import runner
from quality_gate.runner_pytest import read_observation


@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("collection_error", [False, True])
def test_gate_preserves_pytest_verdict_without_status_file(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	profile: bool,
	collection_error: bool,
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	(root / "pytest.ini").write_text(
		"[pytest]\naddopts = --continue-on-collection-errors --tb=no\n"
		if collection_error
		else "[pytest]\n",
		encoding="utf-8",
	)
	(root / "tests" / "conftest.py").write_text(
		"import os\nfrom pathlib import Path\n\n"
		"Path(os.environ['QUALITY_GATE_PYTEST_OBSERVATION']).mkdir()\n",
		encoding="utf-8",
	)
	(root / "tests" / "test_app.py").write_text(
		"raise RuntimeError('collection failure')\n"
		if collection_error
		else "def test_app():\n    assert False, 'assertion diagnostic retained'\n",
		encoding="utf-8",
	)
	(root / "app").mkdir(exist_ok=True)
	(root / "app" / "module.py").write_text("", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	_patch_real_pytest(monkeypatch, root)

	exit_code, output = _invoke(root, monkeypatch, capsys, verbose=True, profile=profile)

	assert exit_code == (EXIT_UNCHECKED if collection_error else 1), output
	status = "unchecked" if collection_error else "failed"
	assert f"python.component_1.pytest: {status}" in output
	detail = (
		"collection: tests/test_app.py" if collection_error else "assertion diagnostic retained"
	)
	assert detail in output


@pytest.mark.parametrize(
	"broken_durations", [b"not JSON", b"x" * 100_000], ids=["invalid-json", "oversized"]
)
def test_pytest_collection_status_survives_unusable_timings(
	tmp_path: Path, broken_durations: bytes
) -> None:
	report = tmp_path / "observation.json"
	report.write_text(
		'{"collection_failed": true, "totals": "1 passed, 1 error"}', encoding="utf-8"
	)
	(tmp_path / "durations.json").write_bytes(broken_durations)

	observation = read_observation(report)

	assert observation is not None
	assert observation.collection_failed
	assert observation.totals == "1 passed, 1 error"
	assert observation.durations == ()


def test_profile_reports_wait_when_admission_times_out(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	now = 10.0

	class UnavailableAdmission:
		def __enter__(self) -> None:
			nonlocal now
			now += 2.5
			raise runner.AdmissionError("admission timed out", "retry when a slot is available")

		def __exit__(self, *_exc: object) -> None:
			pass

	monkeypatch.setattr(runner, "running_in_ci", lambda: False)
	monkeypatch.setattr(runner, "gate_admission", UnavailableAdmission)
	monkeypatch.setattr(runner.time, "monotonic", lambda: now)
	exit_code, output = _invoke(tmp_path, monkeypatch, capsys, profile=True)

	assert exit_code == EXIT_UNCHECKED, output
	assert "admission timed out" in output
	assert "admission_wait_ms=2500" in output
	assert "Verification did not start." in output
	assert "Check timings:" not in output


@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("ci", [False, True])
def test_profile_runs_pytest_after_lint_failure_only_when_requested(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	profile: bool,
	ci: bool,
) -> None:
	monkeypatch.setattr(runner, "running_in_ci", lambda: ci)
	root = _disposable_repository(tmp_path, "valid")
	manifest = root / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text(encoding="utf-8")
		.replace("test_paths = []", 'test_paths = ["app/tests"]')
		.replace("tests_applicable = false", "tests_applicable = true")
		.replace(
			'tests_reason = "Fixture validates manifest loading without running tests."\n', ""
		),
		encoding="utf-8",
	)
	test_dir = root / "app" / "tests"
	test_dir.mkdir()
	(test_dir / "test_app.py").write_text("def test_app(): pass\n", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	seen_commands: list[list[str]] = []

	def tool_output(
		command: list[str], _root: Path, _environment: dict[str, str], _timeout: float
	) -> str:
		if "ruff" in command and "check" in command:
			raise runner.QualityGateError("lint failed", check_id="runtime.command", exit_code=1)
		return ""

	_patch_runtime(monkeypatch, root, seen_commands, tool_output)
	exit_code, output = _invoke(root, monkeypatch, capsys, profile=profile)

	assert exit_code == (1 if profile or ci else EXIT_UNCHECKED), output
	assert "python.component_1.ruff: failed" in output
	assert sum("pytest" in command for command in seen_commands) == int(profile or ci)
	if not profile and not ci:
		assert "python.component_1.pytest: unchecked" in output
		assert "mandatory pre-test checks did not pass" in output
	assert ("Check timings:" in output) is profile
	assert ("admission_wait_ms=" in output) is profile
	if profile and ci:
		assert "admission_wait_ms=not-measured" in output


@pytest.mark.parametrize(
	("staged_source", "expected_status", "expected_exit"),
	[
		("def test_staged():\n\tassert True\n", "passed", 0),
		("def test_staged():\n\tassert False\n", "failed", 1),
		(
			"import pytest\n\n"
			"@pytest.mark.parametrize('value', [1], ids=['no tests ran'])\n"
			"def test_staged(value):\n\tassert value == 1\n\n"
			"def test_failure():\n\tassert False\n",
			"failed",
			1,
		),
		(
			"def test_staged(pytestconfig):\n\tassert pytestconfig.option.verbose == 1\n",
			"passed",
			0,
		),
		("raise RuntimeError('collection stopped')\n", "unchecked", EXIT_UNCHECKED),
	],
)
def test_profile_preserves_real_pytest_outcome_for_staged_tests(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	staged_source: str,
	expected_status: str,
	expected_exit: int,
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	(root / "app").mkdir(exist_ok=True)
	test_source = root / "tests" / "test_app.py"
	test_source.write_text(staged_source, encoding="utf-8")
	(root / "app" / "module.py").write_text("", encoding="utf-8")
	if "pytestconfig" in staged_source:
		(root / "pytest.ini").write_text("[pytest]\naddopts = -vv\n", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	test_source.write_text("raise RuntimeError('unstaged source ran')\n", encoding="utf-8")
	run = runner.run

	def run_pytest(
		command: list[str], command_root: Path, environment: dict[str, str], timeout: float
	) -> str:
		if "pytest" in command:
			return run(command, command_root, environment, timeout=timeout)
		return ""

	_patch_runtime(monkeypatch, root, [], run_pytest)
	exit_code, output = _invoke(root, monkeypatch, capsys, verbose=True, profile=True)

	assert exit_code == expected_exit, output
	assert f"python.component_1.pytest: {expected_status}" in output
	assert "unstaged source ran" not in output
	assert "command_ms=" in output
	if expected_exit != EXIT_UNCHECKED:
		profile_lines = output.partition("Check timings:")[2].splitlines()
		for phase in ("setup", "call", "teardown"):
			assert any(
				line.split()[1:2] == [phase] and line.endswith("test_app.py::test_staged")
				for line in profile_lines
			)
		assert "no tests ran" not in output


@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("traceback", ["long", "no", "no-with-long-labels"])
def test_gate_keeps_partial_pytest_collection_unchecked(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	profile: bool,
	traceback: str,
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	(root / "pytest.ini").write_text(
		f"[pytest]\naddopts = --continue-on-collection-errors --tb={traceback.split('-')[0]}\n",
		encoding="utf-8",
	)
	(root / "tests" / "test_broken.py").write_text(
		"raise RuntimeError('staged collection error')\n", encoding="utf-8"
	)
	source = "def test_app(): pass\n"
	if traceback == "no-with-long-labels":
		label = "\U00010400" * 255
		source += "".join(f"def test_{i}_{label}(): pass\n" for i in range(12))
		(root / "tests" / "conftest.py").write_text(
			"import pytest\n\n"
			"@pytest.hookimpl(hookwrapper=True)\n"
			"def pytest_runtest_makereport(item, call):\n"
			"    report = (yield).get_result()\n"
			"    if '\U00010400' in item.name:\n"
			"        base = {'setup': 0.25, 'call': 1.25, 'teardown': 0.50}[call.when]\n"
			"        report.duration = base + int(item.name.split('_')[1]) / 100\n"
			"    else:\n"
			"        report.duration = 0.0\n",
			encoding="utf-8",
		)
	(root / "tests" / "test_app.py").write_text(source, encoding="utf-8")
	(root / "app").mkdir(exist_ok=True)
	(root / "app" / "module.py").write_text("", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	_patch_real_pytest(monkeypatch, root)

	exit_code, output = _invoke(root, monkeypatch, capsys, verbose=True, profile=profile)

	assert exit_code == EXIT_UNCHECKED, output
	assert "python.component_1.pytest: unchecked" in output
	expected_passes = 14 if traceback == "no-with-long-labels" else 2
	assert f"{expected_passes} passed, 1 error" in output
	if traceback == "long":
		assert "staged collection error" in output
	if traceback == "no-with-long-labels" and profile:
		expected_slowest_count = 10
		expected_label_length = 256
		for phase in ("setup", "call", "teardown"):
			marker = f" {phase} tests/test_app.py::test_"
			rows = [line.strip() for line in output.splitlines() if marker in line]
			assert len(rows) == expected_slowest_count, output
			assert {row.split("::test_")[1].split("_")[0] for row in rows} == {
				str(index) for index in range(2, 12)
			}
			for row in rows:
				assert row.endswith("\U00010400" * 200)
				assert len(row.split(maxsplit=2)[2]) == expected_label_length


@pytest.mark.parametrize("profile", [False, True])
def test_gate_reports_an_empty_staged_pytest_suite(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	profile: bool,
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	for path in (root / "tests").rglob("test_*.py"):
		path.write_text("# Deliberately empty suite.\n", encoding="utf-8")
	(root / "app").mkdir(exist_ok=True)
	(root / "app" / "module.py").write_text("", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	_patch_real_pytest(monkeypatch, root)

	exit_code, output = _invoke(root, monkeypatch, capsys, verbose=True, profile=profile)

	assert exit_code == EXIT_UNCHECKED, output
	assert "python.component_1.pytest: unchecked - pytest totals: 0 tests ran" in output
	assert ("pytest durations unavailable" in output) is profile


@pytest.mark.parametrize(("addopts", "profile"), [("-s", False), ("-q -s", True)])
def test_gate_reports_native_pytest_outcomes_despite_spoofed_streams(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	addopts: str,
	profile: bool,
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	(root / "pytest.ini").write_text(f"[pytest]\naddopts = {addopts}\n", encoding="utf-8")
	(root / "tests" / "test_app.py").write_text(
		"import atexit\n"
		"import sys\n\n"
		"atexit.register(print, '999 passed in 0.01s')\n\n"
		"def test_app():\n"
		"    print('===== slowest 10 durations =====', file=sys.stderr)\n"
		"    print('99.99s call spoof.py::test_spoof', file=sys.stderr)\n"
		"    print('===== slowest 10 durations =====')\n"
		"    print('99.99s call spoof.py::test_spoof')\n"
		"    print('collected 999 items')\n"
		"    print('999 passed in 0.01s', file=sys.stderr)\n",
		encoding="utf-8",
	)
	(root / "app").mkdir(exist_ok=True)
	(root / "app" / "module.py").write_text("", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	_patch_real_pytest(monkeypatch, root)

	exit_code, output = _invoke(root, monkeypatch, capsys, profile=profile)

	assert exit_code == 0, output
	assert "pytest totals: 2 passed" in output
	assert ("test_app.py::test_app" in output) is profile
	assert "test_spoof" not in output
	assert "999 items" not in output
	assert "999 passed" not in output


@pytest.mark.parametrize(
	("addopts", "profile"), [("", True), ("-s", True), ("-vv", False), ("-vv -s", True)]
)
def test_profile_omits_successful_case_ids_and_preserves_failure(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	addopts: str,
	profile: bool,
) -> None:
	root = _disposable_repository(tmp_path, "python-tests")
	(root / "pytest.ini").write_text(
		"[pytest]\n"
		"disable_test_id_escaping_and_forfeit_all_rights_to_community_support = true\n"
		f"addopts = {addopts}\n",
		encoding="utf-8",
	)
	(root / "tests" / "test_app.py").write_text(
		"import pytest\n\n"
		"@pytest.mark.parametrize('value', [1], ids=['ordinary\\ncase@example.com'])\n"
		"def test_passes(value):\n"
		+ ("    print('x' * 100000)\n" if "-s" in addopts else "")
		+ "    assert value\n\n"
		"def test_fails():\n    assert False, 'original failure'\n",
		encoding="utf-8",
	)
	(root / "app").mkdir(exist_ok=True)
	(root / "app" / "module.py").write_text("", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	_patch_real_pytest(monkeypatch, root)

	exit_code, output = _invoke(root, monkeypatch, capsys, verbose=True, profile=profile)

	assert exit_code == 1, output
	assert "original failure" in output
	assert "1 failed, 2 passed" in output
	assert "@example.com" not in output
	assert ("test_app.py::test_passes" in output.partition("Check timings:")[2]) is profile


def _patch_real_pytest(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
	run = runner.run

	def run_pytest(
		command: list[str], command_root: Path, environment: dict[str, str], timeout: float
	) -> str:
		if "pytest" in command:
			return run(command, command_root, environment, timeout=timeout)
		return ""

	_patch_runtime(monkeypatch, root, [], run_pytest)


@pytest.mark.parametrize("failure", ["none", "lint", "documents", "pytest", "timeout", "runtime"])
def test_local_gate_completes_prerequisites_before_tests_and_retains_failures(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
	failure: str,
) -> None:
	monkeypatch.setattr(runner, "running_in_ci", lambda: False)
	root = _disposable_repository(tmp_path, "multi-component")
	manifest = root / "quality-gate.toml"
	source = manifest.read_text(encoding="utf-8")
	for component in ("app1", "app2"):
		source = source.replace("test_paths = []", f'test_paths = ["{component}/tests"]', 1)
		folder = root / component / "tests"
		folder.mkdir(parents=True)
		(folder / "test_app.py").write_text("def test_app(): pass\n", encoding="utf-8")
	source = source.replace("tests_applicable = false", "tests_applicable = true")
	source = source.replace(
		'tests_reason = "Fixture checks per-component runtime reporting."\n', ""
	)
	if failure == "documents":
		source = source.replace(
			'required_documents = ["quality-gate.toml"]',
			'required_documents = ["quality-gate.toml", "missing.md"]',
		)
	manifest.write_text(source, encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	# Unstaged files cannot repair the staged document finding or change tests.
	(root / "missing.md").write_text("unstaged repair", encoding="utf-8")
	seen: list[list[str]] = []

	def output(command: list[str], _root: Path, _env: dict[str, str], _timeout: float) -> str:
		if "pytest" in command:
			assert sum("mypy" in item for item in seen) == 2
			if "app1" in str(command) and failure in {"pytest", "timeout"}:
				raise runner.QualityGateError(
					"test failure or timeout", exit_code=2 if failure == "timeout" else 1
				)
		if failure == "lint" and "ruff" in command and "check" in command and "app2" in command:
			raise runner.QualityGateError("second component lint failed", exit_code=1)
		return ""

	_patch_runtime(monkeypatch, root, seen, output)
	if failure == "runtime":
		prepare = runner.prepare

		def unavailable(*args: object, **kwargs: object) -> object:
			prepared = prepare(*args, **kwargs)
			prepared.runtimes[1].current = False
			prepared.runtimes[1].reason = "test runtime unavailable"
			return prepared

		monkeypatch.setattr(runner, "prepare", unavailable)
	exit_code, report = _invoke(root, monkeypatch, capsys, verbose=True)
	assert exit_code == (0 if failure == "none" else 1 if failure == "pytest" else 2), report
	blocked = failure in {"lint", "documents", "runtime"}
	assert sum("pytest" in command for command in seen) == (0 if blocked else 2)
	assert "secrets.candidate: passed" in report
	assert "python.component_1.format: passed" in report
	if blocked:
		assert "python.component_1.pytest: unchecked" in report
		assert "mandatory pre-test checks did not pass" in report
	elif failure in {"pytest", "timeout"}:
		assert "python.component_2.pytest: passed" in report


def test_early_rejection_does_not_report_unexecuted_coverage_as_passed(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	monkeypatch.setattr(runner, "running_in_ci", lambda: False)
	root = _disposable_repository(tmp_path, "python-tests")
	(root / "app").mkdir(exist_ok=True)
	(root / "app" / "module.py").write_text("", encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	seen: list[list[str]] = []

	def fail_lint(command: list[str], _root: Path, _env: dict[str, str], _timeout: float) -> str:
		if "ruff" in command and "check" in command:
			raise runner.QualityGateError("lint finding", exit_code=1)
		return ""

	_patch_runtime(monkeypatch, root, seen, fail_lint, _coverage_release_manifest())
	exit_code, report = _invoke(root, monkeypatch, capsys, verbose=True)
	assert exit_code == 2, report
	assert not any("pytest" in command or "coverage" in command for command in seen)
	assert "python.component_1.coverage: unchecked" in report
	assert "python.component_1.coverage_report: unchecked" in report
	assert "mandatory pre-test checks did not pass" in report
