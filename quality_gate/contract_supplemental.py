"""Validate bounded supplemental test expansion."""

from __future__ import annotations

from pathlib import Path

from .contract_models import SupplementalTest

MAX_SUPPLEMENTAL_TARGET_MATCHES = 1024
MAX_SUPPLEMENTAL_TARGET_BYTES = 64 * 1024
MAX_SUPPLEMENTAL_TOTAL_MATCHES = 4096
MAX_SUPPLEMENTAL_TOTAL_BYTES = 256 * 1024


class ValidationError(ValueError):
	"""A safe, path-aware contract validation error."""

	def __init__(self, path: str, message: str) -> None:
		self.path = path
		self.message = message
		super().__init__(f"{path}: {message}")


def _validate_supplemental_match(match: Path, root: Path, path: str) -> int:
	try:
		resolved_match = match.resolve()
	except OSError as exc:
		raise ValidationError(path, "must resolve inside the repository") from exc
	if resolved_match != root and root not in resolved_match.parents:
		raise ValidationError(path, "must match only paths inside the repository")
	return len(resolved_match.relative_to(root).as_posix().encode("utf-8"))


def _validate_supplemental_target(root: Path, target: str, path: str) -> tuple[int, int]:
	try:
		matches: list[Path] = []
		for match in root.glob(target):
			if len(matches) >= MAX_SUPPLEMENTAL_TARGET_MATCHES:
				raise ValidationError(
					path,
					f"must match at most {MAX_SUPPLEMENTAL_TARGET_MATCHES} repository paths",
				)
			matches.append(match)
	except ValidationError:
		raise
	except (OSError, ValueError) as exc:
		raise ValidationError(path, "must be a valid repository-relative target") from exc
	if not matches:
		raise ValidationError(path, "must match at least one repository path")
	matched_bytes = sum(_validate_supplemental_match(match, root, path) for match in matches)
	if matched_bytes > MAX_SUPPLEMENTAL_TARGET_BYTES:
		raise ValidationError(
			path, f"matched paths must use at most {MAX_SUPPLEMENTAL_TARGET_BYTES} bytes"
		)
	return len(matches), matched_bytes


def _validate_supplemental_targets(root: Path, tests: tuple[SupplementalTest, ...]) -> None:
	try:
		resolved_root = root.resolve()
	except OSError as exc:
		raise ValidationError("manifest", "repository root cannot be resolved") from exc
	total_matches = 0
	total_bytes = 0
	for test_index, supplemental_test in enumerate(tests):
		for target_index, target in enumerate(supplemental_test.targets):
			path = f"supplemental_tests[{test_index}].targets[{target_index}]"
			match_count, matched_bytes = _validate_supplemental_target(resolved_root, target, path)
			total_matches += match_count
			total_bytes += matched_bytes
			if total_matches > MAX_SUPPLEMENTAL_TOTAL_MATCHES:
				raise ValidationError(
					path,
					f"all targets must match at most {MAX_SUPPLEMENTAL_TOTAL_MATCHES} paths",
				)
			if total_bytes > MAX_SUPPLEMENTAL_TOTAL_BYTES:
				raise ValidationError(
					path,
					f"all matched paths must use at most {MAX_SUPPLEMENTAL_TOTAL_BYTES} bytes",
				)
