"""Primitive schema validation and component path rules."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from pathlib import Path
from typing import cast

from .contract_supplemental import ValidationError

MAX_COMPONENT_STUB_ROOTS = 32
MAX_COMPONENT_SOURCE_ROOTS = 32
ASCII_C0_END = 32
ASCII_DELETE = 127


def _type(value: object, expected: type | tuple[type, ...], path: str) -> object:
	allows_bool = expected is bool or (isinstance(expected, tuple) and bool in expected)
	if not isinstance(value, expected) or (isinstance(value, bool) and not allows_bool):
		names = (
			", ".join(t.__name__ for t in expected)
			if isinstance(expected, tuple)
			else expected.__name__
		)
		raise ValidationError(path, f"expected {names}")
	return value


def _table(value: object, path: str) -> dict[str, object]:
	return cast(dict[str, object], _type(value, dict, path))


def _keys(table: Mapping[str, object], allowed: set[str], path: str) -> None:
	unknown = sorted(set(table) - allowed)
	if unknown:
		raise ValidationError(path, f"unknown field {unknown[0]!r}")


def _string(value: object, path: str) -> str:
	result = cast(str, _type(value, str, path))
	if not result.strip():
		raise ValidationError(path, "must not be empty")
	return result


def _list(value: object, path: str) -> list[object]:
	return cast(list[object], _type(value, list, path))


def _ensure_unique(values: Iterable[str], path: str, message: str) -> None:
	values = tuple(values)
	if len(set(values)) != len(values):
		raise ValidationError(path, message)


def _relative_path(value: object, path: str) -> str:
	result = _string(value, path).replace("\\", "/")
	candidate = Path(result)
	if (
		"\x00" in result
		or ":" in result
		or candidate.is_absolute()
		or bool(candidate.drive)
		or result == "."
		or any(part in {".", ".."} for part in candidate.parts)
	):
		raise ValidationError(path, "must be a repository-relative path")
	return result


def _positive_int(value: object, path: str) -> int:
	result = cast(int, _type(value, int, path))
	if result <= 0:
		raise ValidationError(path, "must be greater than zero")
	return result


def _date(value: object, path: str) -> date:
	raw = _string(value, path)
	try:
		return date.fromisoformat(raw)
	except ValueError as exc:
		raise ValidationError(path, "must use YYYY-MM-DD") from exc


def _component_stub_roots(value: object, path: str) -> tuple[str, ...]:
	roots = tuple(
		_string(item, f"{path}[{index}]") for index, item in enumerate(_list(value, path))
	)
	if len(roots) > MAX_COMPONENT_STUB_ROOTS:
		raise ValidationError(path, f"must contain at most {MAX_COMPONENT_STUB_ROOTS} paths")
	_ensure_unique(roots, path, "must contain unique paths")
	for index, root in enumerate(roots):
		if "\\" in root or ":" in root or any(part in {"", ".", ".."} for part in root.split("/")):
			raise ValidationError(f"{path}[{index}]", "must be a component-relative POSIX path")
	return roots


def _component_source_roots(value: object, path: str, component_path: str) -> tuple[str, ...]:
	roots = tuple(
		_string(item, f"{path}[{index}]") for index, item in enumerate(_list(value, path))
	)
	if len(roots) > MAX_COMPONENT_SOURCE_ROOTS:
		raise ValidationError(path, f"must contain at most {MAX_COMPONENT_SOURCE_ROOTS} paths")
	_ensure_unique(roots, path, "must contain unique paths")
	if len({root.casefold() for root in roots}) != len(roots):
		raise ValidationError(path, "must contain unique paths")
	if component_path == "." and roots:
		raise ValidationError(
			path, "source roots cannot be declared for a repository-root component"
		)
	paths = [component_path, *roots]
	for index, root in enumerate(roots):
		if (
			"\\" in root
			or ":" in root
			or any(
				ord(character) < ASCII_C0_END or ord(character) == ASCII_DELETE
				for character in root
			)
			or any(part in {"", ".", ".."} for part in root.split("/"))
		):
			raise ValidationError(
				f"{path}[{index}]", "must be a repository-relative POSIX directory"
			)
	for index, root in enumerate(roots):
		canonical = root.casefold()
		if any(
			canonical == other.casefold()
			or canonical.startswith(f"{other.casefold()}/")
			or other.casefold().startswith(f"{canonical}/")
			for other in paths[: index + 1]
		):
			raise ValidationError(
				path, "source roots must not overlap each other or the component path"
			)
	return tuple(sorted(roots))
