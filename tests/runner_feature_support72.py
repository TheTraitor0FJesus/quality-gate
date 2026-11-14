"""Shared fixtures and helpers for feature-preserving test partitions."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from quality_gate import native_cache as native_cache_module
from quality_gate import operation as operation_module
from quality_gate import runner
from quality_gate.admission import AdmissionError
from quality_gate.distribution import ExternalTool, ReleaseFile, ReleaseManifest
from quality_gate.launcher import PreparedEnvironment
from quality_gate.operation import GateOperation
from quality_gate.runtime import RuntimeIdentity

FIXTURES = Path(__file__).parent / "fixtures"

EXPECTED_TIMEOUT_WAIT_CALLS = 2

EXPECTED_BIOME_INVOCATIONS = 2

EXPECTED_NATIVE_CACHE_EXECUTIONS = 2

UNCHECKED_EXIT = 2


@pytest.fixture(autouse=True)
def isolate_runner_unit_tests_from_host_gate_slots(monkeypatch: pytest.MonkeyPatch) -> None:
	# Admission behavior is tested explicitly below and in test_native_gate_queue.py.
	monkeypatch.setattr(runner, "gate_admission", lambda: nullcontext())


@pytest.fixture(autouse=True)
def use_long_windows_staging_limit_for_gate_temp_workspace(
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	if os.name == "nt":
		monkeypatch.setattr(native_cache_module, "_WINDOWS_MAX_PATH", 32767)


def _fake_candidate_snapshot(
	root: Path, **_kwargs: object
) -> AbstractContextManager[SimpleNamespace]:
	return nullcontext(SimpleNamespace(root=root))


def _web_manifest(*, javascript: bool = True, css: bool = True) -> str:
	javascript_declaration = 'javascript = ["js/**/*.js"]\n' if javascript else ""
	css_declaration = 'css = ["css/**/*.css"]\n' if css else ""
	return f"""waivers = []

[quality]
schema = 2
policy_release = "v2.0.0"

[repository]
name = "fixture"
domains = ["repository", "web"]
required_documents = ["AGENTS.md"]

[[web]]
name = "frontend"
root = "assets"
{javascript_declaration}{css_declaration}exclude = []
"""


def _web_policy(tmp_path: Path, *, binary: bytes | None = None) -> Path:
	policy_root = tmp_path / "policy-root"
	policy_dir = policy_root / "quality_gate" / "policy"
	shutil.copytree(runner.POLICY_DIR, policy_dir, copy_function=shutil.copyfile)
	if binary is not None:
		inventory = tomllib.loads((policy_dir / "biome.toml").read_text())
		platform = "windows-x64" if os.name == "nt" else "linux-x64"
		entry = inventory["biome"]["platforms"][platform]
		binary_path = policy_root / entry["path"]
		binary_path.write_bytes(binary)
		inventory_text = (policy_dir / "biome.toml").read_text()
		(policy_dir / "biome.toml").write_text(
			inventory_text.replace(entry["sha256"], hashlib.sha256(binary).hexdigest())
		)
	return policy_root


def _web_fixture(
	tmp_path: Path,
	*,
	binary: bytes | None = b"standalone biome",
	javascript: bool = True,
	css: bool = True,
) -> Path:
	(tmp_path / "quality-gate.toml").write_text(
		_web_manifest(javascript=javascript, css=css), encoding="utf-8"
	)
	(tmp_path / "AGENTS.md").write_text("contract\n", encoding="utf-8")
	for relative in ("assets/js/app.js", "assets/css/app.css"):
		asset = tmp_path / relative
		asset.parent.mkdir(parents=True, exist_ok=True)
		content = "const value = 1;\n" if relative.endswith(".js") else "body { color: red; }\n"
		asset.write_text(content, encoding="utf-8")
	return _web_policy(tmp_path, binary=binary)


def _fake_biome_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
	script = tmp_path / "fake_biome.py"
	config = tmp_path / "fake-biome.json"
	config.write_text("{}\n", encoding="utf-8")
	script.write_text(
		"""from pathlib import Path
import sys
import json

args = sys.argv[1:]
with Path(__file__).with_suffix(".jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\\n")
if not args or args[0] != "ci" or "--config-path" not in args:
    raise SystemExit(90)
if "--write" in args or "--fix" in args:
    raise SystemExit(91)
asset_paths = [Path(value) for value in args if value.endswith((".js", ".css"))]
if any(path.name == "undeclared.js" for path in asset_paths):
    raise SystemExit(92)
for path in asset_paths:
    content = path.read_text(encoding="utf-8")
    if "--linter-enabled=true" in args and ("unused" in content or "lint-error" in content):
        print("lint finding")
        raise SystemExit(1)
    if "--formatter-enabled=true" in args and "bad-format" in content:
        print("format finding")
        raise SystemExit(1)
""",
		encoding="utf-8",
	)
	monkeypatch.setattr(
		runner,
		"_biome_command",
		lambda _prepared: [sys.executable, str(script), "ci", "--config-path", str(config)],
	)
	return script.with_suffix(".jsonl")


def _assert_biome_invocations(log_path: Path, expected_assets: set[str]) -> None:
	invocations = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
	assert len(invocations) == EXPECTED_BIOME_INVOCATIONS
	for args in invocations:
		assert "--write" not in args
		assert "--fix" not in args
		assert {value for value in args if value.endswith((".js", ".css"))} == expected_assets
		assert "--assist-enabled=false" in args

	lint_args = next(args for args in invocations if "--linter-enabled=true" in args)
	format_args = next(args for args in invocations if "--formatter-enabled=true" in args)
	assert "--formatter-enabled=false" in lint_args
	assert "--linter-enabled=false" in format_args


def _web_prepared(policy_root: Path) -> SimpleNamespace:
	policy = tomllib.loads((policy_root / "quality_gate" / "policy" / "biome.toml").read_text())[
		"biome"
	]
	platform = "windows-x64" if os.name == "nt" else "linux-x64"
	binary = policy["platforms"][platform]
	return SimpleNamespace(
		policy_root=policy_root,
		release_manifest=ReleaseManifest(
			"v2.0.0",
			(ReleaseFile("quality_gate.whl", "a" * 64),),
			(ExternalTool("biome", policy["version"], binary["path"], binary["sha256"]),),
		),
		runtimes=(),
	)


__all__ = [
	"AbstractContextManager",
	"AdmissionError",
	"EXPECTED_BIOME_INVOCATIONS",
	"EXPECTED_NATIVE_CACHE_EXECUTIONS",
	"EXPECTED_TIMEOUT_WAIT_CALLS",
	"ExternalTool",
	"FIXTURES",
	"GateOperation",
	"Path",
	"PreparedEnvironment",
	"ReleaseFile",
	"ReleaseManifest",
	"RuntimeIdentity",
	"SimpleNamespace",
	"ThreadPoolExecutor",
	"UNCHECKED_EXIT",
	"_assert_biome_invocations",
	"_fake_biome_command",
	"_fake_candidate_snapshot",
	"_web_fixture",
	"_web_manifest",
	"_web_policy",
	"_web_prepared",
	"cast",
	"hashlib",
	"io",
	"isolate_runner_unit_tests_from_host_gate_slots",
	"json",
	"native_cache_module",
	"nullcontext",
	"operation_module",
	"os",
	"pytest",
	"replace",
	"runner",
	"shutil",
	"subprocess",
	"sys",
	"threading",
	"time",
	"tomllib",
	"use_long_windows_staging_limit_for_gate_temp_workspace",
]
