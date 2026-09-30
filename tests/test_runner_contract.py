"""Prompt-contract checks and manifest/Node fixtures with locally controlled sequencing."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from time import perf_counter

import pytest

from quality_gate.contracts import load_manifest

REPOSITORY = Path(__file__).resolve().parents[1]


def _selected_settings_root(environment: Mapping[str, str], profile: Path) -> Path:
	configured_root = environment.get("CODEX_HOME")
	if configured_root:
		return Path(configured_root)
	return profile / ".codex"


def _test_runner_instructions(settings_root: Path) -> str:
	path = settings_root / "agents" / "test-runner.toml"
	if not path.is_file():
		pytest.skip(f"typed test-runner configuration is missing under selected settings root: {path}")
	document = tomllib.loads(path.read_text(encoding="utf-8"))
	if "developer_instructions" in document:
		return document["developer_instructions"]
	return document["skills"]["config"][-1]["developer_instructions"]


def _write_runner_config(settings_root: Path, instructions: str) -> None:
	config = settings_root / "agents" / "test-runner.toml"
	config.parent.mkdir(parents=True, exist_ok=True)
	config.write_text(f'developer_instructions = """{instructions}"""\n', encoding="utf-8")


def test_explicit_settings_root_selects_runner_instructions(tmp_path: Path) -> None:
	selected_root = tmp_path / "selected"
	profile_root = tmp_path / "profile"
	_write_runner_config(selected_root, "selected root")
	_write_runner_config(profile_root / ".codex", "profile fallback")

	assert _selected_settings_root({"CODEX_HOME": str(selected_root)}, profile_root) == selected_root
	assert _test_runner_instructions(selected_root) == "selected root"


def test_profile_settings_root_is_used_when_codex_home_is_unset(tmp_path: Path) -> None:
	profile = tmp_path / "profile"
	selected_root = profile / ".codex"
	_write_runner_config(selected_root, "profile fallback")

	assert _selected_settings_root({}, profile) == selected_root
	assert _test_runner_instructions(selected_root) == "profile fallback"


def test_missing_explicit_settings_root_does_not_fall_back_to_profile(tmp_path: Path) -> None:
	missing_root = tmp_path / "missing-selected-root"
	profile_root = tmp_path / "profile" / ".codex"
	_write_runner_config(profile_root, "must not be read")
	selected_root = _selected_settings_root({"CODEX_HOME": str(missing_root)}, tmp_path / "profile")

	with pytest.raises(pytest.skip.Exception, match="typed test-runner configuration is missing"):
		_test_runner_instructions(selected_root)


def test_prompt_contract_orders_supplementals_before_the_staged_gate() -> None:
	settings_root = _selected_settings_root(os.environ, Path.home())
	instructions = _test_runner_instructions(settings_root)
	preflight = instructions.index("first select `quality-gate --root <repo> validate`")
	declarations = instructions.index("read only the validated manifest's `supplemental_tests`")
	expansion = instructions.index("expand each validated repository-relative target")
	collection = instructions.index("Run every declaration even after an earlier failure")
	gate = instructions.index("always run the staged `quality-gate --root <repo> check`")

	assert preflight < declarations < expansion < collection < gate
	assert "A supplemental failure never suppresses the later staged gate" in instructions


def test_manifestless_full_scope_prompt_contract_keeps_ordinary_suite_discovery() -> None:
	settings_root = _selected_settings_root(os.environ, Path.home())
	instructions = _test_runner_instructions(settings_root)

	assert "For `full` without `quality-gate.toml`" in instructions
	assert "ordinary full suite" in instructions
	assert "Do not discover or run an ordinary full suite in this branch" in instructions


def _git(root: Path, *arguments: str) -> None:
	subprocess.run(
		["git", "-C", str(root), *arguments],
		check=True,
		capture_output=True,
		text=True,
	)


def _runner_fixture(root: Path) -> None:
	(root / "tests").mkdir()
	(root / "tests/failing.test.js").write_text(
		"const fs = require('node:fs');\n"
		"const test = require('node:test');\n"
		"test('failure is collected', () => {\n"
		"  fs.appendFileSync('supplemental.log', 'failure\\n');\n"
		"  throw new Error('expected supplemental failure');\n"
		"});\n",
		encoding="utf-8",
	)
	(root / "tests/passing.test.js").write_text(
		"const fs = require('node:fs');\n"
		"const test = require('node:test');\n"
		"test('later declarations still run', () => {\n"
		"  fs.appendFileSync('supplemental.log', 'success\\n');\n"
		"});\n",
		encoding="utf-8",
	)
	(root / "quality-gate.toml").write_text(
		"""waivers = []

[quality]
schema = 2
policy_release = "v2.0.4"

[repository]
name = "runner-fixture"
domains = ["repository"]
required_documents = ["quality-gate.toml"]

