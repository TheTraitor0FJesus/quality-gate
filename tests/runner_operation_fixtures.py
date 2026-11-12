"""Shared fixtures for operation-aware runner tests."""

import os
from contextlib import nullcontext

import pytest

from quality_gate import native_cache as native_cache_module, runner


@pytest.fixture(autouse=True)
def isolate_runner_unit_tests_from_host_gate_slots(monkeypatch: pytest.MonkeyPatch) -> None:
	# Admission behavior is tested explicitly below and in test_native_gate_queue.py.
	monkeypatch.setattr(runner, "gate_admission", lambda: nullcontext())

@pytest.fixture(autouse=True)
def use_long_windows_staging_limit_for_gate_temp_workspace(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	if os.name == "nt":
		monkeypatch.setattr(native_cache_module, "_WINDOWS_MAX_PATH", 32767)
