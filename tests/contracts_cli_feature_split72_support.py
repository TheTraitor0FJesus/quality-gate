"""Fixtures and helpers for the split feature tests."""

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

import pytest

from quality_gate import __version__
from quality_gate.contracts import (
	MAX_SUPPLEMENTAL_TARGET_MATCHES,
	MAX_SUPPLEMENTAL_TESTS,
	SCHEMA_VERSION,
	CheckResult,
	Finding,
	Status,
	Verdict,
	fingerprint_secret,
	load_manifest,
)
from quality_gate.distribution import PolicyCache

REPOSITORY = Path(__file__).resolve().parents[1]

EXIT_QUALITY_FAILURE = 1

EXIT_UNCHECKED = 2

EXPECTED_JAVASCRIPT_FILE_KIB = 100

EXPECTED_CSS_FILE_KIB = 50

EXPECTED_JAVASCRIPT_TOTAL_KIB = 250

EXPECTED_CSS_TOTAL_KIB = 100

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


def _manifest(
	*,
	python: bool = False,
	web: str = "",
	documents: list[str] | None = None,
	waiver: str = "",
) -> str:
	domains = ["repository"]
	if python:
		domains.append("python")
	if web:
		domains.append("web")
	domain_literal = json.dumps(domains)
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
domains = {domain_literal}
required_documents = {document_literal}

[repository.limits]
max_blob_size_mib = 5

[repository.defaults]
command_timeout_seconds = 120
test_timeout_seconds = 300
gate_timeout_seconds = 600
{component}{web}{waiver}"""


def _web_component(
	*,
	name: str = "frontend",
	root: str = "assets",
	javascript: str = '["js/**/*.js"]',
	css: str = '["css/**/*.css"]',
	exclude: str = "[]",
	limits: str = "",
) -> str:
	return f"""
[[web]]
name = "{name}"
root = "{root}"
javascript = {javascript}
css = {css}
exclude = {exclude}
{limits}
"""


def _supplemental_test(
	*,
	name: str = "node-functional",
	runner: str = "node-test",
	targets: str = '["tests/node/**/*.test.js"]',
) -> str:
	return f'''
[[supplemental_tests]]
name = "{name}"
runner = "{runner}"
targets = {targets}
'''


__all__ = [
	"CheckResult",
	"EXIT_QUALITY_FAILURE",
	"EXIT_UNCHECKED",
	"EXPECTED_CSS_FILE_KIB",
	"EXPECTED_CSS_TOTAL_KIB",
	"EXPECTED_JAVASCRIPT_FILE_KIB",
	"EXPECTED_JAVASCRIPT_TOTAL_KIB",
	"Finding",
	"MAX_SUPPLEMENTAL_TARGET_MATCHES",
	"MAX_SUPPLEMENTAL_TESTS",
	"Path",
	"PolicyCache",
	"REPOSITORY",
	"SCHEMA_VERSION",
	"Status",
	"VALID_WORKFLOW",
	"Verdict",
	"__version__",
	"_git",
	"_init_and_stage",
	"_manifest",
	"_run",
	"_stage_lesson_repository",
	"_supplemental_test",
	"_web_component",
	"date",
	"fingerprint_secret",
	"hashlib",
	"json",
	"load_manifest",
	"os",
	"pytest",
	"shutil",
	"subprocess",
	"sys",
	"timedelta",
	"tomllib",
]
