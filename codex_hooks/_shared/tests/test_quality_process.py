"""Real subprocess tests for the edit deadline and bounded output contract."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.quality_process import ProcessError, execute


def silent_descendant_program() -> str:
	return (
		"import subprocess, sys; from pathlib import Path; "
		"child = subprocess.Popen([sys.executable, '-B', '-c', "
		"'import time; time.sleep(30)'], stdout=subprocess.DEVNULL, "
		"stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL); "
		"Path(sys.argv[1]).write_text(str(child.pid))"
	)


def expire_when_ready(marker: Path, deadline: float) -> Callable[[], float]:
	"""Advance time once a real child exists; keep startup and cleanup time real."""
	expired = False

	def clock() -> float:
		nonlocal expired
		if not expired and marker.exists() and marker.stat().st_size > 0:
			expired = True
			return deadline
		return monotonic()

	return clock


class BoundedProcesses(unittest.TestCase):
	@unittest.skipUnless(sys.platform == "linux", "POSIX cleanup observed through Linux procfs")
	def test_success_terminates_silent_posix_descendant(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			marker = Path(directory) / "silent-child.pid"
			program = silent_descendant_program()
			try:
				result = execute(
					[sys.executable, "-B", "-c", program, str(marker)], deadline=monotonic() + 3
				)
				self.assertEqual(result.returncode, 0)
				self.assertTrue(marker.exists(), "The fixture never started its descendant")
				pid = int(marker.read_text())
				status = Path(f"/proc/{pid}/stat")
				deadline = monotonic() + 1
				while status.exists():
					try:
						state = status.read_text().rsplit(")", 1)[1].split()[0]
					except FileNotFoundError:
						break
					if state == "Z":
						break
					if monotonic() >= deadline:
						self.fail("Successful execution left its silent descendant running")
					sleep(0.01)
			finally:
				if marker.exists() and sys.platform != "win32":
					try:
						os.kill(int(marker.read_text()), signal.SIGKILL)
					except ProcessLookupError:
						pass

	def test_utf8_stdin_stdout_and_exit_status_survive(self) -> None:
		result = execute(
			[
				sys.executable,
				"-B",
				"-c",
				"import sys; print(sys.stdin.buffer.read().decode('utf-8')); raise SystemExit(1)",
			],
			input_text="привет",
			deadline=monotonic() + 3,
		)
		self.assertEqual(result.returncode, 1)
		self.assertEqual(result.stdout.strip(), "привет")

	def test_expired_deadline_does_not_start_a_process(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			marker = Path(directory) / "must-not-exist"
			with self.assertRaisesRegex(ProcessError, "deadline"):
				execute(
					[
						sys.executable,
						"-B",
						"-c",
						"from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
						str(marker),
					],
					deadline=monotonic() - 1,
				)
			self.assertFalse(marker.exists())

	def test_stdout_limit_is_an_explicit_failure(self) -> None:
		with self.assertRaisesRegex(ProcessError, "output_limit"):
			execute(
				[sys.executable, "-B", "-c", "import sys; sys.stdout.buffer.write(b'x' * 65536)"],
				deadline=monotonic() + 3,
				output_limit=1024,
			)

	def test_invalid_utf8_never_leaks_raw_output(self) -> None:
		with self.assertRaisesRegex(ProcessError, "invalid_output") as caught:
			execute(
				[
					sys.executable,
					"-B",
					"-c",
					"import sys; sys.stdout.buffer.write(b'private-fragment\\xff')",
				],
				deadline=monotonic() + 3,
			)
		self.assertNotIn("private-fragment", str(caught.exception))

	@unittest.skipUnless(os.name == "nt", "Windows process-tree contract")
	def test_success_terminates_silent_descendant_before_returning(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			marker = Path(directory) / "silent-child.pid"
			program = silent_descendant_program()
			try:
				result = execute(
					[sys.executable, "-B", "-c", program, str(marker)], deadline=monotonic() + 3
				)
				self.assertEqual(result.returncode, 0)
				self.assertTrue(marker.exists(), "The fixture never started its descendant")
				pid = marker.read_text()
				self.assertTrue(pid.isdecimal(), "The fixture did not publish a complete child PID")
				remaining = subprocess.run(
					["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
					capture_output=True,
					text=True,
					timeout=5,
				)
				self.assertNotIn(f'"{pid}"', remaining.stdout)
			finally:
				if marker.exists():
					subprocess.run(
						["taskkill", "/PID", marker.read_text(), "/T", "/F"],
						capture_output=True,
						timeout=5,
					)

	@unittest.skipUnless(os.name == "nt", "Windows process-tree contract")
	def test_timeout_terminates_descendant_after_parent_exits(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			marker = Path(directory) / "orphan.pid"
			program = (
				"import subprocess, sys; from pathlib import Path; "
				"child = subprocess.Popen([sys.executable, '-B', '-c', "
				"'import time; time.sleep(30)']); "
				"Path(sys.argv[1]).write_text(str(child.pid))"
			)
			try:
				deadline = monotonic() + 10
				with (
					patch(
						"_shared.quality_process.monotonic",
						side_effect=expire_when_ready(marker, deadline),
					),
					self.assertRaisesRegex(ProcessError, "^deadline$"),
				):
					execute([sys.executable, "-B", "-c", program, str(marker)], deadline=deadline)
				self.assertTrue(marker.exists(), "The fixture never started its descendant")
				pid = marker.read_text()
				remaining = subprocess.run(
					["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
					capture_output=True,
					text=True,
					timeout=5,
				)
				self.assertNotIn(f'"{pid}"', remaining.stdout)
			finally:
				if marker.exists():
					subprocess.run(
						["taskkill", "/PID", marker.read_text(), "/T", "/F"],
						capture_output=True,
						timeout=5,
					)

	@unittest.skipUnless(os.name == "nt", "Windows process-tree contract")
	def test_timeout_terminates_owned_descendant(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			marker = Path(directory) / "child.pid"
			program = (
				"import subprocess, sys, time; from pathlib import Path; "
				"child = subprocess.Popen([sys.executable, '-B', '-c', "
				"'import time; time.sleep(30)']); "
				"Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
			)
			try:
				deadline = monotonic() + 10
				with (
					patch(
						"_shared.quality_process.monotonic",
						side_effect=expire_when_ready(marker, deadline),
					),
					self.assertRaisesRegex(ProcessError, "^deadline$"),
				):
					execute([sys.executable, "-B", "-c", program, str(marker)], deadline=deadline)
				self.assertTrue(marker.exists(), "The process-tree fixture never started its child")
				pid = marker.read_text()
				self.assertTrue(pid.isdecimal(), "The fixture did not publish a complete child PID")
				remaining = subprocess.run(
					["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
					capture_output=True,
					text=True,
					timeout=5,
				)
				self.assertNotIn(f'"{pid}"', remaining.stdout)
			finally:
				if marker.exists():
					subprocess.run(
						["taskkill", "/PID", marker.read_text(), "/T", "/F"],
						capture_output=True,
						timeout=5,
					)


if __name__ == "__main__":
	unittest.main()
