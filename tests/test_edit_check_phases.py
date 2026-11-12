"""Working-tree edit-check contract tests."""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import pytest
from test_edit_check import (
	EXIT_UNCHECKED,
	EXPECTED_SELECTIONS,
	REUSED_TWO_SELECTION_PROCESSES,
	assert_project_files_after_cleanup,
	digest,
	invoke,
	managed,
	request,
)

from quality_gate.contracts import load_manifest
from quality_gate.edit_check import Invocation
from quality_gate.edit_check import execute as execute_edit_check
from quality_gate.edit_protocol import SUPPORTED_POLICIES as EDIT_SUPPORTED_POLICIES
from quality_gate.edit_protocol import EditError, decode_request
from quality_gate.runtime import RuntimeManager, runtime_identity

pytest_plugins = ("test_edit_check",)


def test_read_only_validation_reports_final_ruff_findings_without_writing(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	original = b"import sys\nimport os\n\nvalue=  1\n"
	path.write_bytes(original)
	original_mtime = path.stat().st_mtime_ns
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "validate"
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 20_000

	exit_code, result = invoke(payload)

	assert exit_code == 1, result
	assert result["phase"] == "validate"
	assert result["status"] == "failed"
	assert result["writes_may_have_occurred"] is False
	file = result["files"][0]
	assert file["input_sha256"] == digest(original)
	assert file["sha256"] == digest(original)
	assert file["changed"] is False
	assert {finding["rule"] for finding in file["findings"]} >= {"F401", "I001"}
	assert file["format"] == "failed"
	assert path.read_bytes() == original
	assert path.stat().st_mtime_ns == original_mtime
	assert_project_files_after_cleanup(tmp_path, "example.py")


@pytest.mark.parametrize("policy", sorted(EDIT_SUPPORTED_POLICIES))
def test_read_only_validation_supports_every_policy_pin(
	tmp_path: Path, wheel_cache: Path, policy: str
) -> None:
	path = tmp_path / "example.py"
	original = b"value = 1\n"
	path.write_bytes(original)
	original_mtime = path.stat().st_mtime_ns
	payload = request(tmp_path, wheel_cache, "example.py")
	payload["baseline"]["policy_release"] = policy
	payload["phase"] = "validate"
	payload["deadline_unix_ms"] = int(time.time() * 1000) + 25_000

	exit_code, result = invoke(payload)

	assert exit_code == 0, result
	assert result["phase"] == "validate"
	assert result["status"] == "passed"
	assert result["writes_may_have_occurred"] is False
	assert result["selections"][0]["policy_release"] == policy
	file = result["files"][0]
	assert file["input_sha256"] == digest(original)
	assert file["sha256"] == digest(original)
	assert file["changed"] is False
	assert path.read_bytes() == original
	assert path.stat().st_mtime_ns == original_mtime
	assert_project_files_after_cleanup(tmp_path, "example.py")


@pytest.mark.parametrize("changed_input", ["source", "configuration"])
def test_read_only_validation_rejects_inputs_changed_during_checks(
	tmp_path: Path,
	tmp_path_factory: pytest.TempPathFactory,
	edit_cache: Path,
	monkeypatch: pytest.MonkeyPatch,
	changed_input: str,
) -> None:
	path = tmp_path / "example.py"
	original = b"value = 1\n"
	path.write_bytes(original)
	configuration = tmp_path / "ruff.toml"
	if changed_input == "configuration":
		configuration.write_bytes(b"# captured before Ruff starts\n")
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "validate"
	original_run = Invocation.run
	changed = False

	def run_then_change(
		self: Invocation, command: list[str], *, cwd: Path
	) -> tuple[int, bytes, bytes]:
		nonlocal changed
		result = original_run(self, command, cwd=cwd)
		if command[1] == "format" and "--check" in command and not changed:
			changed = True
			if changed_input == "source":
				path.write_bytes(b"value = 2\n")
			else:
				configuration.write_bytes(b"# changed during Ruff\n")
		return result

	monkeypatch.setattr(Invocation, "run", run_then_change)
	with tempfile.TemporaryDirectory(
		prefix="quality-gate-validate-test-", dir=tmp_path_factory.getbasetemp()
	) as directory:
		with pytest.raises(EditError, match="stale_input"):
			execute_edit_check(payload, time.monotonic() + 25, Path(directory))

	assert changed
	assert path.read_bytes() == (b"value = 2\n" if changed_input == "source" else original)
	assert {item.name for item in tmp_path.iterdir()} == (
		{"example.py", "ruff.toml"} if changed_input == "configuration" else {"example.py"}
	)


def test_write_phase_applies_only_safe_fixes_and_formatting(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	original = b"import sys\nimport os\n\nvalue= (os.name, sys.version)\n"
	path.write_bytes(original)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "write"

	exit_code, result = invoke(payload)

	assert exit_code == 0, result
	assert result["phase"] == "write"
	assert result["status"] == "passed"
	assert result["writes_may_have_occurred"] is True
	file = result["files"][0]
	assert file["input_sha256"] == digest(original)
	assert file["sha256"] == digest(path.read_bytes())
	assert file["changed"] is True
	assert file["findings"] == []
	assert file["format"] == "passed"
	assert path.read_bytes() == b"import os\nimport sys\n\nvalue = (os.name, sys.version)\n"
	assert "F401" not in json.dumps(result)
	assert_project_files_after_cleanup(tmp_path, "example.py")


def test_read_only_validation_keeps_unchecked_prerequisites_incomplete(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	original = b"value = 1\n"
	path.write_bytes(original)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "validate"
	payload["baseline"]["policy_release"] = "v99.0.0"

	exit_code, result = invoke(payload)

	assert exit_code == EXIT_UNCHECKED
	assert result["status"] == "unchecked"
	assert result["phase"] == "validate"
	assert result["writes_may_have_occurred"] is False
	assert path.read_bytes() == original
	assert_project_files_after_cleanup(tmp_path, "example.py")


def test_unknown_edit_phase_is_rejected_without_falling_back_to_writer(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	original = b"value=  1\n"
	path.write_bytes(original)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "rewrite"

	exit_code, result = invoke(payload)

	assert exit_code == EXIT_UNCHECKED
	assert result["status"] == "unchecked"
	assert result["reason"] == "invalid_request"
	assert path.read_bytes() == original
	assert {item.name for item in tmp_path.iterdir()} == {"example.py"}


@pytest.mark.parametrize("reader", ["secrets", "types", "integrity"])
def test_ruff_phase_is_rejected_by_other_edit_readers(tmp_path: Path, reader: str) -> None:
	payload = {
		"schema": 1,
		"root": str(tmp_path),
		"files": [],
		"deadline_unix_ms": int(time.time() * 1000) + 5000,
		"phase": "validate",
	}

	with pytest.raises(EditError, match="invalid_request"):
		decode_request(json.dumps(payload).encode(), **{reader: True})


def test_preexisting_rules_production_test_suppression_and_redaction(
	tmp_path: Path, edit_cache: Path
) -> None:
	(tmp_path / "tests").mkdir()
	content = b'password = "synthetic-sensitive-value"\nassert password\n'
	(tmp_path / "production.py").write_bytes(content)
	(tmp_path / "tests/test_example.py").write_bytes(content)
	(tmp_path / "suppressed.py").write_bytes(b"import os  # noqa: F401\n")
	exit_code, result = invoke(
		request(tmp_path, edit_cache, "production.py", "tests/test_example.py", "suppressed.py")
	)
	assert exit_code == 1, (result.get("reason"), result.get("stage"))
	assert {finding["rule"] for finding in result["files"][0]["findings"]} == {"S101", "S105"}
	assert result["files"][1]["status"] == "passed"
	assert result["files"][2]["status"] == "passed"
	assert "synthetic-sensitive-value" not in json.dumps(result)


def test_stale_input_never_rewrites_any_file(tmp_path: Path, edit_cache: Path) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"value=  1\n")
	payload = request(tmp_path, edit_cache, "example.py")
	path.write_bytes(b"value=  2\n")
	exit_code, result = invoke(payload)
	assert exit_code == EXIT_UNCHECKED
	assert result["reason"] == "stale_input"
	assert path.read_bytes() == b"value=  2\n"


def test_managed_old_pin_and_outside_component_baseline_are_distinct(
	tmp_path: Path, edit_cache: Path
) -> None:
	managed(tmp_path, edit_cache)
	(tmp_path / "outside.py").write_bytes(b"other=  2\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py", "outside.py"))
	assert exit_code == 0, result
	assert [file["coverage"] for file in result["files"]] == ["managed", "baseline"]
	assert {selection["policy_release"] for selection in result["selections"]} == {"v2.4.2"}
	assert len(result["selections"]) == EXPECTED_SELECTIONS
	assert result["files"][0]["component"] == "app"
	assert (tmp_path / "outside.py").read_bytes() == b"other = 2\n"
	exit_code, reused = invoke(request(tmp_path, edit_cache, "src/example.py", "outside.py"))
	assert exit_code == 0 and reused["process_count"] == REUSED_TWO_SELECTION_PROCESSES, reused
	manifest = load_manifest(tmp_path)
	manager = RuntimeManager(edit_cache)
	metadata = (
		manager.path_for(tmp_path, runtime_identity(tmp_path, manifest, manifest.python[0]))
		/ "runtime.json"
	)
	original_metadata = metadata.read_bytes()
	try:
		metadata.write_bytes(b"{}")
		exit_code, invalid = invoke(request(tmp_path, edit_cache, "src/example.py", "outside.py"))
		assert (exit_code, invalid["reason"]) == (2, "runtime"), invalid
	finally:
		metadata.write_bytes(original_metadata)
	(tmp_path / "requirements.txt").write_bytes(b"changed-dependency==1\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py"))
	assert (exit_code, result["reason"]) == (2, "runtime")


@pytest.mark.parametrize("failure", ["invalid", "unsupported", "runtime"])
def test_manifest_failures_never_fall_back_to_baseline(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	path = managed(
		tmp_path,
		edit_cache,
		version="v2.3.0" if failure == "unsupported" else "v2.4.2",
		setup=False,
	)
	if failure == "invalid":
		(tmp_path / "quality-gate.toml").write_text(
			'[secret = "synthetic-sensitive-value"', encoding="utf-8"
		)
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py"))
	assert (exit_code, result["reason"]) == (
		2,
		{"invalid": "manifest", "unsupported": "unsupported_policy", "runtime": "runtime"}[failure],
	)
	assert path.read_bytes() == b"value=  1\n"
	assert "synthetic-sensitive-value" not in json.dumps(result)


def test_unrelated_unprepared_component_does_not_block_managed_edit(
	tmp_path: Path, edit_cache: Path
) -> None:
	managed(tmp_path, edit_cache)
	manifest = tmp_path / "quality-gate.toml"
	with manifest.open("a", encoding="utf-8") as stream:
		stream.write("""
[[python]]
name = "unprepared"
path = "other"
python_version = "3.11"
dependency_inputs = []
test_paths = []
tests_applicable = false
tests_reason = "Unrelated component is deliberately not prepared."
""")
	(tmp_path / "other").mkdir()
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py"))
	assert exit_code == 0, result
	assert result["files"][0]["coverage"] == "managed"
	assert len(result["selections"]) == 1
