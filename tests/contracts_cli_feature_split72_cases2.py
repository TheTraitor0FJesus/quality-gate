"""Feature test partition."""

from __future__ import annotations

from contracts_cli_feature_split72_support import (
	EXIT_QUALITY_FAILURE as EXIT_QUALITY_FAILURE,
)
from contracts_cli_feature_split72_support import (
	EXIT_UNCHECKED as EXIT_UNCHECKED,
)
from contracts_cli_feature_split72_support import (
	EXPECTED_CSS_FILE_KIB as EXPECTED_CSS_FILE_KIB,
)
from contracts_cli_feature_split72_support import (
	EXPECTED_CSS_TOTAL_KIB as EXPECTED_CSS_TOTAL_KIB,
)
from contracts_cli_feature_split72_support import (
	EXPECTED_JAVASCRIPT_FILE_KIB as EXPECTED_JAVASCRIPT_FILE_KIB,
)
from contracts_cli_feature_split72_support import (
	EXPECTED_JAVASCRIPT_TOTAL_KIB as EXPECTED_JAVASCRIPT_TOTAL_KIB,
)
from contracts_cli_feature_split72_support import (
	MAX_SUPPLEMENTAL_TARGET_MATCHES as MAX_SUPPLEMENTAL_TARGET_MATCHES,
)
from contracts_cli_feature_split72_support import (
	MAX_SUPPLEMENTAL_TESTS as MAX_SUPPLEMENTAL_TESTS,
)
from contracts_cli_feature_split72_support import (
	REPOSITORY as REPOSITORY,
)
from contracts_cli_feature_split72_support import (
	SCHEMA_VERSION as SCHEMA_VERSION,
)
from contracts_cli_feature_split72_support import (
	VALID_WORKFLOW as VALID_WORKFLOW,
)
from contracts_cli_feature_split72_support import (
	CheckResult as CheckResult,
)
from contracts_cli_feature_split72_support import (
	Finding as Finding,
)
from contracts_cli_feature_split72_support import (
	Path as Path,
)
from contracts_cli_feature_split72_support import (
	PolicyCache as PolicyCache,
)
from contracts_cli_feature_split72_support import (
	Status as Status,
)
from contracts_cli_feature_split72_support import (
	Verdict as Verdict,
)
from contracts_cli_feature_split72_support import (
	__version__ as __version__,
)
from contracts_cli_feature_split72_support import (
	_git as _git,
)
from contracts_cli_feature_split72_support import (
	_init_and_stage as _init_and_stage,
)
from contracts_cli_feature_split72_support import (
	_manifest as _manifest,
)
from contracts_cli_feature_split72_support import (
	_run as _run,
)
from contracts_cli_feature_split72_support import (
	_stage_lesson_repository as _stage_lesson_repository,
)
from contracts_cli_feature_split72_support import (
	_supplemental_test as _supplemental_test,
)
from contracts_cli_feature_split72_support import (
	_web_component as _web_component,
)
from contracts_cli_feature_split72_support import (
	date as date,
)
from contracts_cli_feature_split72_support import (
	fingerprint_secret as fingerprint_secret,
)
from contracts_cli_feature_split72_support import (
	hashlib as hashlib,
)
from contracts_cli_feature_split72_support import (
	json as json,
)
from contracts_cli_feature_split72_support import (
	load_manifest as load_manifest,
)
from contracts_cli_feature_split72_support import (
	os as os,
)
from contracts_cli_feature_split72_support import (
	pytest as pytest,
)
from contracts_cli_feature_split72_support import (
	shutil as shutil,
)
from contracts_cli_feature_split72_support import (
	subprocess as subprocess,
)
from contracts_cli_feature_split72_support import (
	sys as sys,
)
from contracts_cli_feature_split72_support import (
	timedelta as timedelta,
)
from contracts_cli_feature_split72_support import (
	tomllib as tomllib,
)


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
	assert "manifest.documents:" not in result.stdout
	assert "python.component_" not in result.stdout