[repository.limits]
max_blob_size_mib = 5

[repository.defaults]
command_timeout_seconds = 120
test_timeout_seconds = 300
gate_timeout_seconds = 600

[[supplemental_tests]]
name = "failing-node"
runner = "node-test"
targets = ["tests/failing.test.js"]

[[supplemental_tests]]
name = "passing-node"
runner = "node-test"
targets = ["tests/passing.test.js"]
""",
		encoding="utf-8",
	)
	(root / "supplemental.log").write_text("", encoding="utf-8")
	workflow = root / ".github" / "workflows"
	workflow.mkdir(parents=True)
	(workflow / "quality.yml").write_text(
		"""name: Quality gate
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
""",
		encoding="utf-8",
	)
	_git(root, "init", "--quiet")
	_git(root, "config", "user.email", "quality-gate@example.test")
	_git(root, "config", "user.name", "Quality Gate")
	_git(root, "add", "quality-gate.toml", "tests", "supplemental.log", ".github")


def _report_integration_timings(timings: dict[str, float]) -> None:
	setup = {name: value for name, value in timings.items() if name == "fixture setup"}
	calls = {
		name: value
		for name, value in timings.items()
		if name not in {"fixture setup", "fixture teardown"}
	}
	teardown = {name: value for name, value in timings.items() if name == "fixture teardown"}
	if not setup or not calls or not teardown:
		return
	slowest = {
		"setup": max(setup.items(), key=lambda item: item[1]),
		"call": max(calls.items(), key=lambda item: item[1]),
		"teardown": max(teardown.items(), key=lambda item: item[1]),
	}
	print(
		"integration timing evidence: "
		+ "; ".join(
			f"{phase}={name} ({duration:.3f}s)" for phase, (name, duration) in slowest.items()
		)
	)
	slow_operations = {name: value for name, value in timings.items() if value > 2}
	if not slow_operations:
		return
	print(
		"slow integration operations: "
		+ "; ".join(
			f"{name}={duration:.3f}s owner=test-runner contract "
			"classification=mutable test state or behavior under test "
			"impact=slower focused verification next_action=keep the scope function-scoped"
			for name, duration in slow_operations.items()
		)
	)


def test_node_fixture_runs_supplementals_before_a_locally_authored_gate_stub() -> None:
	"""Exercise Node fixtures and local sequencing; this does not invoke the typed runner."""
	temporary = tempfile.TemporaryDirectory(prefix="quality-gate-runner-contract-")
	timings: dict[str, float] = {}
	try:
		root = Path(temporary.name)
		started = perf_counter()
		_runner_fixture(root)
		timings["fixture setup"] = perf_counter() - started

		started = perf_counter()
		preflight = subprocess.run(
			[
				sys.executable,
				"-m",
				"quality_gate.cli",
				"--root",
				str(root),
				"validate",
			],
			cwd=REPOSITORY,
			capture_output=True,
			text=True,
		)
		timings["manifest preflight"] = perf_counter() - started
		assert preflight.returncode == 0, preflight.stdout + preflight.stderr

		manifest = load_manifest(root)
		results: list[int] = []
		for declaration in manifest.supplemental_tests:
			matches = sorted(
				{
					match.relative_to(root).as_posix()
					for target in declaration.targets
					for match in root.glob(target)
				}
			)
			started = perf_counter()
			result = subprocess.run(
				["node", "--test", *matches],
				cwd=root,
				capture_output=True,
				text=True,
			)
			timings[f"supplemental {declaration.name}"] = perf_counter() - started
			results.append(result.returncode)

		gate_script = root / "staged_gate.py"
		gate_script.write_text(
			"from pathlib import Path\n"
			"import sys\n"
			"root = Path(sys.argv[1])\n"
			"if (root / 'supplemental.log').read_text(encoding='utf-8') != 'failure\\nsuccess\\n':\n"
			"    raise SystemExit(1)\n"
			"(root / 'staged-gate.log').write_text('ran\\n', encoding='utf-8')\n",
			encoding="utf-8",
		)
		started = perf_counter()
		gate = subprocess.run(
			[sys.executable, str(gate_script), str(root)],
			cwd=REPOSITORY,
			capture_output=True,
			text=True,
		)
		timings["local staged-gate stub"] = perf_counter() - started
		supplemental_log = (root / "supplemental.log").read_text(encoding="utf-8")
		gate_log = (root / "staged-gate.log").read_text(encoding="utf-8")
	finally:
		started = perf_counter()
		temporary.cleanup()
		timings["fixture teardown"] = perf_counter() - started
		_report_integration_timings(timings)

	assert results == [1, 0]
	assert supplemental_log == "failure\nsuccess\n"
	assert gate.returncode == 0, gate.stdout + gate.stderr
	assert gate_log == "ran\n"
