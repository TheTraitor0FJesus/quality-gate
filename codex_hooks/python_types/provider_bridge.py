"""Run the public edit-types command from an exact verified engine wheel."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in {None, ""}:
	sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.quality_settings import RELEASE, default_settings_path


def main() -> int:
	parser = argparse.ArgumentParser()
	parser.add_argument("--release", required=True)
	arguments = parser.parse_args()
	if not RELEASE.fullmatch(arguments.release):
		return 2
	root = default_settings_path().parents[2]
	sys.path.insert(0, str(root / "hooks"))
	try:
		from _shared.native_quality_gate import run_released_command

		manifest = f'[quality]\npolicy_release = "{arguments.release}"\n'.encode()
		result = run_released_command(Path.cwd(), ["edit-types"], manifest=manifest)
	except (ImportError, OSError, ValueError):
		return 2
	return result.returncode


if __name__ == "__main__":
	raise SystemExit(main())
