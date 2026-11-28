"""Independent full pytest verification for shared CI and installed product checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import __version__
from .contracts import ValidationError, redact
from .distribution import DistributionError
from .edit_process import run_process
from .edit_protocol import EditError
from .launcher import PreparedEnvironment, prepare, prepare_bootstrap
from .runtime import RuntimeUnavailable

MAX_JUNIT_BYTES = 16 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
CLEANUP_SECONDS = 10


class _JUnitBuilder(ET.TreeBuilder):
	def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
		raise ValueError("JUnit document types are forbidden")


def _public_report(path: Path, target: Path) -> ET.Element:
	with path.open("rb") as stream:
		data = stream.read(MAX_JUNIT_BYTES + 1)
	if len(data) > MAX_JUNIT_BYTES:
		raise ValueError("JUnit exceeds its evidence limit")
	# The bounded input uses a builder that rejects every DTD before entity expansion.
	parser = ET.XMLParser(target=_JUnitBuilder())  # noqa: S314
	parser.feed(data)
	root = parser.close()
	for element in root.iter():
		element.text = redact(element.text) if element.text else element.text
		element.tail = redact(element.tail) if element.tail else element.tail
		for key, value in element.attrib.items():
			element.set(key, redact(value))
	ET.ElementTree(root).write(target, encoding="utf-8", xml_declaration=True)
	return root


def _exact_source(root: Path, sha: str, output: Path) -> bool:
	if re.fullmatch(r"[0-9a-f]{40}", sha) is None:
		return False
	head = subprocess.run(
		["git", "rev-parse", "HEAD"],
		cwd=root,
		capture_output=True,
		text=True,
		timeout=30,
		check=False,
	)
	clean = subprocess.run(
		["git", "diff", "--quiet", "--no-ext-diff", "--no-textconv", "HEAD", "--"],
		cwd=root,
		capture_output=True,
		timeout=30,
		check=False,
	)
	arguments = [
		"git",
		"ls-files",
		"--others",
		"--exclude-standard",
		"-z",
		"--",
		".",
		":(exclude,literal).quality-gate-ci",
	]
	if root in output.parents:
		arguments.append(f":(exclude,literal){output.relative_to(root).as_posix()}")
	untracked = subprocess.run(arguments, cwd=root, capture_output=True, timeout=30, check=False)
	return (
		head.returncode == 0
		and head.stdout.strip() == sha
		and clean.returncode == 0
		and untracked.returncode == 0
		and not untracked.stdout
	)


@contextmanager
def _source_worktree(root: Path, sha: str) -> Iterator[Path]:
	"""Run tests in a clean checkout while reusing the prepared runtime and evidence paths."""
	with tempfile.TemporaryDirectory(prefix="qg-pytest-source-") as directory:
		source = Path(directory) / "source"
		try:
			created = subprocess.run(
				["git", "worktree", "add", "--quiet", "--detach", str(source), sha],
				cwd=root,
				capture_output=True,
				text=True,
				encoding="utf-8",
				errors="replace",
				timeout=30,
				check=False,
			)
		except (OSError, subprocess.SubprocessError) as error:
			raise RuntimeUnavailable("exact test source is unavailable") from error
		if created.returncode:
			raise RuntimeUnavailable("exact test source is unavailable")
		try:
			yield source
		finally:
			try:
				removed = subprocess.run(
					["git", "worktree", "remove", "--force", str(source)],
					cwd=root,
					capture_output=True,
					text=True,
					encoding="utf-8",
					errors="replace",
					timeout=30,
					check=False,
				)
			except (OSError, subprocess.SubprocessError) as error:
				raise RuntimeUnavailable("exact test source cleanup failed") from error
			if removed.returncode:
				raise RuntimeUnavailable("exact test source cleanup failed")


def _totals(root: ET.Element) -> dict[str, int]:
	if root.tag not in {"testsuite", "testsuites"}:
		raise ValueError("unexpected JUnit document")
	suites = [root] if root.tag == "testsuite" else list(root)
	if not suites or any(suite.tag != "testsuite" for suite in suites):
		raise ValueError("JUnit has no complete test suite")
	totals = dict.fromkeys(("tests", "failures", "errors", "skipped"), 0)
	for suite in suites:
		observed = {"tests": len(suite.findall("testcase"))}
		observed.update(
			{
				key: len(suite.findall(f"testcase/{tag}"))
				for key, tag in (
					("failures", "failure"),
					("errors", "error"),
					("skipped", "skipped"),
				)
			}
		)
		for key in totals:
			value = suite.get(key, "")
			if not re.fullmatch(r"[0-9]+", value):
				raise ValueError("JUnit totals are unavailable")
			if int(value) != observed[key]:
				raise ValueError("JUnit totals do not match completed test records")
			totals[key] += int(value)
	return totals


def _pytest_outcome(code: int, junit: Path, public: Path) -> dict[str, Any]:
	try:
		totals = _totals(_public_report(junit, public))
	except (OSError, ValueError, ET.ParseError):
		return {"status": "error", "reason": "junit_unavailable"}
	if (
		code == 0
		and totals["tests"] > totals["skipped"]
		and not (totals["failures"] or totals["errors"])
	):
		return {"status": "passed", "totals": totals}
	if code == 1 and totals["failures"] and not totals["errors"]:
		return {"status": "failed", "totals": totals}
	return {"status": "error", "reason": "pytest_incomplete", "totals": totals}


def _run_component(
	prepared: PreparedEnvironment,
	index: int,
	output: Path,
	deadline: float,
	source_root: Path,
) -> dict[str, Any]:
	root = source_root.resolve()
	component = prepared.manifest.python[index]
	item: dict[str, Any] = {"name": component.name, "test_paths": list(component.test_paths)}
	if not component.tests_applicable:
		return {**item, "status": "not_applicable", "reason": component.tests_reason}
	item["status"] = "error"
	if index >= len(prepared.runtimes):
		return {**item, "reason": "runtime_unavailable"}
	runtime = prepared.runtimes[index]
	if not runtime.current or runtime.python is None:
		return {**item, "reason": "runtime_unavailable"}
	if not component.test_paths or any(not (root / path).exists() for path in component.test_paths):
		return {**item, "reason": "test_paths_unavailable"}
	junit = output / f"component-{index + 1}.xml"
	# A previous attempt is never evidence for this process.
	junit.unlink(missing_ok=True)
	command = [
		str(runtime.python),
		"-B",
		"-m",
		"pytest",
		"-p",
		"no:cacheprovider",
		"-q",
		"--junitxml",
		str(junit),
		"--",
		*component.test_paths,
	]
	item.update(arguments=command, junit=junit.name, exit_code=None)
	environment = dict(os.environ)
	for key in (
		"PYTHONHOME",
		"PYTHONPATH",
		"VIRTUAL_ENV",
		"PYTEST_ADDOPTS",
		"GITHUB_EVENT_PATH",
		"GITHUB_BASE_REF",
		"GITHUB_SHA",
	):
		environment.pop(key, None)
	environment["QUALITY_GATE_POLICY_ROOT"] = str(prepared.policy_root)
	end = min(deadline, time.monotonic() + component.timeout_seconds)
	try:
		private = root / "temp"
		private.mkdir(exist_ok=True)
		# Raw reports never enter the uploaded directory, even if the controller is killed.
		with tempfile.TemporaryDirectory(prefix="junit-", dir=private) as directory:
			raw = Path(directory) / "report.xml"
			command[command.index("--junitxml") + 1] = str(raw)
			code, stdout, stderr = run_process(
				command,
				root=root,
				environment=environment,
				deadline=end,
				own_tree=True,
				cleanup_deadline=end + CLEANUP_SECONDS,
				limit=MAX_OUTPUT_BYTES,
				truncate=True,
			)
			outcome = _pytest_outcome(code, raw, junit)
		log = output / f"component-{index + 1}.log"
		log.write_text(
			redact((stdout + stderr).decode("utf-8", errors="replace")), encoding="utf-8"
		)
		item.update(exit_code=code, log=log.name)
	except (OSError, EditError) as error:
		return {**item, "reason": "execution_incomplete", "detail": redact(str(error))}
	return {**item, **outcome}


def run_tests(
	prepared: PreparedEnvironment,
	output: Path,
	*,
	source_sha: str,
	timeout_seconds: int = 2400,
) -> dict[str, Any]:
	"""Run declared tests in an exact-source worktree; never prepare an environment."""
	root = prepared.root.resolve()
	output = output.resolve()
	output.mkdir(parents=True, exist_ok=True)
	result: dict[str, Any] = {
		"schema": 1,
		"kind": "full-pytest",
		"source_sha": source_sha,
		"engine_version": __version__,
		"policy_release": prepared.release_manifest.version,
		"manifest_sha256": hashlib.sha256((root / "quality-gate.toml").read_bytes()).hexdigest(),
		"status": "error",
		"components": [],
	}
	deadline = time.monotonic() + timeout_seconds
	if not _exact_source(root, source_sha, output):
		result["reason"] = "source_changed"
	else:
		try:
			if any(component.tests_applicable for component in prepared.manifest.python):
				with _source_worktree(root, source_sha) as source_root:
					result["components"] = [
						_run_component(prepared, index, output, deadline, source_root)
						for index in range(len(prepared.manifest.python))
					]
					if not _exact_source(source_root, source_sha, output):
						result.update(status="error", reason="source_changed")
			else:
				result["components"] = [
					_run_component(prepared, index, output, deadline, root)
					for index in range(len(prepared.manifest.python))
				]
		except RuntimeUnavailable as error:
			result.update(
				status="error", reason="source_snapshot_unavailable", detail=redact(str(error))
			)
		if result.get("reason") not in {"source_changed", "source_snapshot_unavailable"}:
			statuses = {item["status"] for item in result["components"]}
			result["status"] = (
				"error"
				if "error" in statuses
				else "failed"
				if "failed" in statuses
				else "passed"
				if "passed" in statuses
				else "not_applicable"
			)
		if not _exact_source(root, source_sha, output):
			result.update(status="error", reason="source_changed")
	(output / "result.json").write_text(
		json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
	)
	return result


def main() -> int:
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("--root", type=Path, default=Path.cwd())
	parser.add_argument("--source-sha", required=True)
	parser.add_argument("--output", type=Path, required=True)
	parser.add_argument(
		"--policy-release", help="Exact candidate policy for installed-artifact verification."
	)
	args = parser.parse_args()
	try:
		prepared = (
			prepare_bootstrap(args.root.resolve(), policy_release=args.policy_release)
			if args.policy_release
			else prepare(args.root.resolve())
		)
		result = run_tests(prepared, args.output, source_sha=args.source_sha)
	except (
		OSError,
		ValueError,
		ValidationError,
		DistributionError,
		RuntimeUnavailable,
		subprocess.TimeoutExpired,
	) as error:
		sys.stderr.write(f"Full pytest unavailable: {redact(str(error))}\n")
		return 2
	sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
	if result["status"] in {"passed", "not_applicable"}:
		return 0
	return 1 if result["status"] == "failed" else 2


if __name__ == "__main__":
	raise SystemExit(main())
