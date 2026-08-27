"""CLI-level tests for the schema, verdict, migration, and waiver contracts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from datetime import date, timedelta
from pathlib import Path

from quality_gate.contracts import (
	SCHEMA_VERSION,
	CheckResult,
	Finding,
	Status,
	Verdict,
	fingerprint_secret,
)
from quality_gate.distribution import PolicyCache

REPOSITORY = Path(__file__).resolve().parents[1]
EXIT_QUALITY_FAILURE = 1
EXIT_UNCHECKED = 2
VALID_WORKFLOW = """name: Quality gate
on:
  pull_request:
  push:
permissions: read
concurrency:
  group: quality-${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true
jobs:
  quality-gate:
    name: quality-gate
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@0123456789abcdef0123456789abcdef01234567
"""


def _run(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["PYTHONPATH"] = str(REPOSITORY) + os.pathsep + env.get("PYTHONPATH", "")
	env["PYTHONIOENCODING"] = "utf-8"
	if arguments and arguments[0] in {"check", "audit"}:
		local_appdata = root / ".test-localappdata"
		cache_root = local_appdata / "quality-gate"
		source = root / ".test-policy-release"
		source.mkdir(exist_ok=True)
		scanner = shutil.which("gitleaks")
		if scanner is None:
			policy_root = os.environ.get("QUALITY_GATE_POLICY_ROOT")
			if policy_root:
				for candidate in (
					Path(policy_root) / "gitleaks",
					Path(policy_root) / "gitleaks.exe",
				):
					if candidate.is_file():
						scanner = str(candidate)
						break
		assert scanner is not None, "Gitleaks is required for CLI contract tests"
		shutil.copy2(scanner, source / "gitleaks.exe")
		(source / "quality_gate-2.0.0-py3-none-any.whl").write_bytes(b"test wheel")
		files = ["quality_gate-2.0.0-py3-none-any.whl", "gitleaks.exe"]
		digests = {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in files}
		(source / "release.toml").write_text(
			f'''[release]
version = "v2.0.0"

[[release.files]]
path = "{files[0]}"
sha256 = "{digests[files[0]]}"

[[release.tools]]
name = "gitleaks"
version = "8.30.1"
path = "{files[1]}"
sha256 = "{digests[files[1]]}"
''',
			encoding="utf-8",
		)
		PolicyCache(cache_root).sync(source)
		env["LOCALAPPDATA"] = str(local_appdata)
		env["XDG_CACHE_HOME"] = str(local_appdata)
	return subprocess.run(
		[sys.executable, "-m", "quality_gate", "--root", str(root), *arguments],
		cwd=root,
		env=env,
		capture_output=True,
		text=True,
		check=False,
	)


def _git(
	root: Path, *arguments: str, input_data: str | None = None
) -> subprocess.CompletedProcess[str]:
	env = os.environ.copy()
	env["GIT_CONFIG_GLOBAL"] = str(root / "missing-global-config")
	env["GIT_CONFIG_NOSYSTEM"] = "1"
	return subprocess.run(
		["git", *arguments],
		cwd=root,
		env=env,
		capture_output=True,
		input=input_data,
		text=True,
		check=False,
	)


def _init_and_stage(root: Path, *paths: str) -> None:
	workflow = root / ".github" / "workflows" / "quality.yml"
	workflow.parent.mkdir(parents=True)
	workflow.write_text(VALID_WORKFLOW, encoding="utf-8")
	assert _git(root, "init").returncode == 0
	assert _git(root, "add", *paths, ".github/workflows/quality.yml").returncode == 0


def _stage_lesson_repository(
	root: Path, *, status: str, adaptation: str = "", evidence: str = ""
) -> None:
	(root / "quality-gate.toml").write_text(_manifest(), encoding="utf-8")
	(root / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	lessons = root / "lessons"
	lessons.mkdir()
	(lessons / "incident.md").write_text(
		f"""---
id: incident-1
status: {status}
incident: A defect escaped the gate.
expected_layer: repository check
miss_cause: The case was not covered.
adaptation: {adaptation}
evidence: {evidence}
---

# Lesson

The incident is recorded.
""",
		encoding="utf-8",
	)
	_init_and_stage(root, "quality-gate.toml", "AGENTS.md", "lessons/incident.md")


def _manifest(*, python: bool = False, documents: list[str] | None = None, waiver: str = "") -> str:
	domains = '["repository", "python"]' if python else '["repository"]'
	document_literal = json.dumps(documents or ["AGENTS.md"])
	waiver_root = "" if waiver else "waivers = []\n\n"
	component = (
		"""
[[python]]
name = "app"
path = "quality_gate"
python_version = "3.12"
dependency_inputs = []
test_paths = ["tests"]
tests_applicable = true
timeout_seconds = 300
"""
		if python
		else ""
	)
	return f"""{waiver_root}
[quality]
schema = 2
policy_release = "v2.0.0"

[repository]
name = "fixture"
domains = {domains}
required_documents = {document_literal}

[repository.limits]
max_blob_size_mib = 5

[repository.defaults]
command_timeout_seconds = 120
test_timeout_seconds = 300
gate_timeout_seconds = 600
{component}{waiver}"""


def test_validate_accepts_non_python_manifest(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(_manifest(), encoding="utf-8")
	result = _run(tmp_path, "validate")

	assert result.returncode == 0
	assert "schema 2" in result.stdout


def test_validate_accepts_python_manifest(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(_manifest(python=True), encoding="utf-8")
	result = _run(tmp_path, "validate")

	assert result.returncode == 0


def test_format_cli_requires_explicit_paths(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(_manifest(python=True), encoding="utf-8")
	result = _run(tmp_path, "format")

	assert result.returncode == EXIT_UNCHECKED
	assert "the following arguments are required: paths" in result.stderr


def test_validate_rejects_schema_one_and_migrate_is_read_only(tmp_path: Path) -> None:
	manifest = 'python = []\n\n[quality]\nschema = 1\npolicy = "quality-gate-v1"\n'
	path = tmp_path / "quality-gate.toml"
	path.write_text(manifest, encoding="utf-8")
	before = path.read_bytes()

	validation = _run(tmp_path, "validate")
	migration = _run(tmp_path, "migrate")

	assert validation.returncode == EXIT_UNCHECKED
	assert "migrate" in validation.stdout
	assert migration.returncode == 0
	assert "schema = 2" in migration.stdout
	assert path.read_bytes() == before
	proposal = tomllib.loads(migration.stdout.split("\n", 1)[1])
	assert proposal["quality"]["schema"] == SCHEMA_VERSION


def test_migrate_preserves_a_python_component(tmp_path: Path) -> None:
	legacy = '[quality]\nschema = 1\n\n[[python]]\npath = "app"\ntests = "tests"\n'
	(tmp_path / "quality-gate.toml").write_text(legacy, encoding="utf-8")

	result = _run(tmp_path, "migrate")
	proposal = tomllib.loads(result.stdout.split("\n", 1)[1])

	assert result.returncode == 0
	assert proposal["python"][0]["path"] == "app"
	assert proposal["python"][0]["test_paths"] == ["tests"]


def test_check_reports_quality_failure_with_exit_one(tmp_path: Path) -> None:
	manifest = tmp_path / "quality-gate.toml"
	manifest.write_text(_manifest(documents=["AGENTS.md", "docs/missing.md"]), encoding="utf-8")
	_init_and_stage(tmp_path, manifest.name)
	result = _run(tmp_path, "check")

	assert result.returncode == EXIT_QUALITY_FAILURE
	assert "manifest.documents: failed" in result.stdout
	assert "restore docs/missing.md" in result.stdout


def test_check_cli_blocks_when_python_runtime_is_unavailable(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(_manifest(python=True), encoding="utf-8")
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(tmp_path / "quality_gate").mkdir()
	(tmp_path / "tests").mkdir()
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md")

	result = _run(tmp_path, "check")

	assert result.returncode == EXIT_UNCHECKED
	assert "unchecked" in result.stdout


def test_check_redacts_credential_shaped_required_document(tmp_path: Path) -> None:
	secret = "super-secret-token-123456"
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=[f"token={secret}.txt"]), encoding="utf-8"
	)
	_init_and_stage(tmp_path, "quality-gate.toml")
	result = _run(tmp_path, "check", "--verbose")

	assert result.returncode == EXIT_QUALITY_FAILURE
	assert secret not in result.stdout
	assert "<REDACTED>" in result.stdout


def test_check_verifies_the_staged_candidate_instead_of_the_worktree(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=["AGENTS.md", "candidate.md"]), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(tmp_path / "candidate.md").write_text("staged content\n", encoding="utf-8")
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", "candidate.md")
	(tmp_path / "candidate.md").write_text("", encoding="utf-8")

	result = _run(tmp_path, "check")

	assert result.returncode == 0
	assert "manifest.documents: passed" in result.stdout


def test_check_rejects_a_staged_empty_candidate_even_when_worktree_is_repaired(
	tmp_path: Path,
) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=["AGENTS.md", "candidate.md"]), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(tmp_path / "candidate.md").write_text("valid\n", encoding="utf-8")
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", "candidate.md")
	(tmp_path / "candidate.md").write_text("", encoding="utf-8")
	assert _git(tmp_path, "add", "candidate.md").returncode == 0
	(tmp_path / "candidate.md").write_text("valid in worktree\n", encoding="utf-8")

	result = _run(tmp_path, "check")

	assert result.returncode == EXIT_QUALITY_FAILURE
	assert "candidate.md" in result.stdout


def test_check_uses_a_staged_rename_at_the_cli_boundary(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=["AGENTS.md", "moved.md"]), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(tmp_path / "renamed.md").write_text("candidate\n", encoding="utf-8")
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", "renamed.md")
	assert _git(tmp_path, "mv", "renamed.md", "moved.md").returncode == 0

	result = _run(tmp_path, "check")

	assert result.returncode == 0
	assert "manifest.documents: passed" in result.stdout


def test_check_reports_a_staged_deletion_at_the_cli_boundary(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=["AGENTS.md", "deleted.md"]), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	(tmp_path / "deleted.md").write_text("candidate\n", encoding="utf-8")
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md", "deleted.md")
	assert _git(tmp_path, "rm", "--cached", "deleted.md").returncode == 0

	result = _run(tmp_path, "check")

	assert result.returncode == EXIT_QUALITY_FAILURE
	assert "deleted.md" in result.stdout


def test_check_reports_unmerged_index_as_unchecked(tmp_path: Path) -> None:
	(tmp_path / "quality-gate.toml").write_text(
		_manifest(documents=["AGENTS.md"]), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("base\n", encoding="utf-8")
	_init_and_stage(tmp_path, "quality-gate.toml", "AGENTS.md")
	assert (
		_git(
			tmp_path,
			"-c",
			"user.name=Quality Gate Test",
			"-c",
			"user.email=quality-gate@example.test",
			"commit",
			"-m",
			"base",
		).returncode
		== 0
	)
	assert _git(tmp_path, "checkout", "-b", "other").returncode == 0
	(tmp_path / "AGENTS.md").write_text("other\n", encoding="utf-8")
	assert _git(tmp_path, "add", "AGENTS.md").returncode == 0
	assert (
		_git(
			tmp_path,
			"-c",
			"user.name=Quality Gate Test",
			"-c",
			"user.email=quality-gate@example.test",
			"commit",
			"-m",
			"other",
		).returncode
		== 0
	)
	assert _git(tmp_path, "checkout", "-b", "mainline", "HEAD~1").returncode == 0
	(tmp_path / "AGENTS.md").write_text("mainline\n", encoding="utf-8")
	assert _git(tmp_path, "add", "AGENTS.md").returncode == 0
	assert (
		_git(
			tmp_path,
			"-c",
			"user.name=Quality Gate Test",
			"-c",
			"user.email=quality-gate@example.test",
			"commit",
			"-m",
			"mainline",
		).returncode
		== 0
	)
	assert _git(tmp_path, "read-tree", "-m", "HEAD~1", "HEAD", "other").returncode == 0
	assert _git(tmp_path, "ls-files", "--unmerged", "-z").stdout

	result = _run(tmp_path, "check")

	assert result.returncode == EXIT_UNCHECKED
	assert "candidate.snapshot: unchecked" in result.stdout


def test_invalid_typed_waiver_is_rejected_without_secret_value(tmp_path: Path) -> None:
	secret = "ghp_example-secret-value"
	waiver = f"""
[[waivers]]
kind = "secret"
check_id = "secrets.candidate"
target = "*.env"
reason = "known fixture"
approved_by = "dependabot"
reviewed_on = "{date.today().isoformat()}"
expires_on = "{(date.today() + timedelta(days=1)).isoformat()}"
value = "{secret}"
"""
	(tmp_path / "quality-gate.toml").write_text(_manifest(waiver=waiver), encoding="utf-8")
	result = _run(tmp_path, "validate")

	assert result.returncode == EXIT_UNCHECKED
	assert secret not in result.stdout


def test_exact_current_secret_waiver_is_accepted(tmp_path: Path) -> None:
	secret = "fixture-secret-value"
	waiver = f"""
[[waivers]]
kind = "secret"
check_id = "secrets.candidate"
target = "fixtures/example.txt:1"
reason = "Synthetic fixture is intentionally retained for a security test."
approved_by = "Human Reviewer"
reviewed_on = "{date.today().isoformat()}"
expires_on = "{(date.today() + timedelta(days=1)).isoformat()}"
fingerprint = "{fingerprint_secret(secret)}"
"""
	(tmp_path / "quality-gate.toml").write_text(_manifest(waiver=waiver), encoding="utf-8")
	result = _run(tmp_path, "validate")

	assert result.returncode == 0
	assert secret not in result.stdout


def test_verdict_exit_precedence_and_finding_invariants() -> None:
	result = CheckResult(
		check_id="repository.findings",
		status=Status.FAILED,
		summary="findings",
		findings=(Finding(path="a.txt", message="finding"),),
		recovery_action="repair the findings",
	)
	unchecked = CheckResult(
		check_id="runtime.available",
		status=Status.UNCHECKED,
		summary="runtime unavailable",
		recovery_action="restore the runtime",
	)

	assert Verdict((result,)).exit_code == EXIT_QUALITY_FAILURE
	assert Verdict((result, unchecked)).exit_code == EXIT_UNCHECKED


def test_audit_aggregates_full_history_and_learning_state(tmp_path: Path) -> None:
	_stage_lesson_repository(
		tmp_path, status="learned", adaptation="Added a regression check.", evidence="CI run 123."
	)

	result = _run(tmp_path, "audit")

	assert result.returncode == 0
	assert "lessons.learning: passed" in result.stdout
	assert "secrets.audit: passed" in result.stdout


def test_open_lesson_does_not_block_ordinary_check(tmp_path: Path) -> None:
	_stage_lesson_repository(tmp_path, status="open")

	result = _run(tmp_path, "check")

	assert result.returncode == 0
	assert "lessons.learning: passed" in result.stdout


def test_unlearned_lesson_blocks_audit(tmp_path: Path) -> None:
	_stage_lesson_repository(tmp_path, status="open")

	result = _run(tmp_path, "audit")

	assert result.returncode == EXIT_QUALITY_FAILURE
	assert "lessons.learning: failed" in result.stdout
	assert "record the gate adaptation and verification evidence" in result.stdout