def test_check_reports_only_the_manifest_error_when_manifest_is_unavailable(
	tmp_path: Path,
) -> None:
	(tmp_path / "quality-gate.toml").write_text("quality = [", encoding="utf-8")
	_init_and_stage(tmp_path, "quality-gate.toml")

	result = _run(tmp_path, "check")

	assert result.returncode == EXIT_UNCHECKED
	assert result.stdout.startswith("Quality Gate: 2\n")
	assert "manifest.contract: unchecked" in result.stdout
	assert "repository.git." not in result.stdout
	assert "python.component_" not in result.stdout


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
	assert "unknown field 'value'" in result.stdout
	assert "waivers[0]" in result.stdout
	assert secret not in result.stdout


@pytest.mark.parametrize(
	("field", "invalid_value", "error_path", "error_message"),
	[
		("target", "*.env", "waivers[0].target", "must identify one exact target"),
		("approved_by", "dependabot", "waivers[0].approved_by", "must identify a human reviewer"),
		(
			"expires_on",
			-1,
			"waivers[0].expires_on",
			"must be current and on or after review date",
		),
		(
			"reviewed_on",
			1,
			"waivers[0].expires_on",
			"must be current and on or after review date",
		),
		(
			"fingerprint",
			"not-a-sha256-fingerprint",
			"waivers[0].fingerprint",
			"secret waivers require a SHA-256 fingerprint",
		),
	],
	ids=[
		"wildcard-target",
		"bot-approver",
		"expired-waiver",
		"future-review-date",
		"malformed-fingerprint",
	],
)
def test_invalid_secret_waiver_is_rejected_for_specific_rule(
	tmp_path: Path,
	field: str,
	invalid_value: int | str,
	error_path: str,
	error_message: str,
) -> None:
	today = date.today()
	values = {
		"target": "fixtures/example.txt:1",
		"approved_by": "Human Reviewer",
		"reviewed_on": (today - timedelta(days=2)).isoformat(),
		"expires_on": (today + timedelta(days=2)).isoformat(),
		"fingerprint": fingerprint_secret("fixture-secret-value"),
	}
	values[field] = (
		(today + timedelta(days=invalid_value)).isoformat()
		if isinstance(invalid_value, int)
		else invalid_value
	)
	target = values["target"]
	approved_by = values["approved_by"]
	reviewed_on = values["reviewed_on"]
	expires_on = values["expires_on"]
	fingerprint = values["fingerprint"]
	waiver = f"""
[[waivers]]
kind = "secret"
check_id = "secrets.candidate"
target = "{target}"
reason = "known fixture"
approved_by = "{approved_by}"
reviewed_on = "{reviewed_on}"
expires_on = "{expires_on}"
fingerprint = "{fingerprint}"
"""
	(tmp_path / "quality-gate.toml").write_text(_manifest(waiver=waiver), encoding="utf-8")
	result = _run(tmp_path, "validate")

	assert result.returncode == EXIT_UNCHECKED
	assert error_message in result.stdout
	assert error_path in result.stdout


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
	assert result.stdout.splitlines()[0] == "Quality Gate: 0"
	assert "Observed results:" in result.stdout
	assert "lessons.learning: passed" not in result.stdout
	assert "secrets.audit: passed" not in result.stdout


def test_open_lesson_does_not_block_ordinary_check(tmp_path: Path) -> None:
	_stage_lesson_repository(tmp_path, status="open")

	result = _run(tmp_path, "check")

	assert result.returncode == 0
	assert result.stdout.splitlines()[0] == "Quality Gate: 0"
	assert "Observed results:" in result.stdout
	assert "lessons.learning: passed" not in result.stdout


def test_unlearned_lesson_blocks_audit(tmp_path: Path) -> None:
	_stage_lesson_repository(tmp_path, status="open")

	result = _run(tmp_path, "audit")

	assert result.returncode == EXIT_QUALITY_FAILURE
	assert "lessons.learning: failed" in result.stdout
	assert "record the gate adaptation and verification evidence" in result.stdout
