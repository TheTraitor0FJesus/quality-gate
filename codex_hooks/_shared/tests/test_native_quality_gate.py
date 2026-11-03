"""Behavioral checks for native Quality Gate subprocess output."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from _shared.native_quality_gate import run_released_command


class NativeQualityGateOutputTests(unittest.TestCase):
	def test_utf8_diagnostics_preserve_exit_status_with_legacy_parent_encoding(self) -> None:
		manifest = b'[quality]\npolicy_release = "v1.0.0"\n'
		message = "diagnostic: \u2500\u6f22"
		with tempfile.TemporaryDirectory(prefix="native-gate-utf8-") as directory:
			root = Path(directory)
			release = root / "quality-gate" / "releases" / "v1.0.0"
			release.mkdir(parents=True)
			wheel = release / "quality_gate-1.0.0-py3-none-any.whl"
			with zipfile.ZipFile(wheel, "w") as archive:
				archive.writestr("quality_gate/__init__.py", "")
				archive.writestr(
					"quality_gate/cli.py",
					"import sys\n"
					"message = 'diagnostic: \\u2500\\u6f22'\n"
					"print(message)\n"
					"print(message, file=sys.stderr)\n"
					"raise SystemExit(int(sys.argv[1]))\n",
				)
			digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
			(release / "release.toml").write_text(
				'[release]\nversion = "v1.0.0"\n'
				f'files = [{{path = "{wheel.name}", sha256 = "{digest}"}}]\n',
				encoding="utf-8",
			)
			environment = {
				"LOCALAPPDATA": str(root),
				"PYTHONIOENCODING": "cp1251:strict",
				"PYTHONDONTWRITEBYTECODE": "1",
			}
			with patch.dict(os.environ, environment):
				for exit_status in (0, 1):
					with self.subTest(exit_status=exit_status, capture_output=True):
						result = run_released_command(
							root, [str(exit_status)], manifest=manifest, capture_output=True
						)
						self.assertEqual(result.returncode, exit_status, result.stderr)
						self.assertEqual(result.stdout, message + "\n", result.stderr)
						self.assertEqual(result.stderr, message + "\n")
					with self.subTest(exit_status=exit_status, capture_output=False):
						result = subprocess.run(
							[
								sys.executable,
								"-B",
								"-c",
								"import sys; from pathlib import Path; "
								"from _shared.native_quality_gate import run_released_command; "
								"manifest = b'[quality]\\npolicy_release = \"v1.0.0\"\\n'; "
								"result = run_released_command(Path(sys.argv[1]), "
								"[sys.argv[2]], manifest=manifest); "
								"raise SystemExit(result.returncode)",
								str(root),
								str(exit_status),
							],
							cwd=Path(__file__).resolve().parents[2],
							capture_output=True,
							text=True,
							encoding="utf-8",
							check=False,
							timeout=30,
						)
						self.assertEqual(result.returncode, exit_status, result.stderr)
						self.assertEqual(result.stdout, message + "\n", result.stderr)
						self.assertEqual(result.stderr, message + "\n")


if __name__ == "__main__":
	unittest.main()
