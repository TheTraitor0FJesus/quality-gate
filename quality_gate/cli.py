"""Command-line entry point for the quality gate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from quality_gate.contracts import ValidationError, Verdict, load_manifest
from quality_gate.distribution import DistributionError, PolicyCache
from quality_gate.launcher import prepare
from quality_gate.migration import migration_proposal
from quality_gate.reporting import render
from quality_gate.runner import QualityGateError, _error_result, check, validate
from quality_gate.runtime import RuntimeManager, RuntimeUnavailable, runtime_identity


def parser() -> argparse.ArgumentParser:
	result = argparse.ArgumentParser(prog="quality-gate")
	result.add_argument("--root", type=Path, help="Repository root. Defaults to the Git root.")
	subcommands = result.add_subparsers(dest="command", required=True)
	check_parser = subcommands.add_parser("check", help="Run quality checks.")
	check_parser.add_argument("--verbose", action="store_true", help="Show all redacted findings.")
	subcommands.add_parser("validate", help="Validate the repository quality manifest.")
	subcommands.add_parser("migrate", help="Print a read-only schema 1 migration proposal.")
	sync_parser = subcommands.add_parser(
		"sync", help="Install or roll back an immutable policy release."
	)
	sync_parser.add_argument("--source", type=Path, help="Release directory or zip archive.")
	sync_parser.add_argument("--url", help="Release zip URL. Network is used only by sync.")
	sync_parser.add_argument("--version", help="Require this exact release version.")
	sync_parser.add_argument("--rollback", nargs="?", const="", metavar="VERSION")
	sync_parser.add_argument("--cache-dir", type=Path)
	sync_parser.add_argument("--prune", action="store_true", help="Preview old release cleanup.")
	sync_parser.add_argument("--confirm", action="store_true", help="Apply an explicit prune.")
	doctor_parser = subcommands.add_parser(
		"doctor", help="Diagnose policy and runtime prerequisites."
	)
	doctor_parser.add_argument("--cache-dir", type=Path)
	setup_parser = subcommands.add_parser("setup", help="Prepare the pinned policy and runtimes.")
	setup_parser.add_argument("--cache-dir", type=Path)
	return result


def _sync(arguments: argparse.Namespace) -> None:
	cache = PolicyCache(arguments.cache_dir)
	if arguments.prune:
		if arguments.source or arguments.url or arguments.rollback is not None:
			raise DistributionError("prune cannot be combined with sync or rollback")
		candidates = cache.prune(confirm=arguments.confirm)
		prefix = "pruned" if arguments.confirm else "prune preview"
		sys.stdout.write(f"{prefix}: {', '.join(candidates) if candidates else 'nothing'}\n")
		return
	if arguments.rollback is not None:
		if arguments.source or arguments.url:
			raise DistributionError("rollback cannot be combined with a release source")
		version = arguments.rollback or None
		path = cache.rollback(version)
		sys.stdout.write(f"policy release selected: {path.name}\n")
		return
	if bool(arguments.source) == bool(arguments.url):
		raise DistributionError("sync requires exactly one of --source or --url")
	if arguments.url:
		manifest = cache.sync_url(arguments.url, version=arguments.version)
	else:
		manifest = cache.sync(arguments.source, version=arguments.version)
	sys.stdout.write(f"policy release synced: {manifest.version}\n")


def _doctor(root: Path | None, cache_dir: Path | None) -> int:
	actual_root = root.resolve() if root is not None else None
	try:
		from quality_gate.runner import repository_root

		actual_root = repository_root(actual_root)
		manifest = load_manifest(actual_root)
		cache = PolicyCache(cache_dir)
		cache.select(manifest.policy_release)
		manager = RuntimeManager(cache)
		missing: list[str] = []
		for component in manifest.python:
			inspection = manager.inspect(
				actual_root, runtime_identity(actual_root, manifest, component)
			)
			if not inspection.current:
				missing.append(f"{component.name}: {inspection.reason}")
		if missing:
			for item in missing:
				sys.stdout.write(f"runtime: unchecked - {item}; action: run setup\n")
			return 2
	except (ValidationError, DistributionError, RuntimeUnavailable) as error:
		sys.stdout.write(f"doctor: unchecked - {error}; action: run sync or setup\n")
		return 2
	sys.stdout.write(f"doctor: ready - {actual_root}\n")
	return 0


def _setup(root: Path | None, cache_dir: Path | None) -> None:
	from quality_gate.runner import repository_root

	actual_root = repository_root(root)
	environment = prepare(actual_root, cache_dir=cache_dir, create_runtimes=True)
	sys.stdout.write(f"setup: ready - {environment.manifest.policy_release}\n")


def main() -> int:
	arguments = parser().parse_args()
	try:
		if arguments.command == "validate":
			validate(arguments.root)
		elif arguments.command == "migrate":
			sys.stdout.write(migration_proposal(arguments.root))
		elif arguments.command == "sync":
			_sync(arguments)
		elif arguments.command == "doctor":
			return _doctor(arguments.root, arguments.cache_dir)
		elif arguments.command == "setup":
			_setup(arguments.root, arguments.cache_dir)
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
	except (QualityGateError, DistributionError, RuntimeUnavailable) as error:
		if isinstance(error, (DistributionError, RuntimeUnavailable)):
			error = QualityGateError(
				str(error),
				check_id="runtime.available",
				exit_code=2,
				recovery_action="run sync or setup and retry the quality gate",
			)
		sys.stdout.write(
			render(Verdict((_error_result(error),)), verbose=getattr(arguments, "verbose", False))
		)
		sys.stdout.write("\n")
		return error.exit_code
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
