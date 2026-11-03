"""Opt-in executable integration with an exact released or local provider.

SECRET_PROVIDER_CANDIDATE names a development checkout, never a release cache.
The disposable native-launcher substitute uses its documented source CLI seam.
SECRET_PROVIDER_RELEASE uses the unmodified verified native release launcher.
Neither route proves registered desktop event delivery.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_feedback import feedback
from _shared.quality_settings import settings_operation
from _shared.tests.provider_fixture import native_launcher

ENTRY = Path(__file__).resolve().parents[1] / "check.py"
CANDIDATE = os.environ.get("SECRET_PROVIDER_CANDIDATE")
RELEASE = os.environ.get("SECRET_PROVIDER_RELEASE")
SYNTHETIC = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
OBSERVATIONS: list[dict] = []
LAUNCHER = """import os, subprocess, sys
from released_native_quality_gate import run_released_quality_gate
def run_released_command(root, arguments, *, manifest):
    if arguments != ["edit-secrets"]:
        from released_native_quality_gate import run_released_command as released
        return released(root, arguments, manifest=manifest)
    return subprocess.run([
        sys.executable, "-I", "-B", "-c",
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from quality_gate.cli import main; raise SystemExit(main())",
        os.environ["SECRET_PROVIDER_CANDIDATE"], "edit-secrets",
    ], check=False)
"""


def manifest(waiver: str = "") -> str:
	return (
		("" if waiver else "waivers = []\n")
		+ """[quality]
