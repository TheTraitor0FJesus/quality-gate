"""Related behavior tests."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest
import test_edit_types as edit_types_support
from test_edit_types import (
	STANDARD_MANAGED_RUNTIME_PTH,
	_candidate_typing_cache_parent,
	digest,
	invoke,
	request,
)
from typing_test_support import full_component_diagnostics

from quality_gate.path_safety import is_link_or_reparse

pytest_plugins = ("test_edit_types",)


@pytest.mark.skipif(os.name != "nt", reason="Windows candidate temp parent selection only")
def test_candidate_typing_cache_parent_falls_back_from_reparse_temp_parent(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	policy_cache = tmp_path / "policy-cache"
	policy_cache.mkdir()
	typing_policy = policy_cache / "releases/v4.6.0"
	temp_parent = tmp_path / "short"
	temp_directory = temp_parent / "temp"
	temp_directory.mkdir(parents=True)
	assert len(os.fspath(temp_parent)) < len(os.fspath(policy_cache))
	monkeypatch.setattr(tempfile, "gettempdir", lambda: os.fspath(temp_directory))
	reparse_identity = temp_parent.lstat()
	original_is_link_or_reparse = is_link_or_reparse

	def mark_temp_parent_as_reparse(metadata: os.stat_result) -> bool:
		if (metadata.st_dev, metadata.st_ino) == (
			reparse_identity.st_dev,
			reparse_identity.st_ino,
		):
			return True
		return original_is_link_or_reparse(metadata)

	monkeypatch.setattr(edit_types_support, "is_link_or_reparse", mark_temp_parent_as_reparse)

	assert _candidate_typing_cache_parent(typing_policy) == policy_cache


def test_empty_typing_input_is_explicitly_inapplicable(tmp_path: Path, typing_policy: Path) -> None:
	code, result = invoke(request(tmp_path, typing_policy))
	assert (code, result["status"]) == (0, "not_applicable"), result
	assert result["components"] == []
	assert result["writes_may_have_occurred"] is False


def test_background_cancellation_reaps_active_public_worker(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	payload = request(tmp_path, typing_policy, "source.py", background=True)
	signal = Path(payload["cancel_path"])
	created_parent = not signal.parent.exists()
	signal.parent.mkdir(exist_ok=True)
	stop = threading.Event()
	signalled = threading.Event()

	def cancel() -> None:
		while not stop.wait(0.01):
			if any(tmp_path.glob("temp/qg-*/source/source.py")):
				signal.write_bytes(b"")
				signalled.set()
				return

	thread = threading.Thread(target=cancel)
	thread.start()
	try:
		code, result = invoke(payload)
		assert signalled.is_set(), "Cancellation must reach an active worker."
		assert (code, result["status"], result["reason"]) == (2, "unchecked", "cancelled"), result
		assert result["event_id"] == payload["event_id"]
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		stop.set()
		thread.join(timeout=3)
		signal.unlink(missing_ok=True)
		if created_parent and not any(signal.parent.iterdir()):
			signal.parent.rmdir()
	assert result["writes_may_have_occurred"] is False


def test_background_envelope_binds_event_and_rejects_unowned_cancel_path(
	tmp_path: Path, typing_policy: Path
) -> None:
	payload = request(tmp_path, typing_policy, background=True)
	code, result = invoke(payload)
	assert (code, result["schema"], result["status"]) == (0, 2, "not_applicable"), result
	assert result["mode"] == "background"
	assert result["event_id"] == payload["event_id"]
	payload["cancel_path"] = str(tmp_path / "unowned.cancel")
	code, result = invoke(payload)
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "unsafe_path"), result


def test_expired_valid_background_request_keeps_event_identity(
	tmp_path: Path, typing_policy: Path
) -> None:
	payload = request(tmp_path, typing_policy, background=True)
	payload["deadline_unix_ms"] = int(time.time() * 1000) - 1
	code, result = invoke(payload)
	assert (code, result["schema"], result["status"], result["reason"]) == (
		2,
		2,
		"unchecked",
		"deadline",
	), result
	assert result["event_id"] == payload["event_id"]
	payload["unexpected"] = True
	code, result = invoke(payload)
	assert (code, result["schema"], result["reason"]) == (2, 1, "invalid_request"), result
	assert "event_id" not in result


def test_existing_background_signal_cancels_before_runtime_inspection(
	tmp_path: Path, typing_policy: Path
) -> None:
	(tmp_path / "source.py").write_text("value: int = 1\n", encoding="utf-8")
	payload = request(tmp_path, typing_policy, "source.py", background=True)
	signal = Path(payload["cancel_path"])
	created_parent = not signal.parent.exists()
	signal.parent.mkdir(exist_ok=True)
	signal.write_bytes(b"")
	try:
		code, result = invoke(payload)
		assert (code, result["schema"], result["status"], result["reason"]) == (
			2,
			2,
			"unchecked",
			"cancelled",
		), result
		assert result["event_id"] == payload["event_id"]
		assert not list(tmp_path.glob("temp/qg-*"))
	finally:
		signal.unlink(missing_ok=True)
		if created_parent and not any(signal.parent.iterdir()):
			signal.parent.rmdir()


def test_changed_signature_reports_unchanged_consumer_with_real_mypy(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "api.py").write_text(
		"def accept(value: str) -> str:\n    return value\n", encoding="utf-8"
	)
	(tmp_path / "consumer.py").write_text(
		"from api import accept\nresult = accept(123)\n", encoding="utf-8"
	)
	code, result = invoke(request(tmp_path, typing_policy, "api.py", background=True))
	assert (code, result["status"]) == (1, "failed"), json.dumps(result, indent=2)
	component = result["components"][0]
	assert component["diagnostics"] == [
		{"path": "consumer.py", "line": 2, "column": 17, "code": "arg-type"}
	]
	assert {item["path"] for item in component["inventory"]} == {"api.py", "consumer.py"}
	assert result["selections"][0]["policy_release"] == typing_policy.name
	assert result["selections"][0]["mypy_version"] == "2.3.1"
	assert component["diagnostics"] == full_component_diagnostics(tmp_path, typing_policy, ".")
	assert not list(tmp_path.glob("temp/qg-*"))


def test_typing_does_not_require_unrelated_released_pytest_dependencies(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	for path in site.glob("pluggy*"):
		if path.is_dir():
			shutil.rmtree(path)
		else:
			path.unlink()
	(tmp_path / "source.py").write_text("value: int = 1\n", encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"]) == (0, "passed"), result


def test_standard_managed_runtime_pth_reaches_component_inventory_and_mypy(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	site = (
		tmp_path
		/ ".venv"
		/ (
			"Lib/site-packages"
			if os.name == "nt"
			else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
		)
	)
	for name, content in STANDARD_MANAGED_RUNTIME_PTH.items():
		(site / name).write_bytes(content)
	source = b"value: int = 1\n"
	(tmp_path / "source.py").write_bytes(source)

	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))

	assert (code, result["status"]) == (0, "passed"), result
	assert result["components"][0]["inventory"] == [{"path": "source.py", "sha256": digest(source)}]
	assert result["selections"][0]["mypy_version"] == "2.3.1"
	assert not list(tmp_path.glob("temp/qg-*"))


def test_released_typing_extensions_context_is_supported_without_project_execution(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_text(
		"import email.message\nimport os.path\nimport importlib.metadata\n"
		"from typing_extensions import TypeAlias\nValue: TypeAlias = int\n"
		"raise RuntimeError('must never execute')\n",
		encoding="utf-8",
	)
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"]) == (0, "passed"), result


def test_deleted_module_and_missing_stub_context_are_unchecked(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "consumer.py").write_text(
		"from removed import Value\nvalue: Value\n", encoding="utf-8"
	)
	code, result = invoke(request(tmp_path, typing_policy, "removed.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result
	(tmp_path / "consumer.py").write_text(
		"from .removed import Value\nvalue: Value\n", encoding="utf-8"
	)
	code, result = invoke(request(tmp_path, typing_policy, "removed.py", background=True))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "dependencies"), result


def test_missing_environment_never_uses_global_python(tmp_path: Path, typing_policy: Path) -> None:
	(tmp_path / "example.py").write_text("value: int = 1\n", encoding="utf-8")
	code, result = invoke(request(tmp_path, typing_policy, "example.py"))
	assert (code, result["status"], result["reason"]) == (2, "unchecked", "runtime"), result


def test_unmanaged_dependency_evidence_is_path_ordered(
	tmp_path: Path, typing_policy: Path, typing_environment: Path
) -> None:
	shutil.copytree(typing_environment, tmp_path / ".venv", symlinks=True)
	(tmp_path / "source.py").write_bytes(b"value: int = 1\n")
	(tmp_path / "pyproject.toml").write_bytes(b"[project]\nname = 'fixture'\n")
	(tmp_path / ".mypy.ini").write_bytes(b"[mypy]\n")
	code, result = invoke(request(tmp_path, typing_policy, "source.py", background=True))
	assert (code, result["status"]) == (0, "passed"), result
	assert result["components"][0]["dependency_inputs"] == [
		{"path": ".mypy.ini", "sha256": digest(b"[mypy]\n")},
		{"path": "pyproject.toml", "sha256": digest(b"[project]\nname = 'fixture'\n")},
	]
