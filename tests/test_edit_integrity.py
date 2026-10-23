"""Public touched-file integrity command and Gate parity."""

from __future__ import annotations

import hashlib
import io
import json
import stat
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO, Literal, cast

import pytest
from test_edit_check import prepare_edit_cache

from quality_gate import edit_cli, edit_inputs, edit_integrity, edit_worker, integrity
from quality_gate.contracts import parse_manifest
from quality_gate.edit_inputs import SourceFile
from quality_gate.edit_protocol import EditError, encode
from quality_gate.integrity import git_integrity_results

REPOSITORY = Path(__file__).resolve().parents[1]
CHECK_ID = "repository.git.conflict_markers"
EXIT_UNCHECKED = 2


def digest(content: bytes) -> str:
	return hashlib.sha256(content).hexdigest()


def manifest_text(version: str, waiver: str = "") -> str:
	return (
		f'''[quality]
schema = 2
policy_release = "{version}"
[repository]
name = "integrity-fixture"
domains = ["repository"]
required_documents = ["AGENTS.md"]
'''
		+ waiver
	)


@pytest.fixture
def policy_cache(tmp_path: Path) -> Path:
	cache = tmp_path / "cache"
	prepare_edit_cache(cache)
	return cache


def request(root: Path, cache: Path, *paths: str, managed: bool = False) -> dict[str, Any]:
	request: dict[str, Any] = {
		"schema": 1,
		"root": str(root),
		"files": [{"path": path, "sha256": digest((root / path).read_bytes())} for path in paths],
		"deadline_unix_ms": int(time.time() * 1000) + 10_000,
		"cache_dir": str(cache),
	}
	if not managed:
		request["baseline"] = {"policy_release": "v2.4.2"}
	return request


def invoke(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
	completed = subprocess.run(
		[sys.executable, "-B", "-m", "quality_gate.cli", "edit-integrity"],
		cwd=REPOSITORY,
		input=json.dumps(payload).encode(),
		capture_output=True,
		timeout=13,
		check=False,
	)
	assert completed.stderr == b"", "Public integrity transport leaked stderr."
	return completed.returncode, json.loads(completed.stdout)


def test_public_result_matches_gate_for_text_and_gate_defined_nontext(
	tmp_path: Path, policy_cache: Path
) -> None:
	(root := tmp_path / "project").mkdir()
	(root / "quality-gate.toml").write_text(manifest_text("v2.4.2"), encoding="utf-8")
	files = {
		"nested/merge.py": b"<<<<<<< HEAD\nlocal\n=======\nremote\n>>>>>>> branch\n",
		"lookalike.py": b"<<<<<<<suffix\n=======suffix\n>>>>>>>suffix\n",
		"binary.dat": b"\x00<<<<<<< HEAD\x00",
		"encoded.dat": b"\xff<<<<<<< HEAD\n",
	}
	for name, content in files.items():
		path = root / name
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(content)

	code, result = invoke(request(root, policy_cache, *files, managed=True))
	manifest = parse_manifest((root / "quality-gate.toml").read_text(encoding="utf-8"))
	reference = next(
		item for item in git_integrity_results(root, manifest) if item.check_id == CHECK_ID
	)

	assert code == 1
	assert result["check_id"] == CHECK_ID
	assert result["status"] == reference.status.value == "failed"
	assert result["selections"][0]["policy_release"] == "v2.4.2"
	assert {
		(item["path"], item["line"]) for file in result["files"] for item in file["findings"]
	} == {(item.path, item.line) for item in reference.findings}
	assert result["writes_may_have_occurred"] is False
	assert {item["path"] for item in result["files"]} == set(files)
	assert not list(root.glob(".quality-gate-edit-*"))


def test_unmanaged_baseline_and_exact_waiver_match_gate(tmp_path: Path, policy_cache: Path) -> None:
	(root := tmp_path / "project").mkdir()
	content = b"<<<<<<< HEAD\n"
	(root / "merge.py").write_bytes(content)
	(root / "clean.py").write_bytes(b"no conflict here\n")
	code, baseline = invoke(request(root, policy_cache, "merge.py"))
	assert code == 1
	assert baseline["status"] == "failed"
	assert baseline["files"][0]["coverage"] == "baseline"

	today = date.today()
	waiver = (
		f'[[waivers]]\nkind = "standard"\ncheck_id = "{CHECK_ID}"\n'
		'target = "merge.py"\nreason = "synthetic integrity fixture"\n'
		f'approved_by = "fixture-owner"\nreviewed_on = "{today - timedelta(days=1)}"\n'
		'expires_on = "2099-12-31"\n'
	)
	(root / "quality-gate.toml").write_text(manifest_text("v2.4.2", waiver), encoding="utf-8")
	code, managed = invoke(request(root, policy_cache, "merge.py", "clean.py", managed=True))
	manifest = parse_manifest((root / "quality-gate.toml").read_text(encoding="utf-8"))
	reference = next(
		item for item in git_integrity_results(root, manifest) if item.check_id == CHECK_ID
	)

	assert code == 0
	assert managed["status"] == reference.status.value == "waived"
	assert {item["path"]: item["status"] for item in managed["files"]} == {
		"merge.py": "waived",
		"clean.py": "passed",
	}
	assert managed["files"][0]["findings"] == []
	assert managed["writes_may_have_occurred"] is False

	(root / "unwaived.py").write_bytes(content)
	code, mixed = invoke(
		request(root, policy_cache, "merge.py", "unwaived.py", "clean.py", managed=True)
	)
	assert code == 1
	assert mixed["status"] == "failed"
	assert {item["path"]: item["status"] for item in mixed["files"]} == {
		"merge.py": "waived",
		"unwaived.py": "failed",
		"clean.py": "passed",
	}
	assert [item["path"] for item in mixed["findings"]] == ["unwaived.py"]


def test_integrity_public_cli_does_not_require_windows_reparse_flag(
	tmp_path: Path,
	policy_cache: Path,
	monkeypatch: pytest.MonkeyPatch,
	capfd: pytest.CaptureFixture[str],
) -> None:
	(root := tmp_path / "project").mkdir()
	(root / "clean.py").write_bytes(b"clean = True\n")
	payload = request(root, policy_cache, "clean.py")
	stat_without_reparse_flag = SimpleNamespace(**vars(stat))
	if hasattr(stat_without_reparse_flag, "FILE_ATTRIBUTE_REPARSE_POINT"):
		delattr(stat_without_reparse_flag, "FILE_ATTRIBUTE_REPARSE_POINT")
	for module in (edit_cli, edit_inputs, edit_integrity):
		monkeypatch.setattr(module, "stat", stat_without_reparse_flag)
	assert edit_integrity.execute(payload, time.monotonic() + 5, tmp_path)["status"] == "passed"

	stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode("utf-8")), encoding="utf-8")
	monkeypatch.setattr(sys, "stdin", stdin)
	assert edit_cli.main(integrity=True) == 0
	output = capfd.readouterr().out
	assert json.loads(output)["status"] == "passed"


