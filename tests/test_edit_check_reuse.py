"""Working-tree edit-check contract tests."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest
from test_edit_check import (
	EXIT_UNCHECKED,
	FRESH_PROCESSES,
	REUSED_PROCESSES,
	digest,
	invoke,
	prepare_edit_cache,
	request,
)

from quality_gate.distribution import PolicyCache
from quality_gate.distribution import verify_release as verify_release_cache
from quality_gate.edit_policy import ruff_artifact, tool_path
from quality_gate.edit_protocol import SUPPORTED_POLICIES as EDIT_SUPPORTED_POLICIES

pytest_plugins = ("test_edit_check",)


@pytest.mark.parametrize("module_path", ["zebra.py", "zebra.pyi", "namespace/zebra.py"])
def test_import_layout_change_invalidates_reuse(
	tmp_path: Path, edit_cache: Path, module_path: str
) -> None:
	module_name = module_path.removesuffix(".pyi").removesuffix(".py").replace("/", ".")
	path = tmp_path / "example.py"
	path.write_bytes(
		f"import {module_name}\nimport alpha\n\nvalue = ({module_name}, alpha)\n".encode()
	)
	first_exit, first = invoke(request(tmp_path, edit_cache, "example.py"))
	assert first_exit == 0, first
	warm_exit, warm = invoke(request(tmp_path, edit_cache, "example.py"))
	assert warm_exit == 0 and warm["process_count"] == REUSED_PROCESSES, warm
	module = tmp_path / module_path
	module.parent.mkdir(parents=True, exist_ok=True)
	module.write_bytes(b"# Local module appeared after the previous checks.\n")
	changed_exit, changed = invoke(request(tmp_path, edit_cache, "example.py"))
	assert changed_exit == 0, changed
	assert changed["process_count"] == FRESH_PROCESSES
	assert path.read_bytes() == (
		f"import alpha\n\nimport {module_name}\n\nvalue = ({module_name}, alpha)\n".encode()
	)
	assert changed["files"][0]["sha256"] == digest(path.read_bytes())
	assert changed["files"][0]["findings"] == []
	assert module.read_bytes() == b"# Local module appeared after the previous checks.\n"


@pytest.mark.parametrize("from_wheel", [False, True])
def test_nested_package_import_order_matches_full_tree(
	tmp_path: Path, engine_wheel: Path, from_wheel: bool
) -> None:
	cache = prepare_edit_cache(tmp_path / "cache")
	root = tmp_path / "project"
	package = root / "hooks" / "_shared"
	package.mkdir(parents=True)
	marker = package / "__init__.py"
	marker.write_bytes(b"# Preserve this unrelated package marker.\n")
	path = package / "example.py"
	path.write_bytes(
		b"from _shared.helper import value\nfrom python_types.helper import other\n"
		b"\nresult = (value, other)\n"
	)
	engine = engine_wheel if from_wheel else None
	wheel_hash = digest(engine_wheel.read_bytes())
	exit_code, result = invoke(
		request(root, cache, "hooks/_shared/example.py"), engine_wheel=engine
	)
	assert exit_code == 0, result
	assert path.read_bytes() == (
		b"from python_types.helper import other\n\nfrom _shared.helper import value\n"
		b"\nresult = (value, other)\n"
	)
	assert marker.read_bytes() == b"# Preserve this unrelated package marker.\n"
	release = cache / "releases/v2.4.2"
	artifact = ruff_artifact(release, verify_release_cache(release))
	ordinary = subprocess.run(
		[
			str(tool_path(PolicyCache(cache), artifact.sha256)),
			"check",
			"--no-cache",
			"--config",
			str(release / "quality_gate/policy/ruff.toml"),
			"--output-format",
			"json",
			str(path),
		],
		cwd=root,
		capture_output=True,
		check=False,
		timeout=10,
	)
	assert ordinary.returncode == 0, ordinary.stdout + ordinary.stderr
	assert json.loads(ordinary.stdout) == result["files"][0]["findings"] == []
	_, warm = invoke(request(root, cache, "hooks/_shared/example.py"), engine_wheel=engine)
	assert warm["process_count"] == REUSED_PROCESSES
	marker.unlink()
	exit_code, changed = invoke(
		request(root, cache, "hooks/_shared/example.py"), engine_wheel=engine
	)
	assert exit_code == 0, changed
	assert changed["process_count"] == FRESH_PROCESSES
	assert path.read_bytes() == (
		b"from _shared.helper import value\nfrom python_types.helper import other\n"
		b"\nresult = (value, other)\n"
	)
	assert digest(engine_wheel.read_bytes()) == wheel_hash


def test_policy_selection_change_requires_fresh_ruff(tmp_path: Path, wheel_cache: Path) -> None:
	(tmp_path / "example.py").write_bytes(b"import os\nvalue = os.name\n")
	for policy in sorted(EDIT_SUPPORTED_POLICIES):
		for expected_processes in (FRESH_PROCESSES, REUSED_PROCESSES):
			payload = request(tmp_path, wheel_cache, "example.py")
			payload["baseline"]["policy_release"] = policy
			exit_code, result = invoke(payload)
			assert exit_code == 0, result
			assert result["process_count"] == expected_processes
			assert result["selections"][0]["policy_release"] == policy


@pytest.mark.parametrize(
	"content",
	[
		b"'unterminated\n",
		b"".join(f"variable_{index} = {index}\n".encode() for index in range(260)),
	],
)
def test_unbounded_or_unlexable_import_context_runs_ruff(
	tmp_path: Path, edit_cache: Path, content: bytes
) -> None:
	(tmp_path / "example.py").write_bytes(content)
	for _ in range(2):
		exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
		assert exit_code in {0, 1}, result
		assert result["process_count"] == FRESH_PROCESSES


@pytest.mark.parametrize("change", ["file", "ruff.toml", "pyproject.toml", ".gitignore", "waiver"])
def test_working_tree_changes_invalidate_reuse(
	tmp_path: Path, edit_cache: Path, change: str
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"import os\n\nvalue = os.name\n")
	manifest = tmp_path / "quality-gate.toml"
	manifest.write_text(
		'[quality]\nschema = 2\npolicy_release = "v2.4.2"\n'
		'[repository]\nname = "reuse"\ndomains = ["repository"]\n'
		'required_documents = ["quality-gate.toml"]\n',
		encoding="utf-8",
	)
	exit_code, original = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0, original
	if change == "file":
		path.write_bytes(b"import os\n\nvalue=  2\n")
	elif change == "waiver":
		with manifest.open("a", encoding="utf-8") as stream:
			stream.write(
				'[[waivers]]\nkind = "standard"\ncheck_id = "repository.git.tracked_junk"\n'
				'target = "other.py"\nreason = "Controlled reuse invalidation fixture."\n'
				'approved_by = "fixture"\nreviewed_on = "2026-10-18"\nexpires_on = "2027-10-18"\n'
			)
	else:
		(tmp_path / change).write_bytes(b"# Working-tree configuration changed.\n")
	exit_code, changed = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 0, changed
	assert changed["process_count"] == FRESH_PROCESSES
	assert changed["files"][0]["findings"] == []
	assert changed["files"][0]["sha256"] == digest(path.read_bytes())
	if change != "file":
		assert changed["inputs"] != original["inputs"]


@pytest.mark.parametrize(
	"failure", ["record", "missing", "truncated", "oversized", "key", "no_key", "locked"]
)
def test_corrupt_reuse_cannot_supply_a_clean_verdict(
	tmp_path: Path, edit_cache: Path, failure: str
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"value = missing_name\n")
	exit_code, first = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 1, first
	cache = tmp_path.parent / f"{tmp_path.name}-user-cache" / "quality-gate/edit-results-v1"
	record = next(cache.glob("*.json"))
	if failure == "record":
		forged = json.loads(record.read_bytes())
		forged["payload"]["result"]["status"] = "passed"
		forged["payload"]["result"]["files"][0].update(status="passed", findings=[])
		record.write_text(json.dumps(forged), encoding="utf-8")
	elif failure == "missing":
		record.unlink()
	elif failure == "truncated":
		record.write_bytes(b'{"payload":')
	elif failure == "oversized":
		record.write_bytes(b"x" * (300 * 1024))
	elif failure == "key":
		(cache / "key").write_bytes(b"changed key")
	elif failure == "locked":
		(cache / ".lock").write_bytes(b"held by another invocation\n")
	else:
		(cache / "key").unlink()
	exit_code, checked = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 1, checked
	assert checked["process_count"] == FRESH_PROCESSES
	assert {finding["rule"] for finding in checked["files"][0]["findings"]} == {"F821"}
	assert checked["files"][0]["sha256"] == digest(path.read_bytes())
	assert path.read_bytes() == b"value = missing_name\n"


def test_invalid_request_is_unchecked_and_does_not_echo_input() -> None:
	exit_code, result = invoke(b'{"secret":"synthetic-sensitive-value",')
	assert exit_code == EXIT_UNCHECKED
	assert result["status"] == "unchecked"
	assert result["reason"] == "invalid_request"
	assert "synthetic-sensitive-value" not in json.dumps(result)


def test_expired_invocation_is_unchecked_before_file_selection(tmp_path: Path) -> None:
	exit_code, result = invoke(
		{
			"schema": 1,
			"root": str(tmp_path),
			"files": [],
			"deadline_unix_ms": int(time.time() * 1000) - 1,
		}
	)
	assert exit_code == EXIT_UNCHECKED
	assert result["reason"] == "deadline"
	assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
	("field", "value"),
	[
		("schema", True),
		("extra", 1),
		("deadline_unix_ms", 10**1000),
		("deadline_unix_ms", True),
		("cache_dir", None),
	],
)
def test_invalid_envelope_is_source_free(tmp_path: Path, field: str, value: object) -> None:
	payload: dict[str, Any] = {
		"schema": 1,
		"root": str(tmp_path),
		"files": [],
		"deadline_unix_ms": int(time.time() * 1000) + 5000,
	}
	payload[field] = value
	exit_code, result = invoke(payload)
	assert (exit_code, result["reason"]) == (2, "invalid_request")
	assert list(tmp_path.iterdir()) == []


def test_write_phase_removes_safe_f401_unused_import_from_module(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	path.write_bytes(b"import os\nvalue=1\n")
	payload = request(tmp_path, edit_cache, "example.py")
	payload["phase"] = "write"

	exit_code, result = invoke(payload)

	assert exit_code == 0, result
	assert result["status"] == "passed"
	assert path.read_bytes() == b"value = 1\n"


def test_write_phase_preserves_explicit_public_reexport_in_init(
	tmp_path: Path, edit_cache: Path
) -> None:
	package = tmp_path / "sample_package"
	package.mkdir()
	path = package / "__init__.py"
	original = b"from .api import public_name as public_name\n"
	path.write_bytes(original)
	(package / "api.py").write_bytes(b"public_name = 1\n")
	payload = request(tmp_path, edit_cache, "sample_package/__init__.py")
	payload["phase"] = "write"

	exit_code, result = invoke(payload)

	assert exit_code == 0, result
	assert result["status"] == "passed"
	assert path.read_bytes() == original


def test_safe_fixes_remove_unused_import_and_report_residual_lint(
	tmp_path: Path, edit_cache: Path
) -> None:
	path = tmp_path / "example.py"
	path.write_text("import sys\nimport os\n\nvalue= missing_name\n", encoding="utf-8")
	unrelated = tmp_path / "unrelated.py"
	unrelated.write_bytes(b"other=   2\n")
	exit_code, result = invoke(request(tmp_path, edit_cache, "example.py"))
	assert exit_code == 1, (result.get("reason"), result.get("stage"))
	assert result["status"] == "failed"
	assert "phase" not in result
	file = result["files"][0]
	assert {finding["rule"] for finding in file["findings"]} == {"F821"}
	assert file["format"] == "passed"
	assert file["coverage"] == "baseline"
	assert file["sha256"] == digest(path.read_bytes())
	assert path.read_text(encoding="utf-8") == "value = missing_name\n"
	assert unrelated.read_bytes() == b"other=   2\n"
	path.write_text("import os\nimport sys\n\nvalue = (os.name, sys.version)\n", encoding="utf-8")
	assert invoke(request(tmp_path, edit_cache, "example.py"))[0] == 0
