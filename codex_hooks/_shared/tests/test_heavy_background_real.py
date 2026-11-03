"""Opt-in real native-tool acceptance through coordinator Post and Stop."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_background import BackgroundSession, cancel_session
from _shared.quality_background_state import owned_directory
from _shared.quality_settings import settings_operation


@unittest.skipUnless(
	os.environ.get("LEGACY_SECURITY_ACCEPTANCE_PYTHON"), "Prepared native security tools required"
)
class RealHeavyBackground(unittest.TestCase):
	def test_post_returns_before_heavy_completion_then_stop_delivers_current_result(self) -> None:
		python = Path(os.environ["LEGACY_SECURITY_ACCEPTANCE_PYTHON"]).resolve(strict=True)
		entry = Path(__file__).resolve().parents[1] / "quality_coordinator.py"
		observations = []
		for failing in (False, True):
			with self.subTest(failing=failing), tempfile.TemporaryDirectory() as directory:
				base = Path(directory).resolve()
				root, state, settings = base / "project", base / "state", base / "settings.json"
				root.mkdir()
				subprocess.run(
					["git", "init", "--quiet", str(root)],
					check=True,
					capture_output=True,
					timeout=10,
				)
				content = (
					("TOKEN = '" + "sk-" + "a" * 20 + "'\n").encode()
					if failing
					else b"VALUE: int = 1\n"
				)
				(root / "example.py").write_bytes(content)
				settings_operation(settings, "init")
				settings_operation(
					settings,
					"update",
					{
						"provider_release": "v4.2.1",
						"baseline_release": "v3.1.0",
						"enabled": ["integrity"],
						"baseline_python": str(python),
						"baseline_python_version": "3.12",
					},
				)
				session = BackgroundSession(settings, state, "real-heavy-actor")
				event = {
					"hook_event_name": "PostToolUse",
					"tool_name": "apply_patch",
					"cwd": str(root),
					"session_id": "real-heavy-actor",
					"tool_use_id": "real-heavy-edit",
					"tool_input": {
						"command": (
							"*** Begin Patch\n*** Update File: example.py\n"
							"@@\n-old\n+new\n*** End Patch"
						)
					},
					"tool_response": "Success. Updated the following files:\nM example.py\n",
				}
				environment = {
					**os.environ,
					"CODEX_POST_EDIT_HEAVY_SECURITY": "1",
					"PYTHONDONTWRITEBYTECODE": "1",
					"PYTHONIOENCODING": "utf-8",
					"SEMGREP_SEND_METRICS": "off",
					"SEMGREP_ENABLE_VERSION_CHECK": "0",
				}
				command = [
					str(python),
					"-B",
					str(entry),
					"--settings",
					str(settings),
					"--state-dir",
					str(state),
				]
				started = monotonic()
				try:
					post = subprocess.run(
						command,
						input=json.dumps(event),
						capture_output=True,
						text=True,
						encoding="utf-8",
						env=environment,
						timeout=15,
					)
					post_ms = round((monotonic() - started) * 1000)
					self.assertEqual(post.returncode, 0, post.stderr)
					self.assertEqual(post.stderr, "")
					meta = owned_directory(state, root) / "latest.json"
					queued = json.loads(meta.read_text(encoding="utf-8"))
					self.assertEqual(queued["checks"], ["legacy_security"])
					self.assertEqual(
						queued["status"], "pending", "Post must return while heavy work remains"
					)
					stop = subprocess.run(
						command,
						input=json.dumps({**event, "hook_event_name": "Stop"}),
						capture_output=True,
						text=True,
						encoding="utf-8",
						env=environment,
						timeout=45,
					)
					self.assertEqual(stop.returncode, 0, stop.stderr)
					self.assertEqual(stop.stderr, "")
					final = json.loads(meta.read_text(encoding="utf-8"))
					self.assertEqual(final["status"], "completed", final)
					report = final["wire"]["results"]["legacy_security"]["report"]
					observation = {
						"failing": failing,
						"post_ms": post_ms,
						"total_ms": round((monotonic() - started) * 1000),
						"status_after_post": queued["status"],
						"final_job": final,
					}
					observations.append(observation)
					if output := os.environ.get("HEAVY_BACKGROUND_OBSERVATIONS"):
						Path(output).write_text(
							json.dumps(observations, indent=2), encoding="utf-8"
						)
					self.assertEqual(report["status"], "failed" if failing else "passed", report)
					self.assertEqual(set(final["audiences"]), set(final["acknowledged"]))
					self.assertNotIn(
						"sk-" + "a" * 20, post.stdout + stop.stdout + json.dumps(report)
					)
					self.assertEqual((root / "example.py").read_bytes(), content)
					if not failing:
						self.assertEqual(post.stdout + stop.stdout, "")
				finally:
					cleanup = cancel_session(session=session, deadline=monotonic() + 3)
					self.assertEqual(cleanup, [], cleanup)
