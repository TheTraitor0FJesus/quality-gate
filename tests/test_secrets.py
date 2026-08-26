from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quality_gate.contracts import Manifest, Status, Verdict, fingerprint_secret, load_manifest
from quality_gate.reporting import render
from quality_gate.secrets import (
	secret_audit_result,
	secret_candidate_result,
	secret_history_result,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _prepared(tmp_path: Path, scanner: Path) -> SimpleNamespace:
	return SimpleNamespace(
		policy_root=tmp_path / "release",
		release_manifest=SimpleNamespace(
			tools=(SimpleNamespace(name="gitleaks", path=scanner.name),)
		),
	)


def _manifest(tmp_path: Path, waiver: str = "") -> Manifest:
	waiver_root = "" if waiver else "waivers = []\n"
	value = f"""{waiver_root}

[quality]
schema = 2
policy_release = "v2.0.0"

[repository]
name = "fixture"
domains = ["repository"]
required_documents = ["AGENTS.md"]

[repository.limits]
max_blob_size_mib = 5

[repository.defaults]
command_timeout_seconds = 120
test_timeout_seconds = 300
gate_timeout_seconds = 600
{waiver}"""
	path = tmp_path / "quality-gate.toml"
	path.write_text(value, encoding="utf-8")
	return load_manifest(tmp_path)


def _scanner(tmp_path: Path) -> Path:
	scanner = tmp_path / "gitleaks.exe"
	scanner.write_bytes(b"scanner")
	return scanner


def _write_report(path: Path, report: list[dict[str, object]]) -> None:
	path.write_text(json.dumps(report), encoding="utf-8")


def _run_fake_scanner(report_path: Path, report: list[dict[str, object]]) -> int:
	_write_report(report_path, report)
	return 1


def test_candidate_finding_exposes_only_location_and_fingerprint(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	secret = "ghp_ExampleSyntheticCredential_123456789"
	report = [
		{
			"File": "config.env",
			"StartLine": 4,
			"Secret": secret,
			"Description": "credential rule",
		}
	]
	scanner = _scanner(tmp_path)
	release = tmp_path / "release"
	release.mkdir()
	(release / scanner.name).write_bytes(scanner.read_bytes())
	prepared = _prepared(tmp_path, scanner)
	prepared.policy_root = release
	monkeypatch.setattr(
		"quality_gate.secrets._run_scanner",
		lambda *args, report_path, **kwargs: _run_fake_scanner(report_path, report),
	)
	result = secret_candidate_result(tmp_path, _manifest(tmp_path), prepared)

	assert result.status.value == "failed"
	assert "config.env:4" in result.findings[0].path + result.findings[0].message
	assert fingerprint_secret(secret) in result.findings[0].message
	assert secret not in result.findings[0].message
	assert "credential rule" not in result.findings[0].message
	assert secret not in render(Verdict((result,)), verbose=True)


def test_exact_fingerprint_waiver_applies_to_one_candidate_finding(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	secret = "fixture-secret-value"
	report = [{"File": "fixtures/example.txt", "StartLine": 1, "Secret": secret}]
	scanner = _scanner(tmp_path)
	release = tmp_path / "release"
	release.mkdir()
	(release / scanner.name).write_bytes(scanner.read_bytes())
	prepared = _prepared(tmp_path, scanner)
	prepared.policy_root = release
	waiver = f"""
[[waivers]]
kind = "secret"
check_id = "secrets.candidate"
target = "fixtures/example.txt:1"
reason = "synthetic fixture"
approved_by = "Human Reviewer"
reviewed_on = "2026-08-01"
expires_on = "2026-09-01"
fingerprint = "{fingerprint_secret(secret)}"
"""

	monkeypatch.setattr(
		"quality_gate.secrets._run_scanner",
		lambda *args, report_path, **kwargs: _run_fake_scanner(report_path, report),
	)
	result = secret_candidate_result(tmp_path, _manifest(tmp_path, waiver), prepared)

	assert result.status.value == "waived"
	assert secret not in result.summary


def test_missing_scanner_is_unchecked(tmp_path: Path) -> None:
	prepared = SimpleNamespace(
		policy_root=tmp_path / "release",
		release_manifest=SimpleNamespace(tools=()),
	)
	result = secret_candidate_result(tmp_path, _manifest(tmp_path), prepared)

	assert result.status.value == "unchecked"


def test_history_requires_an_unambiguous_base(tmp_path: Path) -> None:
	prepared = SimpleNamespace(
		policy_root=tmp_path / "release",
		release_manifest=SimpleNamespace(tools=()),
	)
	result = secret_history_result(tmp_path, _manifest(tmp_path), prepared, base="missing")

	assert result.status.value == "unchecked"


def test_history_finding_contains_commit_location_without_secret(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	secret = "history-secret-value"
	report = [
		{
			"File": "config.env",
			"StartLine": 2,
			"Commit": "a" * 40,
			"Secret": secret,
		}
	]
	scanner = _scanner(tmp_path)
	release = tmp_path / "release"
	release.mkdir()
	(release / scanner.name).write_bytes(scanner.read_bytes())
	prepared = _prepared(tmp_path, scanner)
	prepared.policy_root = release
	monkeypatch.setattr(
		"quality_gate.secrets._resolve_commit",
		lambda *args: "b" * 40,
	)
	monkeypatch.setattr(
		"quality_gate.secrets._run_scanner",
		lambda *args, report_path, **kwargs: _run_fake_scanner(report_path, report),
	)

	result = secret_history_result(
		tmp_path,
		_manifest(tmp_path),
		prepared,
		base="origin/main",
		head="HEAD",
	)

	assert result.status is Status.FAILED
	assert result.findings[0].path == f"{'a' * 40}:config.env:2"
	assert secret not in result.findings[0].message


def test_malformed_scanner_report_is_unchecked(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	scanner = _scanner(tmp_path)
	release = tmp_path / "release"
	release.mkdir()
	(release / scanner.name).write_bytes(scanner.read_bytes())
	prepared = _prepared(tmp_path, scanner)
	prepared.policy_root = release

	def write_invalid_report(*args: object, report_path: Path, **kwargs: object) -> int:
		report_path.write_text("not json", encoding="utf-8")
		return 1

	monkeypatch.setattr("quality_gate.secrets._run_scanner", write_invalid_report)
	result = secret_candidate_result(tmp_path, _manifest(tmp_path), prepared)

	assert result.status is Status.UNCHECKED


def test_audit_finding_uses_commit_location_and_hash(tmp_path: Path) -> None:
	result = secret_audit_result(
		tmp_path,
		_manifest(tmp_path),
		SimpleNamespace(
			policy_root=tmp_path / "release",
			release_manifest=SimpleNamespace(tools=()),
		),
	)

	assert result.status.value == "unchecked"
