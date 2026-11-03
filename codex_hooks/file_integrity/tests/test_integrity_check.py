"""The host sends exact files and returns only the validated projection."""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path
from time import monotonic, time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_process import ProcessError

from file_integrity.check import check_files, main
from file_integrity.tests.test_integrity_results import (
	ACTION,
	CHECK_ID,
	ENGINE_RELEASE,
	POLICY_RELEASE,
	digest,
	provider_result,
)


@pytest.fixture(autouse=True)
def independent_project_root(tmp_path: Path) -> None:
	"""Keep the fixture independent when Gate locates temporary work inside a repo."""
	(tmp_path / ".git").mkdir()


def test_host_launches_exact_release_with_shared_deadline_and_current_hash(tmp_path: Path) -> None:
	content = b"clean content\n"
	path = tmp_path / "nested" / "merge.py"
	path.parent.mkdir(parents=True)
	path.write_bytes(content)
	files = [{"path": "nested/merge.py", "sha256": digest(content)}]
	settings = {
		"enabled": ["integrity"],
		"provider_release": ENGINE_RELEASE,
		"baseline_release": POLICY_RELEASE,
	}
	provider = provider_result(
		tmp_path,
		{
			"files": files,
			"baseline": {"policy_release": POLICY_RELEASE},
		},
	)
	process = SimpleNamespace(stdout=json.dumps(provider), stderr="", returncode=0)
	deadline = monotonic() + 5

	with patch("file_integrity.check.execute", return_value=process) as execute:
		result = check_files(tmp_path, files, settings, deadline)

	assert result == {"check": "integrity", "status": "passed", "findings": []}
	arguments = execute.call_args.args[0]
	assert arguments[-2:] == ["--release", ENGINE_RELEASE]
	request = json.loads(execute.call_args.kwargs["input_text"])
	assert request["files"] == files
	assert request["root"] == str(tmp_path.resolve())
	assert request["baseline"] == {"policy_release": POLICY_RELEASE}
	requested_deadline = request["deadline_unix_ms"] / 1000
	assert time() < requested_deadline <= time() + 5


def test_host_projects_only_validated_locations(tmp_path: Path) -> None:
	content = b"<<<<<<< HEAD\n"
	path = tmp_path / "nested" / "merge.py"
	path.parent.mkdir(parents=True)
	path.write_bytes(content)
	files = [{"path": "nested/merge.py", "sha256": digest(content)}]
	settings = {
		"enabled": ["integrity"],
		"provider_release": ENGINE_RELEASE,
		"baseline_release": POLICY_RELEASE,
	}
	finding = {
		"check_id": CHECK_ID,
		"path": "nested/merge.py",
		"line": 1,
		"status": "failed",
		"action": ACTION,
	}
	provider = provider_result(
		tmp_path,
		{
			"files": files,
			"baseline": {"policy_release": POLICY_RELEASE},
		},
		status="failed",
		findings=[finding],
	)
	process = SimpleNamespace(stdout=json.dumps(provider), stderr="", returncode=1)

	with patch("file_integrity.check.execute", return_value=process):
		result = check_files(tmp_path, files, settings, monotonic() + 5)

	assert result == {
		"check": "integrity",
		"status": "failed",
		"findings": [{"path": "nested/merge.py", "line": 1}],
	}
	assert "<<<<<<< HEAD" not in json.dumps(result)


@pytest.mark.parametrize("reason", ["manifest", "release", "stale_input", "deadline"])
def test_host_preserves_provider_reason_for_targeted_recovery(tmp_path: Path, reason: str) -> None:
	content = b"clean content\n"
	path = tmp_path / "merge.py"
	path.write_bytes(content)
	files = [{"path": "merge.py", "sha256": digest(content)}]
	settings = {
		"enabled": ["integrity"],
		"provider_release": ENGINE_RELEASE,
		"baseline_release": POLICY_RELEASE,
	}
	provider = provider_result(
		tmp_path,
		{"files": files, "baseline": {"policy_release": POLICY_RELEASE}},
	)
	provider.update(status="unchecked", reason=reason, files=[], selections=[])
	process = SimpleNamespace(stdout=json.dumps(provider), stderr="", returncode=2)

	with patch("file_integrity.check.execute", return_value=process):
		result = check_files(tmp_path, files, settings, monotonic() + 5)

	assert result == {
		"check": "integrity",
		"status": "unchecked",
		"findings": [],
		"reason": reason,
	}


def test_host_preserves_timeout_process_reason(tmp_path: Path) -> None:
	content = b"clean content\n"
	path = tmp_path / "merge.py"
	path.write_bytes(content)
	files = [{"path": "merge.py", "sha256": digest(content)}]
	settings = {
		"enabled": ["integrity"],
		"provider_release": ENGINE_RELEASE,
		"baseline_release": POLICY_RELEASE,
	}
	with patch("file_integrity.check.execute", side_effect=ProcessError("deadline")):
		result = check_files(tmp_path, files, settings, monotonic() + 5)
	assert result == {
		"check": "integrity",
		"status": "unchecked",
		"findings": [],
		"reason": "deadline",
	}


def test_host_json_entrypoint_returns_provider_status_and_exit(tmp_path: Path) -> None:
	content = b"<<<<<<< HEAD\n"
	path = tmp_path / "merge.py"
	path.write_bytes(content)
	files = [{"path": "merge.py", "sha256": digest(content)}]
	settings = {
		"enabled": ["integrity"],
		"provider_release": ENGINE_RELEASE,
		"baseline_release": POLICY_RELEASE,
	}
	provider = provider_result(
		tmp_path,
		{"files": files, "baseline": {"policy_release": POLICY_RELEASE}},
		status="failed",
		findings=[
			{
				"check_id": CHECK_ID,
				"path": "merge.py",
				"line": 1,
				"status": "failed",
				"action": ACTION,
			}
		],
	)
	process = SimpleNamespace(stdout=json.dumps(provider), stderr="", returncode=1)
	output = io.StringIO()
	payload = json.dumps({"schema": 1, "root": str(tmp_path), "files": files}).encode()
	settings_path = tmp_path / "python-quality.json"

	with (
		patch("_shared.quality_settings.default_settings_path", return_value=settings_path),
		patch("_shared.quality_settings.load_settings", return_value={"current": settings}),
		patch("file_integrity.check.execute", return_value=process),
		patch("sys.stdin", io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8")),
		redirect_stdout(output),
	):
		code = main()

	assert code == 1
	assert json.loads(output.getvalue()) == {
		"check": "integrity",
		"status": "failed",
		"findings": [{"path": "merge.py", "line": 1}],
	}
