#!/usr/bin/env python3
"""Independently register or remove the post-edit hook."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _shared.hook_registration import (  # noqa: E402
	codex_home,
	install,
	installed,
	registration_group,
	uninstall,
)


def group(root: Path, source: Path | None = None) -> dict:
	hooks = (source or Path(__file__).resolve().parents[1]).expanduser().resolve()
	python_quality = hooks / "python_quality"
	registration = registration_group(
		matcher="apply_patch",
		command_path=python_quality / "post_edit_checks.py",
		windows_executable=Path(sys.executable),
		windows_arguments=("-B", str(python_quality / "post_edit_checks.py")),
		status_message="Post-edit checks...",
		timeout=120,
	)
	return registration


def main() -> int:
	parser = argparse.ArgumentParser(prog="install-post-edit-hook")
	parser.add_argument("action", choices=("install", "uninstall", "status"))
	parser.add_argument("--root", type=Path, default=codex_home())
	arguments = parser.parse_args()
	root = arguments.root.expanduser().resolve()
	try:
		selected = group(root)
		if arguments.action == "install":
			changed = install(root, "PostToolUse", selected)
			message = "installed" if changed else "already installed"
		elif arguments.action == "uninstall":
			changed = uninstall(root, "PostToolUse", selected)
			message = "removed" if changed else "not installed"
		else:
			message = "installed" if installed(root, "PostToolUse", selected) else "not installed"
	except (OSError, ValueError) as error:
		sys.stderr.write(f"Post-edit hook registration failed: {error}\n")
		return 2
	sys.stdout.write(f"Post-edit hook {message} in {root / 'hooks.json'}\n")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
