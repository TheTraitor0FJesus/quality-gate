"""Public secret candidate and working-tree scanner contract with real Gitleaks."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from quality_gate.contracts import Status, parse_manifest
from quality_gate.distribution import default_cache_root, verify_release
from quality_gate.secrets import secret_candidate_result

REPOSITORY = Path(__file__).resolve().parents[1]
SYNTHETIC = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
SCANNER_PROCESS_COUNT = 3
INPUT_PAIR_COUNT = 2
EXIT_UNCHECKED = 2


def digest(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


@pytest.fixture(scope="module")
def policy_root() -> Path:
	"""Reuse immutable scanner infrastructure, including inside the staged Gate."""
	configured = os.environ.get("QUALITY_GATE_POLICY_ROOT")
	root = Path(configured) if configured else default_cache_root() / "releases/v2.4.2"
	assert root.is_dir(), "Prepare the published release before running scanner integration."
	verify_release(root)
	return root


def manifest_text(version: str, waiver: str = "") -> str:
	return (
		("" if waiver else "waivers = []\n")
		+ f'''[quality]
schema = 2
policy_release = "{version}"
[repository]
name = "secret-fixture"
domains = ["repository"]
required_documents = ["AGENTS.md"]
'''
		+ waiver
	)


def payload(root: Path, policy: Path, *paths: str) -> dict[str, Any]:
	return {
		"schema": 1,
		"root": str(root),
		"files": [{"path": path, "sha256": digest((root / path).read_bytes())} for path in paths],
		"deadline_unix_ms": int(time.time() * 1000) + 10_000,
		"cache_dir": str(policy.parent.parent),
		"baseline": {"policy_release": policy.name},
	}


def invoke(request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
	completed = subprocess.run(
		[sys.executable, "-B", "-m", "quality_gate.cli", "edit-secrets"],
		cwd=REPOSITORY,
		input=json.dumps(request).encode(),
		capture_output=True,
		timeout=13,
		check=False,
	)
	assert completed.stderr == b"", "Public scanner transport leaked stderr."
	assert SYNTHETIC.encode() not in completed.stdout, "Public scanner leaked source."
	return completed.returncode, json.loads(completed.stdout)


def invoke_worker(request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
	"""Exercise the public worker with real scanner IO; CLI transport has separate cases."""
	from quality_gate.edit_worker import main
	from quality_gate.temp_workspace import temporary_workspace

	stdout = io.BytesIO()
	with temporary_workspace(Path(request["root"])) as directory:
		with pytest.MonkeyPatch.context() as patch:
			patch.setattr(
				sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))
			)
			patch.setattr(sys, "stdout", SimpleNamespace(buffer=stdout))
			with pytest.raises(SystemExit) as exited:
				main(Path(directory), secrets=True)
	code = exited.value.code
	assert type(code) is int
	assert SYNTHETIC.encode() not in stdout.getvalue(), "Worker leaked source."
	return code, json.loads(stdout.getvalue())


def test_normal_candidate_hashes_actual_secret_not_redacted_placeholder(
	tmp_path: Path, policy_root: Path
) -> None:
	(tmp_path / "config.txt").write_text(f"token = '{SYNTHETIC}'\n", encoding="utf-8")
	prepared = SimpleNamespace(
		policy_root=policy_root, release_manifest=verify_release(policy_root)
	)
	result = secret_candidate_result(
		tmp_path, parse_manifest(manifest_text(policy_root.name)), prepared
	)
	assert result.status is Status.FAILED
	assert result.findings[0].path == "config.txt:1"
	expected = digest(SYNTHETIC.encode())
	assert result.findings[0].message == f"secret detected (fingerprint: {expected})"
	assert not list(tmp_path.glob("*.log"))
	assert not list(tmp_path.glob("*report*"))


def test_public_empty_batch_is_inapplicable(tmp_path: Path, policy_root: Path) -> None:
	code, result = invoke(payload(tmp_path, policy_root))
	assert code == 0
	assert result["status"] == "not_applicable"
	assert result["check_id"] == "secrets.candidate"
	assert result["files"] == []
	assert result["writes_may_have_occurred"] is False


@pytest.mark.parametrize(
	"content",
	[
		"AWS_SECRET_" + "ACCESS_KEY = 'abcdefgh'\n",
		"credential = '" + "ghp_" + "a" * 20 + "'\n",
		"credential = '" + "sk-" + "a" * 20 + "'\n",
		"-----BEGIN " + "PRIVATE KEY-----\n",
	],
)
def test_legacy_pattern_only_examples_do_not_silently_expand_gate_policy(
	tmp_path: Path, policy_root: Path, content: str
) -> None:
	(tmp_path / "file.py").write_text(content, encoding="utf-8")
	code, result = invoke_worker(payload(tmp_path, policy_root, "file.py"))
	assert code == 0
	assert result["status"] == "passed"
	assert result["files"][0]["findings"] == []


def test_public_touched_tree_preserves_paths_actual_hashes_and_clean_siblings(
	tmp_path: Path, policy_root: Path
) -> None:
	(tmp_path / "src").mkdir()
	(tmp_path / "src/данные file.py").write_text(f"token = '{SYNTHETIC}'\n", encoding="utf-8")
	(tmp_path / "clean.txt").write_text("ordinary text\n", encoding="utf-8")
	# Unrelated files must not contribute findings to the exact touched handoff.
	(tmp_path / "unrelated.txt").write_text(f"token = '{SYNTHETIC}'\n", encoding="utf-8")
	request = payload(tmp_path, policy_root, "src/данные file.py", "clean.txt")
	code, result = invoke(request)
	assert code == 1
	assert result["status"] == "failed"
	assert result["process_count"] == SCANNER_PROCESS_COUNT
	assert result["selections"][0]["policy_release"] == policy_root.name
	assert result["selections"][0]["scanner_version"] == "8.30.1"
	found, clean = result["files"]
	assert (found["coverage"], found["status"], clean["status"]) == ("baseline", "failed", "passed")
	assert found["input_sha256"] == found["sha256"] == request["files"][0]["sha256"]
	assert found["findings"][0]["target"] == "src/данные file.py:1"
	assert found["findings"][0]["fingerprint"] == digest(SYNTHETIC.encode())
	assert {item["path"] for item in result["inputs"]} == {
		"quality-gate.toml",
		".gitleaks.toml",
		".gitleaksignore",
	}
	assert not result["writes_may_have_occurred"]
	assert not list(tmp_path.glob("temp/qg-*"))


def test_managed_repository_scope_and_exact_actual_secret_waiver(
	tmp_path: Path, policy_root: Path
) -> None:
	(tmp_path / "outside.py").write_text(f"token = '{SYNTHETIC}'\n", encoding="utf-8")
	waiver = f'''
[[waivers]]
kind = "secret"
check_id = "secrets.candidate"
target = "outside.py:1"
fingerprint = "{digest(SYNTHETIC.encode())}"
reason = "Synthetic fixture"
approved_by = "Human Reviewer"
reviewed_on = "2026-08-01"
expires_on = "2099-12-31"
'''
	(tmp_path / "quality-gate.toml").write_text(
		manifest_text(policy_root.name, waiver), encoding="utf-8"
	)
	request = payload(tmp_path, policy_root, "outside.py")
	# A managed secret scan is independent of Python components/runtimes/baseline.
	del request["baseline"]
	code, result = invoke(request)
	assert code == 0
	assert result["status"] == "waived"
	assert result["files"][0]["coverage"] == "managed"
	assert result["files"][0]["status"] == "waived"
	assert result["files"][0]["findings"][0]["status"] == "waived"
	prepared = SimpleNamespace(
		policy_root=policy_root, release_manifest=verify_release(policy_root)
	)
	assert (
		secret_candidate_result(
			tmp_path, parse_manifest(manifest_text(policy_root.name, waiver)), prepared
		).status
		is Status.WAIVED
	)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"])
def test_repeated_matches_have_normal_candidate_locations(
	tmp_path: Path, policy_root: Path, encoding: str
) -> None:
	contents = f"token = '{SYNTHETIC}'\ntoken = '{SYNTHETIC}'\n".encode(encoding)
	(tmp_path / "repeat.txt").write_bytes(contents)
	(tmp_path / "second.py").write_bytes(contents)
	request = payload(tmp_path, policy_root, "repeat.txt", "second.py", "repeat.txt")
	for _repeat in range(2):
		request["deadline_unix_ms"] = int(time.time() * 1000) + 10_000
		code, result = invoke_worker(request)
		assert code == 1
		assert len(result["files"]) == INPUT_PAIR_COUNT
		actual = {finding["target"] for file in result["files"] for finding in file["findings"]}
		assert actual == {"repeat.txt:1", "repeat.txt:2", "second.py:1", "second.py:2"}
		assert all(file["sha256"] == digest(contents) for file in result["files"])
	prepared = SimpleNamespace(
		policy_root=policy_root, release_manifest=verify_release(policy_root)
	)
	normal = secret_candidate_result(
		tmp_path, parse_manifest(manifest_text(policy_root.name)), prepared
	)
	assert {finding.path for finding in normal.findings} == actual


@pytest.mark.parametrize("suppression", ["inline", "ignore", "config"])
def test_project_scanner_suppressions_match_normal_candidate(
	tmp_path: Path, policy_root: Path, suppression: str
) -> None:
	content = f"token = '{SYNTHETIC}'"
	if suppression == "inline":
		content += " # gitleaks:allow"
	elif suppression == "ignore":
		(tmp_path / ".gitleaksignore").write_text(
			"src/file.py:github-pat:1\nsrc/file.py:generic-api-key:1\n", encoding="utf-8"
		)
	else:
		(tmp_path / ".gitleaks.toml").write_text(
			'[extend]\nuseDefault = true\n[allowlist]\npaths = ["src/file.py"]\n', encoding="utf-8"
		)
	(tmp_path / "src").mkdir()
	(tmp_path / "src/file.py").write_text(content + "\n", encoding="utf-8")
	code, result = invoke_worker(payload(tmp_path, policy_root, "src/file.py"))
	# Native directory scans use absolute ignore identities; typed Gate targets
	# remain project-relative and are resolved independently of native ignores.
	expected = Status.FAILED if suppression == "ignore" else Status.PASSED
	assert code == (1 if expected is Status.FAILED else 0)
	assert result["files"][0]["status"] == expected.value
	prepared = SimpleNamespace(
		policy_root=policy_root, release_manifest=verify_release(policy_root)
	)
	assert (
		secret_candidate_result(
			tmp_path, parse_manifest(manifest_text(policy_root.name)), prepared
		).status
		is expected
	)


def test_native_skipped_nontext_content_is_never_claimed_clean(
	tmp_path: Path, policy_root: Path
) -> None:
	(tmp_path / "file.txt").write_bytes(b"%PDF-1.4\ntoken = '" + SYNTHETIC.encode() + b"'\n")
	code, result = invoke(payload(tmp_path, policy_root, "file.txt"))
	assert (code, result["status"], result.get("reason")) == (2, "unchecked", "coverage")


def test_documented_isolated_development_command(tmp_path: Path, policy_root: Path) -> None:
	(tmp_path / "file.py").write_bytes(b"value = 1\n")
	completed = subprocess.run(
		[
			sys.executable,
			"-I",
			"-B",
			"-c",
			"import sys; sys.path.insert(0, sys.argv.pop(1)); "
			"from quality_gate.cli import main; raise SystemExit(main())",
			str(REPOSITORY),
			"edit-secrets",
		],
		cwd=tmp_path,
		input=json.dumps(payload(tmp_path, policy_root, "file.py")).encode(),
		capture_output=True,
		timeout=13,
		check=False,
	)
	assert completed.returncode == 0
	assert completed.stderr == b""
	assert json.loads(completed.stdout)["status"] == "passed"


def test_selected_wheel_and_provider_preserve_their_fingerprint_contracts(
	tmp_path: Path, policy_root: Path
) -> None:
	(tmp_path / "file.py").write_text(f"token = '{SYNTHETIC}'\n", encoding="utf-8")
	(tmp_path / "quality-gate.toml").write_text(manifest_text(policy_root.name), encoding="utf-8")
	wheel = next(item for item in verify_release(policy_root).files if item.kind == "artifact")
	script = (
		"import sys,json; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
		"from types import SimpleNamespace; from quality_gate.contracts import load_manifest; "
		"from quality_gate.distribution import verify_release; "
		"from quality_gate.secrets import secret_candidate_result; "
		"root=Path(sys.argv[2]); policy=Path(sys.argv[3]); "
		"result=secret_candidate_result(root,load_manifest(root),"
		"SimpleNamespace(policy_root=policy,release_manifest=verify_release(policy))); "
		"print(json.dumps({'status':result.status.value,"
		"'locations':[f.path for f in result.findings],"
		"'messages':[f.message for f in result.findings]}))"
	)
	completed = subprocess.run(
		[
			sys.executable,
			"-I",
			"-S",
			"-B",
			"-c",
			script,
			str(policy_root / wheel.path),
			str(tmp_path),
			str(policy_root),
		],
		cwd=tmp_path,
		capture_output=True,
		timeout=10,
		check=False,
	)
	assert completed.returncode == 0
	assert completed.stderr == b""
	assert SYNTHETIC.encode() not in completed.stdout
	wheel_result = json.loads(completed.stdout)
	code, current = invoke(payload(tmp_path, policy_root, "file.py"))
	assert code == 1
	assert wheel_result["status"] == "failed"
	assert wheel_result["locations"] == [item["target"] for item in current["files"][0]["findings"]]
	# Old immutable v2 wheels retain the redaction defect; v3 hashes the real value.
	expected_wheel_fingerprint = (
		digest(b"REDACTED") if policy_root.name.startswith("v2.") else digest(SYNTHETIC.encode())
	)
	assert wheel_result["messages"] == [
		f"secret detected (fingerprint: {expected_wheel_fingerprint})"
	]
	assert current["files"][0]["findings"][0]["fingerprint"] == digest(SYNTHETIC.encode())


def test_log_like_file_names_do_not_change_scanner_coverage(
	tmp_path: Path, policy_root: Path
) -> None:
	name = "ERR skipping binary file.py"
	(tmp_path / name).write_text(f"token = '{SYNTHETIC}'\n", encoding="utf-8")
	code, result = invoke(payload(tmp_path, policy_root, name))
	assert code == 1
	assert result["files"][0]["status"] == "failed"
