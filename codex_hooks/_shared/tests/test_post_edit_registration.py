"""Verify the Windows Post-edit launcher keeps its installing Python runtime."""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from python_quality.install_post_edit_hook import group as post_edit_group  # noqa: E402


class PostEditRegistrationTests(unittest.TestCase):
	def setUp(self) -> None:
		temp_root = Path(__file__).resolve().parents[3] / "temp"
		temp_root.mkdir(exist_ok=True)
		self.temporary = tempfile.TemporaryDirectory(
			prefix="post-edit-registration-", dir=temp_root
		)
		self.addCleanup(self.temporary.cleanup)
		self.root = Path(self.temporary.name)

	def _windows_source(self, checkout_name: str = "checkout with spaces") -> tuple[Path, Path]:
		source = self.root / checkout_name / "codex_hooks"
		python_quality = source / "python_quality"
		python_quality.mkdir(parents=True)
		script = python_quality / "post_edit_checks.py"
		script.write_text(
			"import json, os, sys\n"
			"from pathlib import Path\n"
			"Path(os.environ['CODEX_CAPTURE']).write_text("
			"json.dumps(sys.argv), encoding='utf-8')\n",
			encoding="utf-8",
		)
		return source, script

	def _write_interpreter_stub(self, path: Path) -> None:
		path.parent.mkdir(parents=True, exist_ok=True)
		path.write_bytes(
			b"@echo off\r\n"
			b'if not exist "%~2" exit /b 98\r\n'
			b'>"%CODEX_CAPTURE%" echo %~1\r\n'
			b'>>"%CODEX_CAPTURE%" echo %~2\r\n'
			b"exit /b 0\r\n"
		)

	def _run_registered_command(
		self, command: str, capture: Path, *, path: str
	) -> subprocess.CompletedProcess[str]:
		environment = os.environ.copy()
		environment["CODEX_CAPTURE"] = str(capture)
		environment["PATH"] = path
		return subprocess.run(
			command,
			cwd=self.root,
			env=environment,
			capture_output=True,
			text=True,
			timeout=15,
			check=False,
		)

	@unittest.skipUnless(os.name == "nt", "requires the Windows command interpreter")
	def test_windows_registration_routes_spaced_paths_to_the_installer_interpreter(self) -> None:
		source, script = self._windows_source()
		python_executable = self.root / "Program Files" / "Python 3.11" / "python.cmd"
		self._write_interpreter_stub(python_executable)
		capture = self.root / "interpreter arguments.txt"
		with patch(
			"python_quality.install_post_edit_hook.sys.executable",
			str(python_executable),
		):
			group = post_edit_group(self.root, source)

		completed = self._run_registered_command(
			group["hooks"][0]["commandWindows"],
			capture,
			path=os.environ["PATH"],
		)
		self.assertEqual(completed.returncode, 0, completed.stderr)
		self.assertEqual(
			capture.read_text(encoding="ascii").splitlines(),
			["-B", str(script.resolve())],
		)

	@unittest.skipUnless(os.name == "nt", "requires the Windows command interpreter")
	def test_windows_registration_keeps_metacharacter_paths_as_encoded_data(self) -> None:
		source, script = self._windows_source("checkout&%QG_EDIT_MARKER%")
		python_executable = self.root / "Python&%QG_EDIT_MARKER%" / "python.exe"
		with patch("python_quality.install_post_edit_hook.sys.executable", str(python_executable)):
			group = post_edit_group(self.root, source)

		command = group["hooks"][0]["commandWindows"]
		encoded = command.rsplit(" ", maxsplit=1)[-1]
		script_body = base64.b64decode(encoded).decode("utf-16-le")
		self.assertNotIn("&", command)
		self.assertNotIn("%QG_EDIT_MARKER%", command)
		self.assertIn(f"$executable = '{python_executable}'", script_body)
		self.assertIn(f"'{script.resolve()}'", script_body)

	@unittest.skipUnless(os.name == "nt", "requires the Windows command interpreter")
	def test_windows_registration_runs_script_with_ampersand_in_path(self) -> None:
		source, script = self._windows_source("checkout&%QG_EDIT_MARKER%")
		capture = self.root / "arguments.json"
		with patch.dict(os.environ, {"QG_EDIT_MARKER": "expanded"}):
			group = post_edit_group(self.root, source)
			completed = self._run_registered_command(
				group["hooks"][0]["commandWindows"],
				capture,
				path=os.environ["PATH"],
			)

		self.assertEqual(completed.returncode, 0, completed.stderr)
		self.assertEqual(
			json.loads(capture.read_text(encoding="utf-8")),
			[str(script.resolve())],
		)

	@unittest.skipUnless(os.name == "nt", "requires the Windows command interpreter")
	def test_missing_installer_interpreter_does_not_fall_back_to_path(self) -> None:
		source, _script = self._windows_source()
		fallback = self.root / "PATH fallback" / "python.cmd"
		self._write_interpreter_stub(fallback)
		missing_interpreter = self.root / "Missing Python" / "python.exe"
		capture = self.root / "fallback arguments.txt"
		with patch(
			"python_quality.install_post_edit_hook.sys.executable",
			str(missing_interpreter),
		):
			group = post_edit_group(self.root, source)

		completed = self._run_registered_command(
			group["hooks"][0]["commandWindows"],
			capture,
			path=f"{fallback.parent}{os.pathsep}{os.environ['PATH']}",
		)
		self.assertEqual(completed.returncode, 2, completed.stderr)
		self.assertFalse(capture.exists(), "the unrelated PATH interpreter must not run")

	def test_non_windows_registration_keeps_the_existing_python3_command(self) -> None:
		source = self.root / "checkout with spaces" / "codex_hooks"
		with patch("_shared.hook_registration.os.name", "posix"):
			group = post_edit_group(self.root, source)

		handler = group["hooks"][0]
		self.assertEqual(
			shlex.split(handler["command"]),
			[
				"python3",
				"-B",
				str(source.resolve() / "python_quality" / "post_edit_checks.py"),
			],
		)
		self.assertNotIn("commandWindows", handler)


if __name__ == "__main__":
	unittest.main()
