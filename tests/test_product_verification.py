"""Public read-only product-verification evidence contract."""

import hashlib
import json
from pathlib import Path

import pytest

from quality_gate.product_verification import VerificationError, aggregate, aggregate_directory


def test_missing_required_platform_cannot_pass() -> None:
	request = {
		"schema": 1,
		"repository": "owner/repo",
		"base_sha": "b" * 40,
		"head_sha": "a" * 40,
		"obligations": [
			{"producer": "package", "platform": "linux", "kind": "artifact"},
			{"producer": "package", "platform": "windows", "kind": "artifact"},
		],
	}
	with pytest.raises(VerificationError, match="missing"):
		aggregate(request, [])


def _request() -> dict:
	return {
		"schema": 1,
		"repository": "owner/repo",
		"base_sha": "b" * 40,
		"head_sha": "a" * 40,
		"obligations": [
			{"producer": "package", "platform": "linux", "kind": "artifact"},
			{"producer": "package", "platform": "windows", "kind": "artifact"},
		],
	}


def _result(platform: str) -> dict:
	return {
		"schema": 1,
		"repository": "owner/repo",
		"base_sha": "b" * 40,
		"head_sha": "a" * 40,
		"producer": "package",
		"platform": platform,
		"kind": "artifact",
		"status": "passed",
		"applicability": "applicable",
		"artifact": {"name": f"package-{platform}.zip", "sha256": "c" * 64, "source_sha": "a" * 40},
		"evidence": "run/123/job/456",
	}


def test_complete_platform_results_pass() -> None:
	assert (
		aggregate(_request(), [_result("linux"), _result("windows")])["status"]
		== "content-verified"
	)


@pytest.mark.parametrize(
	"mutation", ["duplicate", "wrong-source", "failed", "wrong-digest", "extra"]
)
def test_invalid_evidence_cannot_pass(mutation: str) -> None:
	linux = _result("linux")
	windows = _result("windows")
	if mutation == "duplicate":
		windows = _result("linux")
	elif mutation == "wrong-source":
		windows["artifact"]["source_sha"] = "d" * 40
	elif mutation == "failed":
		windows["status"] = "failed"
	elif mutation == "wrong-digest":
		windows["artifact"]["sha256"] = "bad"
	else:
		windows["platform"] = "macos"
	with pytest.raises(VerificationError):
		aggregate(_request(), [linux, windows])


def test_checks_only_cannot_impersonate_artifact() -> None:
	request = _request()
	request["obligations"][1]["kind"] = "checks-only"
	windows = _result("windows")
	windows["kind"] = "checks-only"
	with pytest.raises(VerificationError, match="checks-only"):
		aggregate(request, [_result("linux"), windows])


@pytest.mark.parametrize("corrupt", [False, True])
def test_downloaded_artifact_bytes_are_checked(tmp_path: Path, corrupt: bool) -> None:
	for platform in ("linux", "windows"):
		folder = tmp_path / platform
		folder.mkdir()
		result = _result(platform)
		payload = b"actual package"
		result["artifact"]["sha256"] = hashlib.sha256(payload).hexdigest()
		(folder / "verification.json").write_text(json.dumps(result), encoding="utf-8")
		(folder / result["artifact"]["name"]).write_bytes(
			b"corrupt" if corrupt and platform == "windows" else payload
		)
	if corrupt:
		with pytest.raises(VerificationError, match="digest mismatch"):
			aggregate_directory(_request(), tmp_path)
	else:
		assert aggregate_directory(_request(), tmp_path)["status"] == "content-verified"


def _proof(platform: str) -> dict:
	digest = "d" * 64
	return {
		"producer": "package",
		"platform": platform,
		"run": {
			"id": 123,
			"run_attempt": 1,
			"event": "pull_request",
			"path": ".github/workflows/quality.yml",
			"head_sha": "a" * 40,
			"repository": {"full_name": "owner/repo"},
			"head_repository": {"full_name": "fork/repo"},
		},
		"job": {
			"run_id": 123,
			"run_attempt": 1,
			"head_sha": "a" * 40,
			"name": f"Verify product package {platform}",
			"status": "completed",
			"conclusion": "success",
			"started_at": "2026-01-01T00:00:00Z",
			"completed_at": "2026-01-01T00:02:00Z",
			"steps": [
				{"name": "Verify product", "conclusion": "success"},
				{"name": "Upload verification evidence", "conclusion": "success"},
			],
		},
		"artifact": {
			"id": 42 if platform == "linux" else 43,
			"name": f"product-verification-package-{platform}-1",
			"digest": f"sha256:{digest}",
			"expired": False,
			"created_at": "2026-01-01T00:01:00Z",
			"workflow_run": {"id": 123, "head_sha": "a" * 40},
		},
		"logs": f"Artifact ID: {42 if platform == 'linux' else 43}\nSHA256 digest of uploaded artifact zip is {digest}\n",
	}


def test_transport_accepts_read_only_fork_run() -> None:
	from quality_gate.product_verification import verify_transport

	results = [_result("linux"), _result("windows")]
	for result in results:
		result["evidence"] = "run/123/attempt/1"
	assert (
		verify_transport(
			_request(),
			results,
			[_proof("linux"), _proof("windows")],
			run_id=123,
			attempt=1,
			workflow=".github/workflows/quality.yml",
		)["status"]
		== "transport-verified"
	)


@pytest.mark.parametrize("mutation", ["missing", "failed", "wrong-head", "duplicate", "receipt"])
def test_transport_rejects_incomplete_or_conflicting_proof(mutation: str) -> None:
	from quality_gate.product_verification import verify_transport

	results = [_result("linux"), _result("windows")]
	for result in results:
		result["evidence"] = "run/123/attempt/1"
	proofs = [_proof("linux"), _proof("windows")]
	if mutation == "missing":
		proofs.pop()
	elif mutation == "failed":
		proofs[1]["job"]["conclusion"] = "failure"
	elif mutation == "wrong-head":
		proofs[1]["run"]["head_sha"] = "c" * 40
	elif mutation == "duplicate":
		proofs[1] = _proof("linux")
	else:
		proofs[1]["logs"] = "unrelated upload"
	with pytest.raises(VerificationError):
		verify_transport(
			_request(),
			results,
			proofs,
			run_id=123,
			attempt=1,
			workflow=".github/workflows/quality.yml",
		)


def test_pr_adapter_rejects_corrupt_or_wrong_source_artifact(tmp_path: Path) -> None:
	from scripts.release_adapter import ReleaseError, verify_pr
	import subprocess

	head = subprocess.run(
		["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True
	).stdout.strip()
	artifact = tmp_path / "artifact.zip"
	artifact.write_bytes(b"corrupt")
	with pytest.raises(ReleaseError, match="digest"):
		verify_pr(head, artifact, "0" * 64)
	with pytest.raises(ReleaseError, match="exact source"):
		verify_pr("0" * 40, artifact, "0" * 64)
