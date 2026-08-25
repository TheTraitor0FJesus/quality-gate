"""Command-line entry point for the quality gate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from quality_gate.contracts import ValidationError, Verdict
from quality_gate.migration import migration_proposal
from quality_gate.reporting import render
from quality_gate.runner import QualityGateError, _error_result, check, validate


def parser() -> argparse.ArgumentParser:
	result = argparse.ArgumentParser(prog="quality-gate")
	result.add_argument("--root", type=Path, help="Repository root. Defaults to the Git root.")
	subcommands = result.add_subparsers(dest="command", required=True)
	check_parser = subcommands.add_parser("check", help="Run quality checks.")
	check_parser.add_argument("--verbose", action="store_true", help="Show all redacted findings.")
	subcommands.add_parser("validate", help="Validate the repository quality manifest.")
	subcommands.add_parser("migrate", help="Print a read-only schema 1 migration proposal.")
	return result


def main() -> int:
	arguments = parser().parse_args()
	try:
		if arguments.command == "validate":
			validate(arguments.root)
		elif arguments.command == "migrate":
			sys.stdout.write(migration_proposal(arguments.root))
		else:
			verdict = check(arguments.root, verbose=arguments.verbose)
			if verdict.exit_code:
				return verdict.exit_code
	except ValidationError as error:
		action = (
			"review the schema 1 manifest and run migrate"
			if error.path == "quality.schema"
			else f"correct {error.path} and run validate"
		)
		sys.stdout.write(f"manifest: unchecked - {error.message}; action: {action}\n")
		return 2
	except QualityGateError as error:
		sys.stdout.write(
			render(Verdict((_error_result(error),)), verbose=getattr(arguments, "verbose", False))
		)
		sys.stdout.write("\n")
		return error.exit_code
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