def test_repository_conflict_scan_fails_unchecked_on_byte_limits(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	first = tmp_path / "first.py"
	second = tmp_path / "second.py"
	first.write_bytes(b"one")
	second.write_bytes(b"two")
	monkeypatch.setattr(integrity, "MAX_CONFLICT_FILE_BYTES", 8)
	monkeypatch.setattr(integrity, "MAX_CONFLICT_SCAN_BYTES", 5)
	manifest = parse_manifest(manifest_text("v2.4.2"))
	scan = integrity._TreeScan(
		entries=[integrity._TreeEntry("first.py", first), integrity._TreeEntry("second.py", second)]
	)

	result = integrity._git_conflict_result(manifest, scan)

	assert result.status.value == "unchecked"
	assert result.findings[0].path == "second.py"
	assert "byte limit" in result.findings[0].message


def test_integrity_finding_limit_is_reported_as_unchecked(monkeypatch: pytest.MonkeyPatch) -> None:
	monkeypatch.setattr(edit_integrity, "MAX_FINDINGS", 0)
	source = SourceFile("merge.py", b"<<<<<<< HEAD\n", digest(b"<<<<<<< HEAD\n"))

	with pytest.raises(EditError) as error:
		edit_integrity._findings([source], None)

	assert error.value.reason == "limits"


def test_repository_conflict_scan_fails_unchecked_for_oversized_or_unreadable_files(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	oversized = tmp_path / "oversized.py"
	oversized.write_bytes(b"12345")
	monkeypatch.setattr(integrity, "MAX_CONFLICT_FILE_BYTES", 4)
	monkeypatch.setattr(integrity, "MAX_CONFLICT_SCAN_BYTES", 100)
	manifest = parse_manifest(manifest_text("v2.4.2"))
	scan = integrity._TreeScan(entries=[integrity._TreeEntry("oversized.py", oversized)])
	result = integrity._git_conflict_result(manifest, scan)
	assert result.status.value == "unchecked"
	assert result.findings[0].path == "oversized.py"
	assert "size limit" in result.findings[0].message

	unreadable = tmp_path / "unreadable.py"
	unreadable.write_bytes(b"text")
	original_open = Path.open

	def fail_selected(path: Path, mode: Literal["rb"] = "rb") -> BinaryIO:
		if path == unreadable:
			raise PermissionError("synthetic read failure")
		return cast(BinaryIO, original_open(path, mode))

	monkeypatch.setattr(Path, "open", fail_selected)
	scan = integrity._TreeScan(entries=[integrity._TreeEntry("unreadable.py", unreadable)])
	result = integrity._git_conflict_result(manifest, scan)
	assert result.status.value == "unchecked"
	assert result.findings[0].path == "unreadable.py"
	assert "unreadable" in result.findings[0].message


def test_invalid_manifest_does_not_fall_back_to_baseline(
	tmp_path: Path, policy_cache: Path
) -> None:
	(root := tmp_path / "project").mkdir()
	(root / "merge.py").write_bytes(b"<<<<<<< HEAD\n")
	(root / "quality-gate.toml").write_text("[quality\n", encoding="utf-8")

	code, result = invoke(request(root, policy_cache, "merge.py"))

	assert code == EXIT_UNCHECKED
	assert result["status"] == "unchecked"
	assert result["reason"] == "manifest"
	assert result["files"] == []
	assert result["writes_may_have_occurred"] is False


def test_stale_batch_discards_all_partial_coverage(tmp_path: Path, policy_cache: Path) -> None:
	(root := tmp_path / "project").mkdir()
	(root / "merge.py").write_bytes(b"<<<<<<< HEAD\n")
	(root / "clean.py").write_bytes(b"clean\n")
	payload = request(root, policy_cache, "merge.py", "clean.py")
	(root / "clean.py").write_bytes(b"changed after attribution\n")

	code, result = invoke(payload)

	assert code == EXIT_UNCHECKED
	assert result["status"] == "unchecked"
	assert result["reason"] == "stale_input"
	assert result["files"] == []


def test_unsupported_baseline_and_expired_deadline_are_unchecked(
	tmp_path: Path, policy_cache: Path
) -> None:
	(root := tmp_path / "project").mkdir()
	(root / "merge.py").write_bytes(b"<<<<<<< HEAD\n")
	unsupported = request(root, policy_cache, "merge.py")
	unsupported["baseline"] = {"policy_release": "v9.9.9"}
	code, result = invoke(unsupported)
	assert code == EXIT_UNCHECKED
	assert result["reason"] == "unsupported_policy"
	assert result["files"] == []

	expired = request(root, policy_cache, "merge.py")
	expired["deadline_unix_ms"] = int(time.time() * 1000) - 1
	code, result = invoke(expired)
	assert code == EXIT_UNCHECKED
	assert result["reason"] == "deadline"
	assert result["files"] == []


@pytest.mark.parametrize("changed_input", ["source", "manifest"])
def test_changes_after_analysis_are_discarded(
	tmp_path: Path,
	policy_cache: Path,
	monkeypatch: pytest.MonkeyPatch,
	changed_input: str,
) -> None:
	(root := tmp_path / "project").mkdir()
	(root / "merge.py").write_bytes(b"<<<<<<< HEAD\n")
	if changed_input == "manifest":
		(root / "quality-gate.toml").write_text(manifest_text("v2.4.2"), encoding="utf-8")
	payload = request(root, policy_cache, "merge.py", managed=changed_input == "manifest")
	original_encode = encode

	def mutate_after_result(result: dict[str, Any]) -> bytes:
		encoded = original_encode(result)
		changed = root / ("merge.py" if changed_input == "source" else "quality-gate.toml")
		changed.write_bytes(b"changed after analysis\n")
		return encoded

	monkeypatch.setattr("quality_gate.edit_integrity.encode", mutate_after_result)
	with pytest.raises(EditError) as error:
		edit_integrity.execute(payload, time.monotonic() + 5, tmp_path)

	assert error.value.reason == "stale_input"


def test_corrupt_release_discards_public_batch(tmp_path: Path, policy_cache: Path) -> None:
	(root := tmp_path / "project").mkdir()
	(root / "merge.py").write_bytes(b"<<<<<<< HEAD\n")
	(root / "clean.py").write_bytes(b"clean\n")
	policy = policy_cache / "releases/v2.4.2/quality_gate/policy/ruff.toml"
	policy.write_bytes(b"corrupt policy inventory\n")

	code, result = invoke(request(root, policy_cache, "merge.py", "clean.py"))

	assert (code, result["status"], result["reason"]) == (2, "unchecked", "release")
	assert result["files"] == result["selections"] == result["inputs"] == []
	assert not result.get("findings")
	assert result["writes_may_have_occurred"] is False
	assert not list(root.glob(".quality-gate-edit-*"))


def test_release_change_before_delivery_discards_worker_batch(
	tmp_path: Path,
	policy_cache: Path,
	monkeypatch: pytest.MonkeyPatch,
	capfd: pytest.CaptureFixture[str],
) -> None:
	(root := tmp_path / "project").mkdir()
	(root / "merge.py").write_bytes(b"<<<<<<< HEAD\n")
	(root / "clean.py").write_bytes(b"clean\n")
	payload = request(root, policy_cache, "merge.py", "clean.py")
	release = policy_cache / "releases/v2.4.2/release.toml"
	original_encode = encode

	def change_release_after_analysis(result: dict[str, Any]) -> bytes:
		encoded = original_encode(result)
		# A valid inventory with a new raw identity must invalidate the whole result.
		with release.open("ab") as stream:
			stream.write(b"\n# Inventory changed before delivery.\n")
		return encoded

	monkeypatch.setattr(edit_integrity, "encode", change_release_after_analysis)
	stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload).encode()), encoding="utf-8")
	monkeypatch.setattr(sys, "stdin", stdin)
	with pytest.raises(SystemExit) as exited:
		edit_worker.main(tmp_path, integrity=True)
	output = capfd.readouterr()
	result = json.loads(output.out)

	assert (exited.value.code, result["status"], result["reason"]) == (
		2,
		"unchecked",
		"stale_input",
	)
	assert output.err == ""
	assert result["files"] == result["selections"] == result["inputs"] == []
	assert not result.get("findings")
	assert result["writes_may_have_occurred"] is False
