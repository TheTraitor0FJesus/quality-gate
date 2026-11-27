"""Explicit profiling retains local admission diagnostics."""

from pathlib import Path

import pytest
from test_cli_python_contract import EXIT_UNCHECKED, _invoke

from quality_gate import runner


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
