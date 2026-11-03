"""Opt-in acceptance of a published engine against explicitly prepared projects.

Prepare managed/ and unmanaged/ below TYPING_ACCEPTANCE_ROOT before verification.
Neither this suite nor an edit operation installs dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_background import BackgroundSession, cancel_session, collect, schedule
from _shared.quality_settings import settings_operation

BAD_API = "def accept(value: str) -> str:\n    return value\n"
GOOD_API = "def accept(value: int) -> str:\n    return str(value)\n"
PROVIDER_RELEASE = os.environ.get("TYPING_ACCEPTANCE_PROVIDER_RELEASE", "v4.2.1")
POLICY_RELEASE = os.environ.get("TYPING_ACCEPTANCE_POLICY_RELEASE", "v4.2.1")


@unittest.skipUnless(os.environ.get("TYPING_ACCEPTANCE_ROOT"), "Prepared release fixtures required")
class ReleasedTyping(unittest.TestCase):
	@unittest.skipUnless(os.environ.get("TYPING_PROFILE"), "Explicit latency profile required")
	def test_complete_event_latency_profile(self) -> None:
		base = Path(os.environ["TYPING_ACCEPTANCE_ROOT"]).resolve(strict=True)
		profile, count = os.environ["TYPING_PROFILE"].split("-")
		self.assertIn(profile, {"managed", "unmanaged"})
		self.assertIn(count, {"1", "10"})
		root = base / profile
		source_dir = root / "app" if profile == "managed" else root
		paths = [source_dir / "api.py"] + [source_dir / f"aux{i}.py" for i in range(1, int(count))]
		with tempfile.TemporaryDirectory(dir=base) as directory:
			storage = Path(directory)
			settings = self.settings(storage)
			settings_operation(
				settings,
				"update",
				{
					"baseline_release": POLICY_RELEASE,
					"provider_release": PROVIDER_RELEASE,
					"enabled": ["ruff", "secrets", "types"],
					"baseline_python": sys.executable,
					"baseline_python_version": "3.12",
				},
			)
			samples = []
			try:
				for index in range(30):
					for path in paths:
						path.write_text(
							(BAD_API if index % 2 == 0 else GOOD_API)
							if path == paths[0]
							else f"VALUE: int = {index}\n",
							encoding="utf-8",
						)
					samples.append(self.profile_event(root, paths, settings, storage, index))
					(base / f"latency-{profile}-{count}.json").write_text(
						json.dumps(
							{
								"engine": PROVIDER_RELEASE,
								"policy": POLICY_RELEASE,
								"persistent_mypy_cache": False,
								"enabled": ["ruff", "secrets", "types"],
								"samples": samples,
							}
						),
						encoding="utf-8",
					)
			finally:
				cancel_session(
					session=BackgroundSession(settings, storage / "state", "profile"),
					deadline=monotonic() + 3,
				)
				paths[0].write_text(BAD_API, encoding="utf-8")
				for path in paths[1:]:
					path.unlink(missing_ok=True)

	def profile_event(
		self, root: Path, paths: list[Path], settings: Path, storage: Path, index: int
	) -> dict:
		started = monotonic()
		names = [path.relative_to(root).as_posix() for path in paths]
		event = {
			"hook_event_name": "PostToolUse",
			"tool_name": "apply_patch",
			"cwd": str(root),
			"session_id": "profile",
			"tool_use_id": f"profile-{index}",
			"tool_input": {
				"command": "*** Begin Patch\n"
				+ "".join(f"*** Update File: {name}\n@@\n-old\n+new\n" for name in names)
				+ "*** End Patch"
			},
			"tool_response": "Success. Updated the following files:\n"
			+ "".join(f"M {name}\n" for name in names),
		}
		process = subprocess.run(
			[
				sys.executable,
				"-B",
				str(Path(__file__).parents[2] / "_shared/quality_coordinator.py"),
				"--settings",
				str(settings),
				"--state-dir",
				str(storage / "state"),
			],
			input=json.dumps(event),
			capture_output=True,
			text=True,
			encoding="utf-8",
			timeout=15,
		)
		self.assertEqual(process.returncode, 0)
		self.assertEqual(process.stderr, "")
		self.assertEqual(
			process.stdout, "", "Clean foreground checks and pending typing must be silent"
		)
		foreground_ms = round((monotonic() - started) * 1000)
		reports = collect(
			session=BackgroundSession(settings, storage / "state", "profile"),
			deadline=monotonic() + 43,
			drain=True,
			include_clean=True,
		)
		self.assertEqual(len(reports), 4, reports)
		self.assertEqual(
			{item["check"] for item in reports}, {"ruff", "secrets", "builtin_secrets", "types"}
		)
		reports_by_check = {item["check"]: item for item in reports}
		self.assertEqual(
			{
				check: reports_by_check[check]["status"]
				for check in ("ruff", "secrets", "builtin_secrets")
			},
			{"ruff": "passed", "secrets": "passed", "builtin_secrets": "passed"},
			reports,
		)
		report = reports_by_check["types"]
		self.assertEqual(report["status"], "failed" if index % 2 == 0 else "passed", report)
		measurement = {
			"index": index,
			"foreground_ms": foreground_ms,
			"total_ms": round((monotonic() - started) * 1000),
			"provider_ms": report["elapsed_ms"],
			"provider_process_count": report["process_count"],
			"environment": report["selections"][0]["environment"],
			"components": [
				{key: item[key] for key in ("source_count", "import_count", "analysis_ms")}
				for item in report["components"]
			],
		}
		if index == 0:
			self.assertEqual(report["findings"], self.full_scope(root, report, measurement))
		return measurement

	def test_published_engine_matches_full_scope_and_rechecks_signature_correction(self) -> None:
		base = Path(os.environ["TYPING_ACCEPTANCE_ROOT"]).resolve(strict=True)
		measurements = []
		for profile in ("unmanaged", "managed"):
			with self.subTest(profile=profile), tempfile.TemporaryDirectory(dir=base) as directory:
				root = base / profile
				source = root / ("app/api.py" if profile == "managed" else "api.py")
				settings = self.settings(Path(directory))
				try:
					source.write_text(BAD_API, encoding="utf-8")
					failed, timing = self.analyze(root, source, settings, Path(directory))
					self.assertEqual(failed["status"], "failed", failed)
					self.assertEqual(failed["engine_version"], PROVIDER_RELEASE.removeprefix("v"))
					self.assertEqual(failed["selections"][0]["mypy_version"], "2.3.1")
					findings = failed["findings"]
					self.assertEqual(len(findings), 1)
					self.assertEqual(findings[0]["rule"], "arg-type")
					self.assertEqual(
						findings[0]["path"],
						"app/consumer.py" if profile == "managed" else "consumer.py",
					)
					self.assertEqual(findings, self.full_scope(root, failed))
					measurements.append({"profile": profile, "case": "cold-error", **timing})
					source.write_text(GOOD_API, encoding="utf-8")
					passed, timing = self.analyze(root, source, settings, Path(directory))
					self.assertEqual(passed["status"], "passed", passed)
					self.assertEqual(passed["findings"], [])
					measurements.append({"profile": profile, "case": "signature-fix", **timing})
				finally:
					cancel_session(
						session=BackgroundSession(settings, Path(directory) / "state", "released"),
						deadline=monotonic() + 3,
					)
					source.write_text(BAD_API, encoding="utf-8")
		(base / "host-integration.json").write_text(json.dumps(measurements), encoding="utf-8")

	def settings(self, directory: Path) -> Path:
		path = directory / "settings.json"
		settings_operation(path, "init")
		settings_operation(
			path,
			"update",
			{
				"baseline_release": POLICY_RELEASE,
				"provider_release": PROVIDER_RELEASE,
				"enabled": ["types"],
			},
		)
		return path

	def analyze(
		self, root: Path, source: Path, settings: Path, directory: Path
	) -> tuple[dict, dict]:
		started = monotonic()
		result = schedule(
			root,
			[
				{
					"path": source.relative_to(root).as_posix(),
					"sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
				}
			],
			session=BackgroundSession(settings, directory / "state", "released"),
			deadline=started + 10,
		)
		self.assertEqual(result["status"], "pending", result)
		scheduled_ms = round((monotonic() - started) * 1000)
		reports = collect(
			session=BackgroundSession(settings, directory / "state", "released"),
			deadline=monotonic() + 43,
			drain=True,
			include_clean=True,
		)
		self.assertEqual(len(reports), 1, reports)
		report = reports[0]
		return report, {
			"schedule_ms": scheduled_ms,
			"total_ms": round((monotonic() - started) * 1000),
			"provider_ms": report.get("elapsed_ms"),
			"process_count": report.get("process_count"),
			"components": [
				{key: item[key] for key in ("source_count", "import_count", "analysis_ms")}
				for item in report.get("components", [])
			],
		}

	def full_scope(self, root: Path, report: dict, metrics: dict | None = None) -> list[dict]:
		selection = report["selections"][0]
		policy = Path(os.environ["LOCALAPPDATA"]) / "quality-gate/releases"
		with tempfile.TemporaryDirectory() as cache:
			process = subprocess.run(
				[
					selection["python"],
					"-I",
					"-B",
					"-m",
					"mypy",
					"--config-file",
					str(policy / selection["policy_release"] / "quality_gate/policy/mypy.ini"),
					"--show-column-numbers",
					"--show-error-codes",
					"--no-pretty",
					"--no-error-summary",
					"--cache-dir",
					cache,
					*(["--verbose"] if metrics is not None else []),
					"app" if (root / "quality-gate.toml").exists() else ".",
				],
				cwd=root,
				capture_output=True,
				text=True,
				encoding="utf-8",
				check=False,
				timeout=30,
			)
		self.assertEqual(process.returncode, 1, "Expected the independent full-component error")
		if metrics is None:
			self.assertEqual(process.stderr, "", "The independent checker must complete normally")
		else:
			graph = re.search(r"Loaded graph with (\d+) nodes", process.stderr)
			self.assertIsNotNone(graph, "The independent checker must report its completed graph")
			metrics["mypy_graph_nodes"] = int(graph[1]) if graph else None
			started = monotonic()
			version = subprocess.run(
				[selection["python"], "-I", "-B", "-m", "mypy", "--version"],
				capture_output=True,
				text=True,
				encoding="utf-8",
				check=False,
				timeout=10,
			)
			metrics["mypy_version_startup_ms"] = round((monotonic() - started) * 1000)
			self.assertEqual(version.returncode, 0)
			self.assertTrue(version.stdout.startswith("mypy 2.3.1"))
			started = monotonic()
			startup = subprocess.run(
				[selection["python"], "-I", "-B", "-c", "pass"],
				capture_output=True,
				check=False,
				timeout=10,
			)
			metrics["python_startup_ms"] = round((monotonic() - started) * 1000)
			self.assertEqual(startup.returncode, 0)
		findings = []
		for line in process.stdout.splitlines():
			match = re.fullmatch(r"(.+?):(\d+):(\d+): error: .*  \[([a-z0-9-]+)\]", line)
			self.assertIsNotNone(match, "Unexpected independent checker output")
			if match:
				findings.append(
					{
						"path": Path(match[1]).as_posix().removeprefix("./"),
						"line": int(match[2]),
						"column": int(match[3]),
						"rule": match[4],
					}
				)
		return findings


if __name__ == "__main__":
	unittest.main()
