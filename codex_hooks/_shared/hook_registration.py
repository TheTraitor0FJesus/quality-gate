"""Small, exact-registration helpers for independently managed Codex hooks."""

from __future__ import annotations

import base64
import json
import os
import shlex
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from _shared.quality_settings import install_transaction


def codex_home() -> Path:
	value = os.environ.get("CODEX_HOME")
	if value:
		return Path(value).expanduser().resolve()
	profile = os.environ.get("USERPROFILE") or os.environ.get("HOME")
	return (Path(profile).expanduser() if profile else Path.home()) / ".codex"


def registration_group(
	*,
	matcher: str,
	command_path: Path,
	windows_executable: Path,
	windows_arguments: Sequence[str] = (),
	status_message: str,
	timeout: int,
) -> dict[str, Any]:
	handler: dict[str, Any] = {
		"type": "command",
		"command": shlex.join(["python3", "-B", str(command_path)]),
		"statusMessage": status_message,
		"timeout": timeout,
	}
	if os.name == "nt":
		handler["commandWindows"] = _windows_program_command(windows_executable, windows_arguments)
	return {"matcher": matcher, "hooks": [handler]}


def _powershell_string(value: str) -> str:
	return "'" + value.replace("'", "''") + "'"


def _windows_program_command(executable: Path, arguments: Sequence[str]) -> str:
	values = ", ".join(_powershell_string(argument) for argument in arguments)
	script = "\n".join(
		(
			"$ErrorActionPreference = 'Stop'",
			f"$executable = {_powershell_string(str(executable))}",
			f"$arguments = @({values})",
			"if (-not (Test-Path -LiteralPath $executable -PathType Leaf)) { exit 2 }",
			"& $executable @arguments",
			"if ($null -eq $LASTEXITCODE) { exit 2 }",
			"exit $LASTEXITCODE",
		)
	)
	encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
	return subprocess.list2cmdline(
		["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
	)


def read_configuration(root: Path) -> dict[str, Any]:
	path = root / "hooks.json"
	if not path.exists():
		return {"hooks": {}}
	try:
		configuration = json.loads(path.read_text(encoding="utf-8"))
	except (OSError, UnicodeError, json.JSONDecodeError) as error:
		raise ValueError("hooks.json is not readable JSON") from error
	if not isinstance(configuration, dict):
		raise ValueError("hooks.json must contain a JSON object")
	if not isinstance(configuration.get("hooks", {}), dict):
		raise ValueError("hooks.json hooks entry must be an object")
	configuration.setdefault("hooks", {})
	return configuration


def write_configuration(root: Path, configuration: dict[str, Any]) -> None:
	path = root / "hooks.json"
	content = json.dumps(configuration, ensure_ascii=False, indent=2) + "\n"
	temporary_path: Path | None = None
	try:
		with tempfile.NamedTemporaryFile(
			mode="w", encoding="utf-8", newline="\n", dir=root, delete=False
		) as temporary:
			temporary.write(content)
			temporary_path = Path(temporary.name)
		os.replace(temporary_path, path)
	except OSError:
		if temporary_path is not None:
			temporary_path.unlink(missing_ok=True)
		raise


def installed(root: Path, event: str, group: dict[str, Any]) -> bool:
	configuration = read_configuration(root)
	entries = configuration["hooks"].get(event, [])
	if not isinstance(entries, list):
		raise ValueError(f"hooks.json {event} entries must be an array")
	return group in entries


def install(root: Path, event: str, group: dict[str, Any]) -> bool:
	with install_transaction(root):
		configuration = read_configuration(root)
		entries = configuration["hooks"].setdefault(event, [])
		if not isinstance(entries, list):
			raise ValueError(f"hooks.json {event} entries must be an array")
		if group in entries:
			return False
		entries.append(group)
		write_configuration(root, configuration)
		return True


def uninstall(root: Path, event: str, group: dict[str, Any]) -> bool:
	with install_transaction(root):
		configuration = read_configuration(root)
		entries = configuration["hooks"].get(event, [])
		if not isinstance(entries, list):
			raise ValueError(f"hooks.json {event} entries must be an array")
		filtered = [entry for entry in entries if entry != group]
		if len(filtered) == len(entries):
			return False
		if filtered:
			configuration["hooks"][event] = filtered
		else:
			configuration["hooks"].pop(event, None)
		write_configuration(root, configuration)
		return True
