"""Provider response validation is atomic and rechecks every file identity."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from time import monotonic
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from file_integrity.results import ACTION, CHECK_ID, validate_result

ENGINE_RELEASE = "v3.2.0"
POLICY_RELEASE = "v3.1.0"


def digest(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


def request(root: Path, content: bytes) -> dict[str, Any]:
	(root / ".git").mkdir(exist_ok=True)
	path = root / "nested" / "merge.py"
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_bytes(content)
	return {
		"schema": 1,
		"root": str(root.resolve()),
		"files": [{"path": "nested/merge.py", "sha256": digest(content)}],
		"deadline_unix_ms": 9_999_999_999_999,
		"baseline": {"policy_release": POLICY_RELEASE},
	}


def provider_result(
	root: Path,
	payload: dict[str, Any],
	*,
	status: str = "passed",
	findings: list[dict[str, Any]] | None = None,
	manifest: bytes | None = None,
) -> dict[str, Any]:
	entry = payload["files"][0]
	coverage = "managed" if manifest is not None else "baseline"
	files = [
		{
			"path": entry["path"],
			"status": status,
			"coverage": coverage,
			"input_sha256": entry["sha256"],
			"sha256": entry["sha256"],
			"changed": False,
			"selection": 0,
			"findings": findings or [],
		}
	]
	return {
		"schema": 1,
		"engine_version": ENGINE_RELEASE.removeprefix("v"),
		"root": str(root.resolve()),
		"check_id": CHECK_ID,
		"status": status,
		"reason": None,
		"findings": findings or [],
		"files": files,
		"selections": [
			{
				"policy_release": POLICY_RELEASE,
				"release_sha256": digest(b"release"),
				"waiver_sha256": digest(b"waivers"),
			}
		],
		"inputs": [
			{
				"path": "quality-gate.toml",
				"sha256": digest(manifest) if manifest is not None else None,
			}
		],
		"process_count": 2,
		"writes_may_have_occurred": False,
	}


def validate(root: Path, payload: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
	return validate_result(
		json.dumps(data),
		1 if data["status"] == "failed" else 0,
		payload,
		ENGINE_RELEASE,
		monotonic() + 5,
	)


def test_accepts_only_the_complete_source_free_failure_projection(tmp_path: Path) -> None:
	content = b"<<<<<<< HEAD\n"
	payload = request(tmp_path, content)
	finding = {
		"check_id": CHECK_ID,
		"path": "nested/merge.py",
		"line": 1,
		"status": "failed",
		"action": ACTION,
	}
	data = provider_result(tmp_path, payload, status="failed", findings=[finding])

	result = validate(tmp_path, payload, data)

	assert result == {
		"status": "failed",
		"reason": None,
		"findings": [{"path": "nested/merge.py", "line": 1}],
	}
	assert content.decode() not in json.dumps(result)


def test_changed_source_and_incomplete_coverage_fail_closed(tmp_path: Path) -> None:
	payload = request(tmp_path, b"clean\n")
	data = provider_result(tmp_path, payload)
	data["files"] = []

	missing = validate(tmp_path, payload, data)
	assert missing["status"] == "unchecked"
	assert missing["reason"] == "invalid_provider_result"

	data = provider_result(tmp_path, payload)
	(tmp_path / "nested" / "merge.py").write_bytes(b"changed\n")
	stale = validate(tmp_path, payload, data)
	assert stale["status"] == "unchecked"
	assert stale["reason"] == "stale_input"


def test_selection_and_exit_status_must_match(tmp_path: Path) -> None:
	payload = request(tmp_path, b"clean\n")
	data = provider_result(tmp_path, payload)
	data["selections"][0]["policy_release"] = "v2.5.0"
	wrong_release = validate(tmp_path, payload, data)
	assert wrong_release["status"] == "unchecked"
	assert wrong_release["reason"] == "invalid_provider_result"

	data = provider_result(tmp_path, payload)
	wrong_exit = validate_result(json.dumps(data), 1, payload, ENGINE_RELEASE, monotonic() + 5)
	assert wrong_exit["status"] == "unchecked"
	assert wrong_exit["reason"] == "invalid_provider_result"


def test_managed_selection_is_bound_to_the_current_manifest(tmp_path: Path) -> None:
	payload = request(tmp_path, b"clean\n")
	manifest = (
		b'[quality]\nschema = 2\npolicy_release = "v3.1.0"\n'
		b'[repository]\nname = "integrity fixture"\ndomains = ["repository"]\n'
		b'required_documents = ["AGENTS.md"]\n'
	)
	(tmp_path / "quality-gate.toml").write_bytes(manifest)
	data = provider_result(tmp_path, payload, manifest=manifest)

	accepted = validate(tmp_path, payload, data)
	assert accepted["status"] == "passed"

	(tmp_path / "quality-gate.toml").write_bytes(manifest + b"# changed\n")
	stale = validate(tmp_path, payload, data)
	assert stale["status"] == "unchecked"
	assert stale["reason"] == "stale_input"
