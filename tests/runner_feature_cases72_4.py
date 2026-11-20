"""Feature-preserving test partition."""

from __future__ import annotations

from runner_feature_support72 import (
	EXPECTED_BIOME_INVOCATIONS as EXPECTED_BIOME_INVOCATIONS,
)
from runner_feature_support72 import (
	EXPECTED_NATIVE_CACHE_EXECUTIONS as EXPECTED_NATIVE_CACHE_EXECUTIONS,
)
from runner_feature_support72 import (
	EXPECTED_TIMEOUT_WAIT_CALLS as EXPECTED_TIMEOUT_WAIT_CALLS,
)
from runner_feature_support72 import (
	FIXTURES as FIXTURES,
)
from runner_feature_support72 import (
	UNCHECKED_EXIT as UNCHECKED_EXIT,
)
from runner_feature_support72 import (
	AbstractContextManager as AbstractContextManager,
)
from runner_feature_support72 import (
	AdmissionError as AdmissionError,
)
from runner_feature_support72 import (
	ExternalTool as ExternalTool,
)
from runner_feature_support72 import (
	GateOperation as GateOperation,
)
from runner_feature_support72 import (
	Path as Path,
)
from runner_feature_support72 import (
	PreparedEnvironment as PreparedEnvironment,
)
from runner_feature_support72 import (
	ReleaseFile as ReleaseFile,
)
from runner_feature_support72 import (
	ReleaseManifest as ReleaseManifest,
)
from runner_feature_support72 import (
	RuntimeIdentity as RuntimeIdentity,
)
from runner_feature_support72 import (
	SimpleNamespace as SimpleNamespace,
)
from runner_feature_support72 import (
	ThreadPoolExecutor as ThreadPoolExecutor,
)
from runner_feature_support72 import (
	_assert_biome_invocations as _assert_biome_invocations,
)
from runner_feature_support72 import (
	_fake_biome_command as _fake_biome_command,
)
from runner_feature_support72 import (
	_fake_candidate_snapshot as _fake_candidate_snapshot,
)
from runner_feature_support72 import (
	_web_fixture as _web_fixture,
)
from runner_feature_support72 import (
	_web_manifest as _web_manifest,
)
from runner_feature_support72 import (
	_web_policy as _web_policy,
)
from runner_feature_support72 import (
	_web_prepared as _web_prepared,
)
from runner_feature_support72 import (
	cast as cast,
)
from runner_feature_support72 import (
	hashlib as hashlib,
)
from runner_feature_support72 import (
	io as io,
)
from runner_feature_support72 import (
	isolate_runner_unit_tests_from_host_gate_slots as isolate_runner_unit_tests_from_host_gate_slots,
)
from runner_feature_support72 import (
	json as json,
)
from runner_feature_support72 import (
	native_cache_module as native_cache_module,
)
from runner_feature_support72 import (
	nullcontext as nullcontext,
)
from runner_feature_support72 import (
	operation_module as operation_module,
)
from runner_feature_support72 import (
	os as os,
)
from runner_feature_support72 import (
	pytest as pytest,
)
from runner_feature_support72 import (
	replace as replace,
)
from runner_feature_support72 import (
	runner as runner,
)
from runner_feature_support72 import (
	shutil as shutil,
)
from runner_feature_support72 import (
	subprocess as subprocess,
)
from runner_feature_support72 import (
	sys as sys,
)
from runner_feature_support72 import (
	threading as threading,
)
from runner_feature_support72 import (
	time as time,
)
from runner_feature_support72 import (
	tomllib as tomllib,
)
from runner_feature_support72 import (
	use_long_windows_staging_limit_for_gate_temp_workspace as use_long_windows_staging_limit_for_gate_temp_workspace,
)


def test_check_marks_biome_unchecked_when_standalone_binary_is_missing(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path, binary=None)
	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: _web_prepared(policy_root))

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.UNCHECKED] * 2
	assert verdict.exit_code == runner.EXIT_UNCHECKED


def test_check_marks_biome_unchecked_when_standalone_binary_is_corrupt(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	platform = "windows-x64" if os.name == "nt" else "linux-x64"
	entry = tomllib.loads((policy_root / "quality_gate" / "policy" / "biome.toml").read_text())[
		"biome"
	]["platforms"][platform]
	(policy_root / entry["path"]).write_bytes(b"corrupt biome")

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: _web_prepared(policy_root))

	verdict = runner.check(tmp_path)

	biome_results = [result for result in verdict.results if ".biome_" in result.check_id]
	assert [result.status for result in biome_results] == [runner.Status.UNCHECKED] * 2


def test_check_does_not_duplicate_biome_results_after_partial_web_failure(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	policy_root = _web_fixture(tmp_path)
	(tmp_path / "quality-gate.toml").write_text(
		_web_manifest()
		+ '\n[[web]]\nname = "second"\nroot = "assets"\n'
		+ 'javascript = ["js/**/*.js"]\ncss = []\nexclude = []\n',
		encoding="utf-8",
	)
	prepared = _web_prepared(policy_root)
	command_calls = 0

	def fail_first_biome_command(_prepared: object) -> list[str]:
		nonlocal command_calls
		command_calls += 1
		if command_calls == 1:
			raise runner.QualityGateError(
				"first Biome component is unavailable",
				exit_code=runner.EXIT_UNCHECKED,
				recovery_action="restore Biome",
			)
		return ["unused"]

	def fail_temporary_directory(_root: Path) -> object:
		raise OSError("temporary directory unavailable")

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)
	monkeypatch.setattr(runner, "_biome_command", fail_first_biome_command)
	monkeypatch.setattr(runner, "temporary_directory", fail_temporary_directory)

	verdict = runner.check(tmp_path)

	biome_ids = [result.check_id for result in verdict.results if ".biome_" in result.check_id]
	assert biome_ids == [
		"web.component_1.biome_lint",
		"web.component_1.biome_format",
		"web.component_2.biome_lint",
		"web.component_2.biome_format",
	]
	assert all(
		result.status is runner.Status.UNCHECKED
		for result in verdict.results
		if ".biome_" in result.check_id
	)


@pytest.mark.parametrize(
	("relative", "content", "failed_check"),
	(
		("assets/js/app.js", "const unused = 1;\n", "web.component_1.biome_lint"),
		("assets/css/app.css", "a { lint-error: true; }\n", "web.component_1.biome_lint"),
		("assets/css/app.css", "a{bad-format}\n", "web.component_1.biome_format"),
	),
)
def test_check_reports_biome_findings_for_invalid_staged_content(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	relative: str,
	content: str,
	failed_check: str,
) -> None:
	policy_root = _web_fixture(tmp_path)
	prepared = _web_prepared(policy_root)
	_fake_biome_command(tmp_path, monkeypatch)
	(tmp_path / relative).write_text(content, encoding="utf-8")

	monkeypatch.setattr(runner, "candidate_snapshot", _fake_candidate_snapshot)
	monkeypatch.setattr(runner, "prepare", lambda *args, **kwargs: prepared)

	verdict = runner.check(tmp_path)

	biome_results = {
		result.check_id: result.status for result in verdict.results if ".biome_" in result.check_id
	}
	assert biome_results[failed_check] is runner.Status.FAILED
	other_check = next(check_id for check_id in biome_results if check_id != failed_check)
	assert biome_results[other_check] is runner.Status.PASSED
