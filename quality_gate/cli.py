"""Command-line entry point for the quality gate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from quality_gate.runner import QualityGateError, check, install_dependencies, validate


def parser() -> argparse.ArgumentParser:
	result = argparse.ArgumentParser(prog="quality-gate")
	result.add_argument("--root", type=Path, help="Repository root. Defaults to the Git root.")
	subcommands = result.add_subparsers(dest="command", required=True)
	check_parser = subcommands.add_parser("check", help="Run quality checks.")
	check_parser.add_argument(
		"--changed",
		action="store_true",
		help="Check staged Python files.",
	)
	subcommands.add_parser("validate", help="Validate the repository quality manifest.")
	subcommands.add_parser(
		"install-dependencies",
		help="Install development dependencies declared by the manifest.",
	)
	return result


def main() -> int:
	arguments = parser().parse_args()
	try:
		if arguments.command == "validate":
			validate(arguments.root)
		elif arguments.command == "install-dependencies":
			install_dependencies(arguments.root)
		else:
			check(arguments.root, changed=arguments.changed)
	except QualityGateError as error:
		report = (
			"QUALITY GATE FAILED\nCause:\n"
			f"{error}\nAction:\n"
			"Fix the reported configuration or check failure, then retry.\n"
		)
		sys.stdout.write(report)
		return 1
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
