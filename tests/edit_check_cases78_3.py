"""Edit-check public contract tests, partition 3."""

from __future__ import annotations

from edit_check_support78 import (
	EXIT_UNCHECKED as EXIT_UNCHECKED,
	EXPECTED_SELECTIONS as EXPECTED_SELECTIONS,
	EditError as EditError,
	Path as Path,
	REUSED_TWO_SELECTION_PROCESSES as REUSED_TWO_SELECTION_PROCESSES,
	RuntimeManager as RuntimeManager,
	assert_project_files_after_cleanup as assert_project_files_after_cleanup,
	decode_request as decode_request,
	digest as digest,
	invoke as invoke,
	json as json,
	load_manifest as load_manifest,
	managed as managed,
	os as os,
	prepare_edit_cache as prepare_edit_cache,
	pytest as pytest,
	request as request,
	runtime_identity as runtime_identity,
	runtime_wrapper as runtime_wrapper,
	subprocess as subprocess,
	sys as sys,
	time as time,
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


def test_version_probe_never_runs_installed_startup_hooks(tmp_path: Path, edit_cache: Path) -> None:
	managed(tmp_path, edit_cache)
	manifest = load_manifest(tmp_path)
	manager = RuntimeManager(edit_cache)
	runtime = manager.path_for(tmp_path, runtime_identity(tmp_path, manifest, manifest.python[0]))
	site = runtime / (
		"Lib/site-packages"
		if os.name == "nt"
		else f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
	)
	marker = tmp_path / "startup-executed.txt"
	(site / "edit_probe_side_effect.pth").write_text(
		f"import pathlib; pathlib.Path({str(marker)!r}).write_bytes(b'executed')\n",
		encoding="utf-8",
	)
	exit_code, result = invoke(request(tmp_path, edit_cache, "src/example.py"))
	assert exit_code == 0, result
	assert not marker.exists(), "Version probing must not execute installed project startup hooks"


@pytest.mark.skipif(os.name == "nt", reason="POSIX Python executable symlink layout")
def test_baseline_interpreter_symlink_is_supported(tmp_path: Path, edit_cache: Path) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	python = tmp_path / "python-link"
	python.symlink_to(sys.executable)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(python)
	exit_code, result = invoke(payload)
	assert exit_code == 0, result
	assert result["selections"][0]["python"] == str(python)
	assert result["selections"][0]["python_sha256"] == digest(Path(sys.executable).read_bytes())


def test_interpreter_change_during_probe_invalidates_result(
	tmp_path: Path, edit_cache: Path
) -> None:
	(tmp_path / "example.py").write_bytes(b"value=  1\n")
	python = tmp_path / ("runtime.cmd" if os.name == "nt" else "runtime")
	response = json.dumps(
		{"version": f"{sys.version_info.major}.{sys.version_info.minor}", "executable": str(python)}
	)
	runtime_wrapper(
		tmp_path,
		f"from pathlib import Path\nPath({str(python)!r}).write_bytes(b'changed\\n')\n"
		f"print({response!r})\n",
	)
	payload = request(tmp_path, edit_cache, "example.py")
	payload["baseline"]["python"] = str(python)
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, "stale_input"), result
	assert (tmp_path / "example.py").read_bytes() == b"value=  1\n"


def test_working_tree_is_checked_without_reading_or_rewriting_index(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	staged = b"value = 1\n"
	path.write_bytes(staged)
	for arguments in (["init"], ["add", "example.py"]):
		process = subprocess.run(
			["git", "-C", str(tmp_path), *arguments], capture_output=True, check=False
		)
		assert process.returncode == 0
	index_before = (tmp_path / ".git/index").read_bytes()
	path.write_bytes(b"import os\nvalue=  2\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0, result
	assert result["files"][0]["findings"] == []
	assert path.read_bytes() == b"value = 2\n"
	assert (tmp_path / ".git/index").read_bytes() == index_before
	process = subprocess.run(
		["git", "-C", str(tmp_path), "show", ":example.py"], capture_output=True, check=False
	)
	assert process.returncode == 0
	assert process.stdout == staged


def test_empty_non_python_duplicate_and_policy_excluded_inputs(
	tmp_path: Path, edit_cache: Path
) -> None:
	assert invoke(request(tmp_path, edit_cache))[1]["status"] == "not_applicable"
	(tmp_path / "text.txt").write_bytes(b"ordinary text\n")
	assert invoke(request(tmp_path, edit_cache, "text.txt"))[1]["status"] == "not_applicable"
	(tmp_path / ".venv").mkdir()
	path = tmp_path / ".venv/example.py"
	path.write_bytes(b"value=  1\n")
	exit_code, result = invoke(
		request(tmp_path, edit_cache, ".venv/example.py", ".venv/example.py")
	)
	assert exit_code == 0
	assert result["status"] == "not_applicable"
	assert len(result["files"]) == 1
	assert path.read_bytes() == b"value=  1\n"


@pytest.mark.parametrize(
	"failure", ["escape", "directory", "deleted", "file_size", "count", "configuration", "findings"]
)
def test_bounded_input_failures_never_publish(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"value=  1\n")
	payload = request(tmp_path, edit_cache, "example.py")
	expected = "limits"
	if failure == "escape":
		payload["files"] = [{"path": "../escape.py", "sha256": "0" * 64}]
		expected = "unsafe_path"
	elif failure == "directory":
		payload["files"] = [{"path": "directory", "sha256": "0" * 64}]
		(tmp_path / "directory").mkdir()
		expected = "unsafe_path"
	elif failure == "deleted":
		path.unlink()
		expected = "io"
	elif failure == "file_size":
		path.write_bytes(b"#" * (128 * 1024 + 1))
		payload = request(tmp_path, edit_cache, "example.py")
	elif failure == "count":
		payload["files"] *= 11
	elif failure == "configuration":
		(tmp_path / "pyproject.toml").write_bytes(b"#" * (256 * 1024 + 1))
	elif failure == "findings":
		path.write_bytes(b"assert True\n" * 201)
		payload = request(tmp_path, edit_cache, "example.py")
	before = path.read_bytes() if path.exists() else None
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, expected), result
	assert (path.read_bytes() if path.exists() else None) == before


def test_syntax_error_is_redacted_and_does_not_prevent_other_file_formatting(
	tmp_path: Path, edit_cache: Path
) -> None:
	(tmp_path / "broken.py").write_bytes(b'value = "synthetic-sensitive-value\n')
	(tmp_path / "valid.py").write_bytes(b"value=  1\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "broken.py", "valid.py"))
	assert exit_code == 1, (result.get("reason"), result.get("stage"))
	assert result["files"][0]["status"] == "failed"
	assert result["files"][1]["format"] == "passed"
	assert (tmp_path / "valid.py").read_bytes() == b"value = 1\n"
	assert "synthetic-sensitive-value" not in json.dumps(result)