schema = 2
policy_release = "v2.4.2"
[repository]
name = "host-secret-fixture"
domains = ["repository"]
required_documents = ["AGENTS.md"]
"""
		+ waiver
	)


@unittest.skipUnless(CANDIDATE or RELEASE, "Select an explicit secret provider for integration")
class SecretProviderIntegration(unittest.TestCase):
	def setUp(self) -> None:
		self.assertFalse(CANDIDATE and RELEASE, "Select development source or a release")
		self.candidate = Path(CANDIDATE).resolve(strict=True) if CANDIDATE else None
		if self.candidate:
			self.assertTrue((self.candidate / "docs" / "edit-secrets.md").is_file())
		self.source_hashes = self.identities()
		self.temporary = tempfile.TemporaryDirectory(
			prefix="secret-provider-host-", dir=Path.home()
		)
		self.addCleanup(self.temporary.cleanup)
		self.base = Path(self.temporary.name).resolve()
		self.root = self.base / "project"
		self.root.mkdir()
		host = self.base / "host"
		launcher = host / "hooks" / "_shared" / "native_quality_gate.py"
		launcher.parent.mkdir(parents=True)
		installed = native_launcher()
		self.assertTrue(
			installed.is_file(), "Prepare the native release launcher before integration"
		)
		shutil.copyfile(installed, launcher)
		for name in ("codex_hook_adapter.py", "command_safety.py", "github_safety.py"):
			shutil.copyfile(installed.parent / name, launcher.parent / name)
		if CANDIDATE:
			shutil.copyfile(installed, launcher.with_name("released_native_quality_gate.py"))
			launcher.write_text(LAUNCHER, encoding="utf-8")
		self.environment = {
			**os.environ,
			"CODEX_HOME": str(host),
			"CODEX_POST_EDIT_HEAVY_SECURITY": "0",
		}
		self.settings = self.base / "settings.json"
		settings_operation(self.settings, "init")
		settings_operation(
			self.settings,
			"update",
			{
				"provider_release": RELEASE or "v2.6.2",
				"baseline_release": "v2.4.2",
				"enabled": ["secrets"],
				"budget_seconds": 10,
				"baseline_python": sys.executable,
				"baseline_python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
			},
		)

	def identities(self) -> dict[str, str]:
		if self.candidate is None:
			return {}
		return {
			path.relative_to(self.candidate).as_posix(): hashlib.sha256(
				path.read_bytes()
			).hexdigest()
			for path in sorted((self.candidate / "quality_gate").glob("*.py"))
		}

	def tearDown(self) -> None:
		self.assertEqual(
			self.source_hashes, self.identities(), "Provider changed during integration"
		)

	def invoke(self, paths: list[str]) -> tuple[int, dict]:
		request = {
			"schema": 1,
			"root": str(self.root),
			"files": [
				{
					"path": path,
					"sha256": hashlib.sha256((self.root / path).read_bytes()).hexdigest(),
				}
				for path in paths
			],
		}
		started = monotonic()
		process = subprocess.run(
			[sys.executable, "-B", str(ENTRY), "--settings", str(self.settings)],
			input=json.dumps(request),
			capture_output=True,
			text=True,
			encoding="utf-8",
			env=self.environment,
			timeout=15,
		)
		elapsed = int((monotonic() - started) * 1000)
		self.assertTrue(process.stderr == "", "Host leaked provider diagnostics")
		self.assertTrue(SYNTHETIC not in process.stdout, "Host leaked synthetic content")
		result = json.loads(process.stdout)
		OBSERVATIONS.append(
			{
				"case": self.id(),
				"status": result["status"],
				"reason": result.get("reason"),
				"elapsed_ms": elapsed,
				"process_count": result.get("process_count"),
				"selections": result.get("selections", []),
			}
		)
		return process.returncode, result

	def write(self, path: str, content: bytes) -> None:
		full = self.root / path
		full.parent.mkdir(parents=True, exist_ok=True)
		full.write_bytes(content)

	def test_managed_unmanaged_and_non_git_clean_failure_fix_and_repeat(self) -> None:
		name = "src/пример file.txt"
		for mode in ("non_git", "unmanaged", "managed"):
			with self.subTest(mode=mode):
				if mode != "non_git":
					(self.root / ".git").mkdir(exist_ok=True)
				if mode == "managed":
					(self.root / "quality-gate.toml").write_text(manifest(), encoding="utf-8")
				self.write(name, b"VALUE = 1\n")
				code, clean = self.invoke([name])
				self.assertEqual((code, clean["status"]), (0, "passed"))
				self.write(name, f"token = '{SYNTHETIC}'\n".encode())
				code, result = self.invoke([name])
				self.assertEqual((code, result["status"]), (1, "failed"))
				finding = result["files"][0]["findings"][0]
				self.assertEqual(
					(finding["target"], finding["fingerprint"]),
					(name + ":1", hashlib.sha256(SYNTHETIC.encode()).hexdigest()),
				)
				self.assertEqual(
					result["files"][0]["coverage"], "managed" if mode == "managed" else "baseline"
				)
				context = feedback("PostToolUse", [result], evidence_dir=self.base / "evidence")
				self.assertIsNotNone(context)
				self.assertTrue(SYNTHETIC not in json.dumps(context), "Feedback leaked content")
				self.write(name, b"VALUE = 2\n")
				for _ in range(2):
					_, clean = self.invoke([name])
					self.assertEqual(clean["status"], "passed")
					self.assertIsNone(feedback("PostToolUse", [clean]))

	def test_exact_waiver_stays_silent_and_rename_loses_old_target(self) -> None:
		name = "example.py"
		self.write(name, f"TOKEN = '{SYNTHETIC}'\n".encode())
		waiver = f'''[[waivers]]
kind = "secret"
check_id = "secrets.candidate"
target = "{name}:1"
fingerprint = "{hashlib.sha256(SYNTHETIC.encode()).hexdigest()}"
reason = "Synthetic host fixture"
approved_by = "Human Reviewer"
reviewed_on = "2026-08-01"
expires_on = "2099-12-31"
'''
		(self.root / "quality-gate.toml").write_text(manifest(waiver), encoding="utf-8")
		code, result = self.invoke([name])
		self.assertEqual((code, result["status"]), (0, "waived"))
		self.assertIsNone(feedback("PostToolUse", [result]))
		(self.root / name).rename(self.root / "renamed.py")
		code, result = self.invoke(["renamed.py"])
		self.assertEqual((code, result["status"]), (1, "failed"))
		self.assertEqual(result["files"][0]["findings"][0]["target"], "renamed.py:1")
		(self.root / "renamed.py").unlink()
		self.assertEqual(self.invoke([])[1]["status"], "not_applicable")

	def test_binary_and_unsupported_bytes_are_explicitly_unchecked(self) -> None:
		for content in (b"%PDF-1.7\nexample\n", b"\xff\x00", b"\xff\xfeV\x00"):
			self.write("example.py", content)
			code, result = self.invoke(["example.py"])
			self.assertEqual(
				(code, result["status"], result["reason"]), (2, "unchecked", "coverage")
			)

	@unittest.skipUnless(RELEASE, "Same-release normal parity requires an immutable release")
	def test_same_released_normal_scan_preserves_actual_fingerprints_and_waiver(self) -> None:
		subprocess.run(["git", "init", "--quiet", str(self.root)], check=True, capture_output=True)
		self.write("AGENTS.md", b"# Disposable secret parity fixture\n")
		self.write(".gitignore", b"/temp/\n")
		self.write(
			".github/workflows/quality-gate.yml",
			b"name: Quality Gate\non:\n  pull_request:\n  push:\n    branches: [main]\n"
			b"permissions:\n  contents: read\nconcurrency:\n"
			b"  group: quality-gate-${{ github.workflow }}-${{ github.ref }}\n"
			b"  cancel-in-progress: true\n"
			b"jobs:\n  quality-gate:\n    uses: TheTraitor0FJesus/quality-gate/"
			b".github/workflows/quality.yml@7bfba6baa8666a2211a83e2e61c5f8526770864d\n",
		)
		self.write("example.txt", f"token = '{SYNTHETIC}'\n".encode())
		digest = hashlib.sha256(SYNTHETIC.encode()).hexdigest()
		waiver = f'''[[waivers]]
kind = "secret"
check_id = "secrets.candidate"
target = "example.txt:1"
fingerprint = "{digest}"
reason = "Synthetic same-release fixture"
approved_by = "Human Reviewer"
reviewed_on = "2026-10-20"
expires_on = "2099-12-31"
'''
		for waived in (False, True):
			with self.subTest(waived=waived):
				configuration = manifest(waiver if waived else "").replace("v2.4.2", RELEASE or "")
				self.write("quality-gate.toml", configuration.encode())
				subprocess.run(
					["git", "-C", str(self.root), "add", "."], check=True, capture_output=True
				)
				_, edited = self.invoke(["example.txt"])
				self.assertEqual(edited["files"][0]["findings"][0]["fingerprint"], digest)
				normal = subprocess.run(
					[
						sys.executable,
						"-B",
						"-c",
						"import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
						"from native_quality_gate import run_released_command; root=Path.cwd(); "
						"raise SystemExit(run_released_command(root, "
						"['--root', str(root), 'check', '--verbose'], "
						"manifest=(root/'quality-gate.toml').read_bytes()).returncode)",
						str(Path(self.environment["CODEX_HOME"]) / "hooks" / "_shared"),
					],
					cwd=self.root,
					env=self.environment,
					capture_output=True,
					text=True,
					encoding="utf-8",
					timeout=30,
				)
				output = normal.stdout + normal.stderr
				self.assertEqual(normal.returncode, 0 if waived else 1, output)
				self.assertNotIn(SYNTHETIC, output)
				self.assertEqual(normal.stderr, "")
				self.assertIn("repository.workflow: passed", output)
				status = "waived" if waived else "failed"
				self.assertEqual(edited["status"], status)
				self.assertIn(f"secrets.candidate: {status}", output)
				if not waived:
					self.assertIn(f"example.txt:1: secret detected (fingerprint: {digest})", output)

	def test_real_ruff_writer_then_secret_reader_and_silent_correction(self) -> None:
		settings_operation(
			self.settings,
			"update",
			{
				"provider_release": RELEASE or "v2.6.2",
				"baseline_release": "v2.4.2",
				"enabled": ["ruff", "secrets"],
				"budget_seconds": 10,
				"baseline_python": sys.executable,
				"baseline_python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
			},
		)
		entry = ENTRY.parents[1] / "_shared" / "quality_coordinator.py"
		event = {
			"hook_event_name": "PostToolUse",
			"tool_name": "apply_patch",
			"cwd": str(self.root),
			"session_id": "secret-development",
			"tool_use_id": "development-edit",
			"tool_input": {
				"command": "*** Begin Patch\n*** Update File: example.py\n@@\n-x\n+y\n*** End Patch"
			},
			"tool_response": "Success. Updated the following files:\nM example.py\n",
		}
		for failing in (True, False):
			self.write(
				"example.py",
				(f"TOKEN='{SYNTHETIC}'\nVALUE=1\n" if failing else "VALUE=2\n").encode(),
			)
			started = monotonic()
			outputs = []
			# Post may return while the reader is pending; Stop owns its drain.
			for name, timeout in (("PostToolUse", 15), ("Stop", 45)):
				process = subprocess.run(
					[
						sys.executable,
						"-B",
						str(entry),
						"--settings",
						str(self.settings),
						"--state-dir",
						str(self.base / "state"),
						"--evidence-dir",
						str(self.base / "evidence"),
					],
					input=json.dumps({**event, "hook_event_name": name}),
					capture_output=True,
					text=True,
					encoding="utf-8",
					env=self.environment,
					timeout=timeout,
				)
				self.assertEqual(process.stderr, "", "Coordinator emitted raw diagnostics")
				self.assertNotIn(SYNTHETIC, process.stdout, "Coordinator leaked content")
				self.assertEqual(process.returncode, 0)
				self.assertNotIn("UNCHECKED", process.stdout)
				outputs.append(process.stdout)
			OBSERVATIONS.append(
				{
					"case": self.id(),
					"status": "failed" if failing else "passed",
					"elapsed_ms": int((monotonic() - started) * 1000),
				}
			)
			if failing:
				responses = [json.loads(output) for output in outputs if output]
				context = "\n".join(
					response.get("reason", "")
					+ response.get("hookSpecificOutput", {}).get("additionalContext", "")
					for response in responses
				)
				self.assertIn("example.py:1 secrets.candidate", context)
				self.assertTrue(b"VALUE = 1\n" in (self.root / "example.py").read_bytes())
			else:
				self.assertEqual(outputs, ["", ""])
				self.assertEqual((self.root / "example.py").read_bytes(), b"VALUE = 2\n")


if __name__ == "__main__":
	unittest.main()
