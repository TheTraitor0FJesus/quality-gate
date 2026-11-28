"""Independent full-suite execution uses prepared runtimes and real pytest evidence."""

import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from cli_feature_support72 import _disposable_repository, _git

from quality_gate.contracts import load_manifest
from quality_gate.runtime import RuntimeInspection


def _candidate(tmp_path: Path, source: str) -> tuple[SimpleNamespace, str]:
	root = _disposable_repository(tmp_path, "python-tests")
	manifest = root / "quality-gate.toml"
	manifest.write_text(
		manifest.read_text().replace('["tests", "tests/extra"]', '["tests"]'),
		encoding="utf-8",
	)
	shutil.rmtree(root / "tests")
	(root / "tests").mkdir()
	(root / "tests" / "test_probe.py").write_text(source, encoding="utf-8")
	assert _git(root, "add", ".").returncode == 0
	commit = _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "fixture")
	assert commit.returncode == 0, commit.stderr
	return SimpleNamespace(
		root=root,
		manifest=load_manifest(root),
		policy_root=tmp_path / "selected-policy",
		release_manifest=SimpleNamespace(version="v4.9.0"),
		runtimes=(RuntimeInspection(Path(sys.prefix), Path(sys.executable), True),),
	), _git(root, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize(
	("source", "status"),
	[
		("def test_probe():\n    assert True\n", "passed"),
		("def test_probe():\n    assert False\n", "failed"),
		("raise RuntimeError('collection failure')\n", "error"),
		("VALUE = 1\n", "error"),
		("raise KeyboardInterrupt\n", "error"),
		("import pytest\npytestmark = pytest.mark.skip\ndef test_probe(): pass\n", "error"),
	],
)
def test_full_suite_reports_actual_pytest_outcomes(tmp_path: Path, source: str, status: str) -> None:
	from quality_gate.pytest_ci import run_tests

	prepared, sha = _candidate(tmp_path, source)
	result = run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == status, result
	assert result["source_sha"] == sha
	assert result["components"][0]["status"] == status
	assert (tmp_path / "evidence" / "result.json").is_file()
	if status == "passed":
		assert result["components"][0]["totals"] == {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}


def test_full_suite_ignores_inherited_test_selection(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
	from quality_gate.pytest_ci import run_tests

	prepared, sha = _candidate(
		tmp_path, "def test_pass(): pass\ndef test_fail(): assert False\n"
	)
	monkeypatch.setenv("PYTEST_ADDOPTS", "-k test_pass")
	result = run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == "failed", result
	assert result["components"][0]["totals"] == {
		"tests": 2, "failures": 1, "errors": 0, "skipped": 0,
	}


def test_ignored_local_sources_cannot_make_full_suite_pass(tmp_path: Path) -> None:
	from quality_gate.pytest_ci import run_tests

	prepared, sha = _candidate(
		tmp_path,
		"def test_probe():\n"
		"    from app import local_config\n"
		"    assert local_config.VALUE == 1\n",
	)
	ignored_source = prepared.root / "app" / "local_config.py"
	ignored_source.parent.mkdir(exist_ok=True)
	ignored_source.write_text("VALUE = 1\n", encoding="utf-8")
	exclude = prepared.root / ".git" / "info" / "exclude"
	exclude.write_text(exclude.read_text(encoding="utf-8") + "\n/app/local_config.py\n", encoding="utf-8")

	result = run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] in {"failed", "error"}, result
	assert result["components"][0]["status"] in {"failed", "error"}
	assert ignored_source.read_text(encoding="utf-8") == "VALUE = 1\n"
	worktrees = _git(prepared.root, "worktree", "list", "--porcelain")
	assert sum(line.startswith("worktree ") for line in worktrees.stdout.splitlines()) == 1


def test_unavailable_source_worktree_is_an_error(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
	import subprocess

	from quality_gate import pytest_ci

	prepared, sha = _candidate(tmp_path, "def test_probe(): pass\n")
	original_run = pytest_ci.subprocess.run

	def reject_worktree(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
		if command[:3] == ["git", "worktree", "add"]:
			return subprocess.CompletedProcess(command, 1, stdout="", stderr="unavailable")
		return original_run(command, **kwargs)

	monkeypatch.setattr(pytest_ci.subprocess, "run", reject_worktree)
	result = pytest_ci.run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == "error", result
	assert result["reason"] == "source_snapshot_unavailable"


def test_missing_runtime_is_an_error(tmp_path: Path) -> None:
	from quality_gate.pytest_ci import run_tests

	prepared, sha = _candidate(tmp_path, "def test_probe(): pass\n")
	prepared.runtimes = (RuntimeInspection(tmp_path / "missing", None, False, "missing"),)
	result = run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == "error"
	assert result["components"][0]["reason"] == "runtime_unavailable"


def test_zero_exit_without_junit_is_incomplete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from quality_gate import pytest_ci

	prepared, sha = _candidate(tmp_path, "def test_probe(): pass\n")
	monkeypatch.setattr(pytest_ci, "run_process", lambda *_args, **_kwargs: (0, b"", b""))
	result = pytest_ci.run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == "error"
	assert result["components"][0]["reason"] == "junit_unavailable"


def test_changed_source_cannot_report_success(tmp_path: Path) -> None:
	from quality_gate.pytest_ci import run_tests

	prepared, sha = _candidate(tmp_path, 'from pathlib import Path\ndef test_probe():\n    Path("app/app.py").write_text("CHANGED = True\\n")\n')
	result = run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == "error"
	assert result["reason"] == "source_changed"


def test_timeout_is_incomplete(tmp_path: Path) -> None:
	from quality_gate.pytest_ci import run_tests

	prepared, sha = _candidate(tmp_path, "import time\ndef test_probe():\n    time.sleep(60)\n")
	result = run_tests(prepared, tmp_path / "evidence", source_sha=sha, timeout_seconds=2)
	assert result["status"] == "error"
	assert result["components"][0]["reason"] == "execution_incomplete"


def test_explicitly_test_free_component_is_not_applicable(tmp_path: Path) -> None:
	from quality_gate.pytest_ci import run_tests

	prepared, sha = _candidate(tmp_path, "def test_probe(): assert False\n")
	component = replace(prepared.manifest.python[0], tests_applicable=False, test_paths=(), tests_reason="No executable tests")
	prepared.manifest = replace(prepared.manifest, python=(component,))
	prepared.runtimes = ()
	result = run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == "not_applicable"
	assert result["components"] == [{"name": component.name, "test_paths": [], "status": "not_applicable", "reason": "No executable tests"}]


def test_all_components_and_declared_paths_run_after_a_failure(tmp_path: Path) -> None:
	from quality_gate.pytest_ci import run_tests

	prepared, _sha = _candidate(tmp_path, "def test_probe(): assert False\n")
	root = prepared.root
	(root / "extra").mkdir()
	(root / "extra/test_second.py").write_text("def test_second(): pass\n")
	(root / "third").mkdir()
	(root / "third/test_third.py").write_text("def test_third(): pass\n")
	assert _git(root, "add", ".").returncode == 0
	assert _git(root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "paths").returncode == 0
	sha = _git(root, "rev-parse", "HEAD").stdout.strip()
	first = replace(prepared.manifest.python[0], test_paths=("tests", "extra"))
	second = replace(first, name="second", test_paths=("third",))
	prepared.manifest = replace(prepared.manifest, python=(first, second))
	prepared.runtimes *= 2
	result = run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == "failed"
	assert [item["status"] for item in result["components"]] == ["failed", "passed"]
	assert [item["totals"]["tests"] for item in result["components"]] == [2, 1]


@pytest.mark.parametrize("xml", ["<broken", '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"/></testsuites>'])
def test_invalid_or_incomplete_junit_cannot_report_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xml: str) -> None:
	from quality_gate import pytest_ci

	prepared, sha = _candidate(tmp_path, "def test_probe(): pass\n")
	def incomplete(command: list[str], **_kwargs: object) -> tuple[int, bytes, bytes]:
		Path(command[command.index("--junitxml") + 1]).write_text(xml)
		return 0, b"", b""
	monkeypatch.setattr(pytest_ci, "run_process", incomplete)
	result = pytest_ci.run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == "error"
	assert result["components"][0]["reason"] == "junit_unavailable"


def test_published_failure_reports_redact_secrets(tmp_path: Path) -> None:
	from quality_gate.pytest_ci import run_tests

	secret = "ghp_" + "exampleCredential123456789"
	prepared, sha = _candidate(tmp_path, f'def test_probe():\n    raise RuntimeError("{secret}")\n')
	output = tmp_path / "evidence"
	result = run_tests(prepared, output, source_sha=sha)
	assert result["status"] == "failed", result
	assert (output / "component-1.xml").is_file()
	for path in output.iterdir():
		assert secret not in path.read_text(encoding="utf-8")
	assert "REDACTED" in (output / "component-1.xml").read_text(encoding="utf-8")


def test_interrupted_raw_report_never_enters_public_evidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
	from collections.abc import Iterator
	from contextlib import contextmanager

	from quality_gate import pytest_ci
	from quality_gate.edit_protocol import EditError

	prepared, sha = _candidate(tmp_path, "def test_probe(): pass\n")
	isolated_roots: list[Path] = []
	original_worktree = pytest_ci._source_worktree

	@contextmanager
	def observe_worktree(root: Path, source_sha: str) -> Iterator[Path]:
		with original_worktree(root, source_sha) as isolated:
			isolated_roots.append(isolated)
			yield isolated
			assert not list((isolated / "temp").glob("junit-*"))

	def interrupted(command: list[str], **_kwargs: object) -> tuple[int, bytes, bytes]:
		raw = Path(command[command.index("--junitxml") + 1])
		raw.write_text('<testsuites>secret=private-value</testsuites>')
		raise EditError("deadline")
	monkeypatch.setattr(pytest_ci, "_source_worktree", observe_worktree)
	monkeypatch.setattr(pytest_ci, "run_process", interrupted)
	output = tmp_path / "evidence"
	result = pytest_ci.run_tests(prepared, output, source_sha=sha)
	assert result["status"] == "error"
	assert not list(output.glob("*.xml"))
	assert len(isolated_roots) == 1
	assert not isolated_roots[0].exists()


def test_pytest_receives_selected_policy_without_parent_pr_history(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
	from quality_gate.pytest_ci import run_tests

	policy_root = tmp_path / "selected-policy"
	prepared, sha = _candidate(tmp_path, f'''import os
def test_environment():
    assert os.environ["QUALITY_GATE_POLICY_ROOT"] == {str(policy_root)!r}
    assert not {{"GITHUB_EVENT_PATH", "GITHUB_BASE_REF", "GITHUB_SHA"}} & os.environ.keys()
''')
	monkeypatch.setenv("QUALITY_GATE_POLICY_ROOT", str(tmp_path / "unrelated-policy"))
	for key in ("GITHUB_EVENT_PATH", "GITHUB_BASE_REF", "GITHUB_SHA"):
		monkeypatch.setenv(key, "unrelated-parent-pr")
	result = run_tests(prepared, tmp_path / "evidence", source_sha=sha)
	assert result["status"] == "passed", result
	assert result["components"][0]["totals"]["tests"] == 1


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
@pytest.mark.parametrize("entity", ['"expanded"', 'SYSTEM "file:///unreadable"'])
def test_junit_document_types_are_rejected_before_entity_expansion(
	tmp_path: Path, encoding: str, entity: str,
) -> None:
	from quality_gate.pytest_ci import _public_report

	raw = tmp_path / "raw.xml"
	raw.write_bytes((
		f'<!DOCTYPE testsuites [<!ENTITY payload {entity}>]>'
		'<testsuites><testsuite name="&payload;"/></testsuites>'
	).encode(encoding))
	public = tmp_path / "public.xml"
	with pytest.raises(ValueError, match="document types are forbidden"):
		_public_report(raw, public)
	assert not public.exists()
