#!/usr/bin/env python3
"""Codex hook adapter for migrated personal hooks."""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from importlib.util import find_spec
from pathlib import Path
from time import monotonic
from typing import Any, TextIO

from .command_safety import CommandViolation, evaluate_command
from .github_safety import GithubViolation, evaluate_github_tool
from .native_quality_gate import run_released_quality_gate

SCANNABLE_EXTENSIONS = {
	".py",
	".js",
	".ts",
	".jsx",
	".tsx",
	".mjs",
	".html",
	".htm",
	".css",
	".json",
	".yaml",
	".yml",
	".toml",
	".ini",
	".cfg",
	".md",
	".txt",
	".env",
	".sh",
	".bash",
	".ps1",
	".psm1",
}

BUILTIN_SECRET_PATTERNS = (
	("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
	(
		"AWS secret access key assignment",
		re.compile(r"(?i)\bAWS_SECRET_ACCESS_KEY\b\s*=\s*['\"][^'\"]{8,}['\"]"),
	),
	("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{20,}\b")),
	("OpenAI API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
	("Private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
)

DUMP_VERBS_RE = re.compile(
	r"\b(cat|head|tail|less|more|type|xxd|od|strings|base64|openssl|"
	r"Get-Content|gc|python3?|perl|php|node|ruby|awk|printf|dd|hexdump|"
	r"nl|tee|vi|vim|nano|emacs|view|ed|sed|zcat|bzcat|xzcat|jq|find|"
	r"cp|mv|install|Copy-Item|cp|Move-Item|Get-ChildItem|gci)\b",
	re.IGNORECASE,
)
OBFUSCATED_EXEC_RE = re.compile(
	r"(base64\s+(-d|--decode)|xxd\s+-r|openssl\s+enc\s+-d|"
	r"\biconv\b|\bunhex\b|\\x[0-9a-f]{2}|\$'\\x|"
	r"b64decode|fromhex|unhexlify|atob\(|Buffer\.from\([^)]*base64)",
	re.IGNORECASE,
)
CMD_SUBST_RE = re.compile(r"\$\(|`")
SENSITIVE_FILE_PATTERNS = (
	r"(?<![A-Za-z0-9_.-])\.env(?![A-Za-z0-9_.-])",
	r"\bsecrets?\b",
	r"\bcredentials?\b",
	r"\bid_rsa\b",
	r"\bid_ed25519\b",
	r"\.pem\b",
	r"\.key\b",
	r"\.p12\b",
	r"\.pfx\b",
	r"authorized_keys",
	r"\.pgpass\b",
	r"\bshadow\b",
	r"htpasswd",
	r"\.npmrc\b",
	r"\.pypirc\b",
	r"\.aws\b",
	r"\.ssh\b",
)
SENSITIVE_NAME_PATTERNS = {r"\bsecrets?\b", r"\bcredentials?\b"}
SAFE_ENV_TEMPLATE_SUFFIXES = {".dist", ".example", ".sample", ".template"}
SHELL_TOKEN_RE = re.compile(r"""(?:[^\s"'`;&|<>]+|"[^"]*"|'[^']*'|`[^`]*`)+|&&|\|\||[;&|<>\r\n]""")
SHELL_OPERATORS = {";", "&&", "||", "|", "&", "\r", "\n"}
TEXT_OUTPUT_COMMANDS = {"echo", "printf", "write-output", "write-host"}
COMMAND_WRAPPERS = {"sudo", "env", "command", "nohup"}
WRAPPER_VALUE_OPTIONS = {
	"-u",
	"--user",
	"--unset",
	"-g",
	"--group",
	"-h",
	"--host",
	"-p",
	"--prompt",
	"-C",
	"--close-from",
	"-D",
	"--chdir",
	"-r",
	"--role",
	"-t",
	"--type",
}
SSH_VALUE_OPTIONS = {
	"-i",
	"-F",
	"-p",
	"-l",
	"-o",
	"-J",
	"-S",
	"-b",
	"-c",
	"-D",
	"-E",
	"-L",
	"-R",
	"-W",
	"-w",
}
SSH_PATH_OPTIONS = {"-i", "-F", "-S", "-E"}
SEARCH_EXPRESSION_OPTIONS = {"-e", "--regexp", "--pattern", "-pattern", "--expression"}
SEARCH_COMMANDS = {
	"rg",
	"rg.exe",
	"grep",
	"grep.exe",
	"findstr",
	"findstr.exe",
	"select-string",
	"sls",
	"sed",
}
SEARCH_PATTERN_OPTIONS = {
	"-e",
	"--regexp",
	"--pattern",
	"-pattern",
	"--glob",
	"-g",
	"--iglob",
	"--exclude",
	"--exclude-dir",
	"--include",
	"-include",
}
SEARCH_VALUE_OPTIONS = {
	"-a",
	"--after-context",
	"-b",
	"--before-context",
	"-c",
	"--context",
}
SEARCH_PATH_OPTIONS = {
	"-path",
	"-literalpath",
	"--file",
	"-f",
}
PATH_VALUE_OPTIONS = {
	"-filter",
	"-path",
	"-literalpath",
	"--file",
}
LOCAL_ACCESS_COMMANDS = {
	"cat",
	"head",
	"tail",
	"less",
	"more",
	"type",
	"xxd",
	"od",
	"strings",
	"base64",
	"openssl",
	"get-content",
	"gc",
	"python",
	"python3",
	"perl",
	"php",
	"node",
	"ruby",
	"awk",
	"printf",
	"dd",
	"hexdump",
	"nl",
	"tee",
	"vi",
	"vim",
	"nano",
	"emacs",
	"view",
	"ed",
	"sed",
	"zcat",
	"bzcat",
	"xzcat",
	"jq",
	"find",
	"cp",
	"mv",
	"install",
	"copy-item",
	"move-item",
	"get-childitem",
	"gci",
}
SCRIPT_COMMANDS = {"python", "python3", "perl", "php", "node", "ruby"}
SCRIPT_CODE_OPTIONS = {"-c", "-e", "--eval", "--command"}
SCRIPT_FILE_ACCESS_RE = re.compile(
	r"(?i)\b(?:open|read_text|read_bytes|readfile|readfilesync|read_file|"
	r"write_text|write_bytes|get-content|cat|type)\b"
)
PYTHON_WRITE_METHODS = {"write_text", "write_bytes"}
RISKY_DIR_PATTERNS = (
	r"/usr/local/bin",
	r"/usr/local/sbin",
	r"/etc/cron\.",
	r"/etc/systemd/system",
	r"/root\b",
)
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.MULTILINE)
DIFF_FILE_RE = re.compile(r"^diff --git a/(.*?) b/(.*?)$", re.MULTILINE)

CODEX_HOME = Path.home() / ".codex"
RUFF_CONFIG = CODEX_HOME / "ruff.toml"
CODEX_GIT_CONFIG = CODEX_HOME / "MY-SETTINGS" / "gitconfig"
QUALITY_GATE_MANIFEST = "quality-gate.toml"
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__"}
MAX_TEST_DEPTH = 2
RUFF_TIMEOUT_SECONDS = 60
PYTEST_TIMEOUT_SECONDS = 300
MYPY_TIMEOUT_SECONDS = 120
POST_EDIT_BUDGET_SECONDS = 25
POST_EDIT_FAST_RUFF_TIMEOUT_SECONDS = 10
POST_EDIT_HEAVY_SECURITY = os.environ.get("CODEX_POST_EDIT_HEAVY_SECURITY") == "1"
GATE_SUMMARY_DETAIL_LIMIT = 1_500
PAYLOAD_PARSE_ERROR_KEY = "_codex_hook_payload_parse_error"


def as_text(value: object) -> str:
	"""Normalize subprocess and optional hook values without raising a second error."""
	if value is None:
		return ""
	if isinstance(value, str):
		return value
	if isinstance(value, bytes):
		return value.decode("utf-8", errors="replace")
	return str(value)


def load_payload() -> dict[str, Any]:
	raw = ""
	try:
		try:
			reconfigure = getattr(sys.stdin, "reconfigure", None)
			if callable(reconfigure):
				reconfigure(encoding="utf-8", errors="replace")
		except (AttributeError, OSError):
			pass
		raw = sys.stdin.read().lstrip("\ufeff")
		if not raw.strip():
			return {}
		starts = [idx for idx in (raw.find("{"), raw.find("[")) if idx != -1]
		if starts:
			raw = raw[min(starts) :]
		data = json.loads(raw)
	except Exception as exc:
		return {PAYLOAD_PARSE_ERROR_KEY: type(exc).__name__}
	if not isinstance(data, dict):
		return {PAYLOAD_PARSE_ERROR_KEY: "payload-root-is-not-an-object"}
	return data


def emit_json(value: dict[str, Any]) -> None:
	"""Write one hook protocol message to standard output."""
	sys.stdout.write(f"{json.dumps(value, ensure_ascii=False)}\n")


def codex_block(event: str, reason: str) -> None:
	if event == "PreToolUse":
		emit_json(
			{
				"hookSpecificOutput": {
					"hookEventName": "PreToolUse",
					"permissionDecision": "deny",
					"permissionDecisionReason": reason,
				}
			}
		)
		return
	else:
		emit_json({"decision": "block", "reason": reason})
	sys.exit(2)


def destructive_command_reason(violation: CommandViolation) -> str:
	return (
		"USER SAFETY HOOK blocked this command "
		f"[{violation.rule_id}: {violation.label}]. "
		"Do not retry or rephrase it. Continue with a safe alternative. "
		"If it is essential, show the exact command to the user and briefly explain "
		"why they must run it manually."
	)


def sensitive_access_reason(rule: str) -> str:
	return (
		f"USER SAFETY HOOK blocked sensitive-file access [{rule}]. "
		"Do not retry or use an equivalent access path. Continue without secrets. "
		"If that is impossible, ask the user for the minimum non-secret input needed."
	)


def github_safety_reason(violation: GithubViolation) -> str:
	return (
		"USER SAFETY HOOK blocked this GitHub MCP action "
		f"[{violation.rule_id}: {violation.label}]. Main is read-only. "
		"Continue on an explicit non-main branch and use a pull request. "
		"Do not retry through another tool. If the action is essential, give the "
		"user the exact action and briefly explain why they must perform it."
	)


def github_safety_violation(payload: dict[str, Any]) -> GithubViolation | None:
	tool_input = normalize_tool_input(payload.get("tool_input", {}))
	tool_name = str(payload.get("tool_name", ""))
	return evaluate_github_tool(tool_name, tool_input)


def matches_shell_block(command: str) -> str | None:
	violation = evaluate_command(command)
	return violation.rule_id if violation else None


def ssh_option_sensitive_path(option: str, value: str) -> str | None:
	if option == "-o":
		parts = re.split(r"[=\s]+", value, maxsplit=1)
		if parts[0].lower() not in {
			"identityfile",
			"certificatefile",
			"userknownhostsfile",
			"globalknownhostsfile",
		}:
			return None
		value = parts[1] if len(parts) > 1 else ""
	elif option not in SSH_PATH_OPTIONS:
		return None
	return sensitive_path_match(value)


def ssh_remote_start(tokens: list[str]) -> tuple[int, str | None]:
	"""Locate the destination after SSH options and inspect identity/config paths."""
	index = 1
	while index < len(tokens) and tokens[index].startswith("-"):
		raw_option = tokens[index]
		if raw_option == "--":
			return index + 2, None
		prefix = raw_option[: len("-i")]
		option = prefix if prefix in SSH_VALUE_OPTIONS else raw_option
		if option not in SSH_VALUE_OPTIONS:
			index += 1
			continue
		inline = raw_option[len(option) :]
		value = inline or (tokens[index + 1] if index + 1 < len(tokens) else "")
		if match := ssh_option_sensitive_path(option, value):
			return index, f"sensitive-file:{match}"
		index += 1 if inline else 2
	return index + 1, None


def conservative_sensitive_text(command: str) -> str | None:
	for word in shell_tokens(command):
		for part in word.split():
			if match := sensitive_path_match(part):
				return match
	return None


def remote_segment_risk(program: list[str], remote: str) -> str | None:
	if not program:
		return None
	verb = executable_name(program[0])
	if verb in TEXT_OUTPUT_COMMANDS and not CMD_SUBST_RE.search(remote):
		return None
	if verb not in LOCAL_ACCESS_COMMANDS | SEARCH_COMMANDS:
		if match := conservative_sensitive_text(" ".join(program)):
			return f"sensitive-file:{match}"
	if DUMP_VERBS_RE.search(" ".join(program)):
		if CMD_SUBST_RE.search(remote):
			return "obfuscated-path-construction"
		for pattern in RISKY_DIR_PATTERNS:
			if re.search(pattern, remote, re.IGNORECASE):
				return f"risky-dir-over-ssh:{pattern}"
	return None


def ssh_sensitive_access(tokens: list[str]) -> str | None:
	"""Check remote programs, retaining conservative treatment of unknown shells."""
	index, violation = ssh_remote_start(tokens)
	if violation:
		return violation
	remote = " ".join(tokens[index:])
	flattened = re.sub(r"['\"`]", "", remote)
	if OBFUSCATED_EXEC_RE.search(remote) or OBFUSCATED_EXEC_RE.search(flattened):
		return "obfuscated-decode-over-ssh"
	segments = shell_segments(remote)
	if match := local_sensitive_access(remote, segments):
		return f"sensitive-file:{match}"
	for segment in segments:
		if match := remote_segment_risk(command_tokens(segment), remote):
			return match
	return None


def transfer_operand_path(operand: str) -> str | None:
	if not operand.startswith("-"):
		return operand.rsplit(":", 1)[-1]
	name, _, value = operand.partition("=")
	if name in {"--files-from", "--include-from", "--exclude-from"}:
		return value
	if operand[: len("-i")] in {"-i", "-F", "-b"}:
		return operand[len("-i") :]
	return None


def transfer_sensitive_access(tokens: list[str], command: str) -> str | None:
	options_ended = False
	for operand in tokens[1:]:
		if operand == "--":
			options_ended = True
			continue
		path = operand.rsplit(":", 1)[-1] if options_ended else transfer_operand_path(operand)
		if path is not None and (match := sensitive_path_match(path)):
			return f"sensitive-file:{match}"
	if OBFUSCATED_EXEC_RE.search(command):
		return "obfuscated-decode-over-ssh"
	return None


def remote_command_access(tokens: list[str], command: str) -> str | None:
	verb = executable_name(tokens[0])
	if re.match(
		r"^(?:if|then|else|elif|for|foreach|while|until|do|try|catch|finally)\b|^[(){}]", verb
	):
		for index, token in enumerate(tokens):
			if executable_name(token) in {"ssh", "scp", "sftp", "rsync"}:
				if match := remote_command_access(tokens[index:], command):
					return match
	elif verb in {"scp", "sftp", "rsync"}:
		return transfer_sensitive_access(tokens, command)
	elif verb == "ssh":
		return ssh_sensitive_access(tokens)
	return None


def matches_secret_exfil(command: str, segments: list[list[str]] | None = None) -> str | None:
	"""Inspect actual remote commands instead of remote-looking text arguments."""
	for segment in shell_segments(command) if segments is None else segments:
		tokens = command_tokens(segment)
		if not tokens:
			continue
		if match := remote_command_access(tokens, command):
			return match
	return None


def has_scratch_path_segment(value: str) -> bool:
	"""Return whether a normalized path is inside a ``.scratch`` directory."""
	cleaned = value.strip().strip("'\"`").rstrip(",;)")
	if not cleaned:
		return False

	parts: list[str] = []
	for component in cleaned.replace("\\", "/").split("/"):
		component = component.casefold()
		if component in {"", "."}:
			continue
		if component == "..":
			if parts:
				parts.pop()
			continue
		parts.append(component)
	return ".scratch" in parts


def matches_env_access(value: str) -> bool:
	"""Return whether a tool argument refers to a runtime environment file."""
	if has_scratch_path_segment(value):
		return False
	name = Path(value).name.lower()
	if name.startswith(".env.") and Path(name).suffix in SAFE_ENV_TEMPLATE_SUFFIXES:
		return False
	return name == ".env" or name == ".envrc" or name.startswith(".env.")


def shell_tokens(command: str) -> list[str]:
	"""Tokenize common shell arguments without interpreting command text."""
	return [token.strip("'\"`") for token in SHELL_TOKEN_RE.findall(command or "")]


def shell_segments(command: str) -> list[list[str]]:
	"""Find unquoted command boundaries in one pass, retaining quotes on operands."""
	segments: list[list[str]] = []
	current: list[str] = []
	for token in SHELL_TOKEN_RE.findall(command or ""):
		if token in SHELL_OPERATORS:
			if current:
				segments.append(current)
				current = []
		else:
			current.append(token)
	if current:
		segments.append(current)
	return segments


def executable_name(token: str) -> str:
	return token.strip("'\"`").rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower().removesuffix(".exe")


def command_tokens(segment: list[str]) -> list[str]:
	"""Unwrap common command prefixes without searching ordinary argument positions."""
	tokens = [token.strip("'\"`") for token in segment]
	index = 0
	while index < len(tokens):
		if re.match(r"^[A-Za-z_]\w*=", tokens[index]):
			index += 1
			continue
		verb = executable_name(tokens[index])
		if verb not in COMMAND_WRAPPERS:
			return tokens[index:]
		index += 1
		if verb == "command" and index < len(tokens) and tokens[index] in {"-v", "-V"}:
			return []
		while index < len(tokens) and tokens[index].startswith("-"):
			if tokens[index] in WRAPPER_VALUE_OPTIONS:
				index += 1
			index += 1
	return []


def redirect_sensitive_path(segment: list[str]) -> str | None:
	for index, token in enumerate(segment[:-1]):
		if token in {"<", ">"}:
			if match := sensitive_path_match(segment[index + 1]):
				return match
	return None


def sensitive_path_match(value: str, *, check_names: bool = True) -> str | None:
	"""Return a sensitive-file pattern only when the value looks like a path."""
	cleaned = value.strip().strip("'\"`").rstrip(",;)")
	if not cleaned or cleaned.startswith("!"):
		return None
	if has_scratch_path_segment(cleaned):
		return None
	if matches_env_access(cleaned):
		return "runtime-env-file"
	parent, _, basename = cleaned.replace("\\", "/").rpartition("/")
	# Source modules and descriptive documents are not credential stores by name.
	ordinary_file = basename.lower().endswith(".py") or (
		basename.lower().endswith(".md")
		and not re.fullmatch(r"(?:secrets?|credentials?)\.md", basename, re.IGNORECASE)
	)
	for pattern in SENSITIVE_FILE_PATTERNS:
		if not check_names and pattern in SENSITIVE_NAME_PATTERNS:
			continue
		subject = parent if ordinary_file and pattern in SENSITIVE_NAME_PATTERNS else cleaned
		if re.search(pattern, subject, re.IGNORECASE):
			return pattern
	return None


def search_option(token: str) -> tuple[str | None, str | None]:
	lowered = token.lower()
	name, separator, _ = lowered.partition("=")
	if name in SEARCH_EXPRESSION_OPTIONS:
		return "expression", token.split("=", 1)[1] if separator else None
	if lowered in SEARCH_PATTERN_OPTIONS:
		return "pattern", None
	if lowered in SEARCH_PATH_OPTIONS:
		return "path", None
	if lowered in SEARCH_VALUE_OPTIONS:
		return "value", None
	for kind, options in (
		("pattern", SEARCH_PATTERN_OPTIONS),
		("path", SEARCH_PATH_OPTIONS),
		("value", SEARCH_VALUE_OPTIONS),
	):
		if separator and name in options:
			return kind, token.split("=", 1)[1]
	if token.startswith("-e") and len(token) > len("-e"):
		return "expression", token[len("-e") :]
	if token.startswith("-f") and len(token) > len("-f"):
		return "path", token[len("-f") :]
	return None, None


def search_sensitive_path(tokens: list[str]) -> str | None:  # noqa: C901
	"""Check path operands while ignoring search expressions and glob values."""
	pattern_seen = False
	options_ended = False
	option_value: str | None = None
	for token in tokens[1:]:
		if option_value is not None:
			if option_value == "path":
				match = sensitive_path_match(token)
				if match:
					return match
			option_value = None
			continue
		option, inline_value = (None, None) if options_ended else search_option(token)
		if option is not None:
			if option == "expression":
				pattern_seen = True
			if option == "path" and token.split("=", 1)[0] in {"-f", "--file"}:
				pattern_seen = True
			if option == "path" and inline_value is not None:
				match = sensitive_path_match(inline_value)
				if match:
					return match
			elif inline_value is None:
				option_value = option
			continue
		if token == "--" and not options_ended:  # noqa: S105 -- shell argument separator
			options_ended = True
			continue
		if token.startswith("-") and not options_ended:
			continue
		if not pattern_seen:
			pattern_seen = True
			continue
		match = sensitive_path_match(token)
		if match:
			return match
	return None


def python_path_values(expression: ast.AST, bindings: dict[str, set[str]]) -> set[str]:
	"""Snapshot literal path components and aliases at the point of assignment."""
	values: set[str] = set()
	for node in ast.walk(expression):
		if isinstance(node, ast.Constant) and isinstance(node.value, str):
			values.add(node.value)
		elif isinstance(node, ast.Name):
			values.update(bindings.get(node.id, set()))
	return values


def python_scope_names(node: ast.AST) -> set[str]:
	"""Collect local stores while excluding nested lexical scopes."""
	names: set[str] = set()
	pending = list(ast.iter_child_nodes(node))
	while pending:
		child = pending.pop()
		if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
			names.add(child.name)
			continue
		if isinstance(child, ast.Lambda):
			continue
		if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
			names.add(child.id)
		if isinstance(child, ast.arg):
			names.add(child.arg)
		pending.extend(ast.iter_child_nodes(child))
	return names


class PythonPathVisitor(ast.NodeVisitor):
	"""Inspect source order and lexical scopes; merge uncertain control-flow values."""

	def __init__(self) -> None:
		self.bindings: dict[str, set[str]] = {}
		self.match: str | None = None

	def visit(self, node: ast.AST) -> None:
		if self.match is None:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
				self.visit_scope(node)
			elif isinstance(
				node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.Try, ast.TryStar, ast.Match)
			):
				self.visit_branches(node)
			else:
				super().visit(node)

	def bind(self, target: ast.AST, expression: ast.AST | None) -> None:
		values = python_path_values(expression, self.bindings) if expression else set()
		for node in ast.walk(target):
			if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
				self.bindings[node.id] = values.copy()

	def visit_Assign(self, node: ast.Assign) -> None:
		self.visit(node.value)
		for target in node.targets:
			self.bind(target, node.value)

	def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
		if node.value:
			self.visit(node.value)
		self.bind(node.target, node.value)

	def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
		self.visit(node.value)
		self.bind(node.target, node.value)

	def visit_scope(self, node: ast.AST) -> None:
		outer = self.bindings
		self.bindings = outer.copy()
		for name in python_scope_names(node):
			self.bindings[name] = set()
		self.generic_visit(node)
		self.bindings = outer

	def visit_branches(self, node: ast.AST) -> None:
		for field in ("test", "iter", "subject"):
			if expression := getattr(node, field, None):
				self.visit(expression)
		before = self.bindings.copy()
		branches = [getattr(node, field, []) for field in ("body", "orelse", "finalbody")]
		branches.extend(
			[child] for field in ("handlers", "cases") for child in getattr(node, field, [])
		)
		states = [before]
		for branch in branches:
			self.bindings = before.copy()
			for child in branch:
				self.visit(child)
			states.append(self.bindings)
		self.bindings = {}
		for state in states:
			for name, values in state.items():
				self.bindings.setdefault(name, set()).update(values)

	def visit_Call(self, node: ast.Call) -> None:
		function = node.func
		name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
		if SCRIPT_FILE_ACCESS_RE.fullmatch(name):
			for expression in python_file_expressions(node, name):
				for value in sorted(python_path_values(expression, self.bindings)):
					match = sensitive_path_match(
						value, check_names=name not in PYTHON_WRITE_METHODS
					)
					if match:
						self.match = match
						return
		self.generic_visit(node)


def python_file_expressions(node: ast.Call, name: str) -> list[ast.expr]:
	"""Separate a method receiver from content/encoding arguments."""
	expressions = node.args[:1]
	if isinstance(node.func, ast.Attribute):
		expressions = [node.func.value]
		if name not in {"read_text", "read_bytes", *PYTHON_WRITE_METHODS}:
			expressions.extend(node.args[:1])
	expressions.extend(
		keyword.value for keyword in node.keywords if keyword.arg in {"file", "path"}
	)
	return expressions


def python_sensitive_path(code: str) -> str | None:
	"""Inspect file operations without executing Python or resolving dynamic calls."""
	try:
		tree = ast.parse(code)
	except (SyntaxError, ValueError):
		return sensitive_path_match(code) if SCRIPT_FILE_ACCESS_RE.search(code) else None
	visitor = PythonPathVisitor()
	visitor.visit(tree)
	return visitor.match


def local_command_sensitive_path(  # noqa: C901
	tokens: list[str], index: int, verb: str
) -> str | None:
	for operand_index, operand in enumerate(tokens[index + 1 :], start=index + 1):
		# PowerShell accepts comma-separated path lists as a single operand.
		if verb in {"get-content", "gc"} and "," in operand:
			for path_operand in operand.split(","):
				if match := sensitive_path_match(path_operand):
					return match
			continue
		lowered = operand.lower()
		if verb in SCRIPT_COMMANDS and lowered in SCRIPT_CODE_OPTIONS:
			if operand_index + 1 < len(tokens):
				code = tokens[operand_index + 1]
				if verb in {"python", "python3"}:
					if match := python_sensitive_path(code):
						return match
					continue
				if SCRIPT_FILE_ACCESS_RE.search(code):
					match = sensitive_path_match(code)
					if match:
						return match
			continue
		if (
			verb in SCRIPT_COMMANDS
			and operand_index > index + 1
			and tokens[operand_index - 1].lower() in SCRIPT_CODE_OPTIONS
		):
			continue
		if lowered in PATH_VALUE_OPTIONS and operand_index + 1 < len(tokens):
			match = sensitive_path_match(tokens[operand_index + 1])
			if match:
				return match
			continue
		if lowered.startswith(tuple(f"{option}=" for option in PATH_VALUE_OPTIONS)):
			match = sensitive_path_match(operand.split("=", 1)[1])
			if match:
				return match
			continue
		if lowered.startswith("-"):
			continue
		match = sensitive_path_match(operand)
		if match:
			return match
	return None


def git_search_tokens(tokens: list[str]) -> list[str]:
	index = 1
	while index < len(tokens) and tokens[index].startswith("-"):
		if tokens[index] in {"-C", "-c", "--git-dir", "--work-tree"}:
			index += 1
		index += 1
	return tokens[index:] if index < len(tokens) and tokens[index] == "grep" else []


def segment_sensitive_access(tokens: list[str], command: str) -> str | None:
	if not tokens:
		return None
	verb = executable_name(tokens[0])
	if verb == "git":
		return search_sensitive_path(grep) if (grep := git_search_tokens(tokens)) else None
	if verb in SEARCH_COMMANDS:
		return search_sensitive_path(tokens)
	if verb in TEXT_OUTPUT_COMMANDS:
		return conservative_sensitive_text(command) if CMD_SUBST_RE.search(command) else None
	if verb in LOCAL_ACCESS_COMMANDS:
		return local_command_sensitive_path(tokens, 0, verb)
	if re.match(
		r"^(?:if|then|else|elif|for|foreach|while|until|do|try|catch|finally)\b|^[(){}]", verb
	):
		# Compound shell syntax retains the conservative scan for nested operations.
		for index, token in enumerate(tokens):
			nested_verb = executable_name(token)
			if nested_verb in LOCAL_ACCESS_COMMANDS:
				if match := local_command_sensitive_path(tokens, index, nested_verb):
					return match
	return None


def local_sensitive_access(command: str, segments: list[list[str]] | None = None) -> str | None:
	"""Inspect command positions and redirects after a single boundary pass."""
	for segment in shell_segments(command) if segments is None else segments:
		if match := redirect_sensitive_path(segment):
			return match
		if match := segment_sensitive_access(command_tokens(segment), " ".join(segment)):
			return match
	return None


def normalize_tool_input(tool_input: object) -> dict[str, Any]:
	return tool_input if isinstance(tool_input, dict) else {}


def extract_patch_paths(command: str) -> list[str]:
	paths: list[str] = []
	for match in PATCH_FILE_RE.finditer(command or ""):
		paths.append(match.group(1).strip())
	for match in DIFF_FILE_RE.finditer(command or ""):
		paths.append(match.group(2).strip())
	return unique(paths)


def is_patch_payload(command: str) -> bool:
	"""Return whether command text is an apply-patch payload, not executable shell."""
	return bool(PATCH_FILE_RE.search(command or "") or DIFF_FILE_RE.search(command or ""))


def unique(values: list[str]) -> list[str]:
	seen: set[str] = set()
	result: list[str] = []
	for value in values:
		key = value.lower()
		if key not in seen:
			seen.add(key)
			result.append(value)
	return result


def resolve_path(path_text: str, cwd: str) -> Path:
	p = Path(os.path.expanduser(path_text.strip().strip('"')))
	if not p.is_absolute():
		p = Path(cwd) / p
	return p.resolve()


def changed_paths(payload: dict[str, Any], *, base_cwd: str | None = None) -> list[Path]:
	tool_input = normalize_tool_input(payload.get("tool_input", {}))
	cwd = base_cwd or str(payload.get("cwd") or os.getcwd())
	candidates: list[str] = []

	for key in ("file_path", "path"):
		value = tool_input.get(key)
		if isinstance(value, str) and value:
			candidates.append(value)

	command = tool_input.get("command")
	if isinstance(command, str):
		candidates.extend(extract_patch_paths(command))

	paths: list[Path] = []
	for candidate in unique(candidates):
		try:
			paths.append(resolve_path(candidate, cwd))
		except OSError:
			continue
	return unique_paths(paths)


def unique_paths(paths: list[Path]) -> list[Path]:
	seen: set[str] = set()
	result: list[Path] = []
	for path in paths:
		key = str(path).lower()
		if key not in seen:
			seen.add(key)
			result.append(path)
	return result


def run_safety(payload: dict[str, Any]) -> None:
	if parse_error := payload.get(PAYLOAD_PARSE_ERROR_KEY):
		codex_block(
			"PreToolUse",
			"USER SAFETY HOOK blocked this tool call [invalid-hook-payload: "
			f"{parse_error}]. The safety payload could not be parsed. Do not retry the "
			"command blindly; report this hook parsing error to the user.",
		)
		return
	if violation := github_safety_violation(payload):
		codex_block("PreToolUse", github_safety_reason(violation))
		return
	run_local_safety(payload)


def run_local_safety(payload: dict[str, Any]) -> None:
	"""Apply command and local-file checks after host-tool policy passes."""
	tool_input = normalize_tool_input(payload.get("tool_input", {}))
	command = str(tool_input.get("command", ""))
	is_patch = is_patch_payload(command)
	base_cwd = str(
		tool_input.get("workdir") or tool_input.get("cwd") or payload.get("cwd") or os.getcwd()
	)
	branch_cache: dict[str, str | None] = {}

	def branch_lookup(repository_path: str | None) -> str | None:
		try:
			cwd = str(resolve_path(repository_path or ".", base_cwd))
		except OSError:
			return None
		if cwd not in branch_cache:
			branch_cache[cwd] = current_git_branch(cwd)
		return branch_cache[cwd]

	if not is_patch and (violation := evaluate_command(command, branch_lookup=branch_lookup)):
		codex_block("PreToolUse", destructive_command_reason(violation))
		return
	segments = shell_segments(command) if not is_patch else []
	if not is_patch and (sensitive_match := local_sensitive_access(command, segments)):
		codex_block(
			"PreToolUse",
			sensitive_access_reason(f"sensitive-file-access:{sensitive_match}"),
		)
		return
	if not is_patch and (exfiltration_match := matches_secret_exfil(command, segments)):
		codex_block(
			"PreToolUse",
			sensitive_access_reason(exfiltration_match),
		)
		return

	for path in changed_paths(payload, base_cwd=base_cwd):
		path_text = str(path)
		if matches_env_access(path_text):
			codex_block(
				"PreToolUse",
				sensitive_access_reason("runtime-env-file"),
			)
			return
		if sensitive_match := sensitive_path_match(path_text):
			codex_block(
				"PreToolUse",
				sensitive_access_reason(f"sensitive-file-access:{sensitive_match}"),
			)
			return


def run_semgrep(file_path: Path) -> list[dict[str, Any]]:
	semgrep_bin = shutil.which("semgrep")
	if not semgrep_bin:
		return [
			{
				"severity": "LOW",
				"tool": "HOOK",
				"line": "-",
				"message": (
					"semgrep not installed - SAST scan skipped. Install: pip install semgrep"
				),
				"check_id": "setup",
			}
		]
	try:
		result = subprocess.run(
			[
				semgrep_bin,
				"--config=p/default",
				"--json",
				"--quiet",
				str(file_path),
			],
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			timeout=90,
		)
	except subprocess.TimeoutExpired:
		return [
			{
				"severity": "LOW",
				"tool": "HOOK",
				"line": "-",
				"message": "semgrep timed out after 90s - SAST scan skipped.",
				"check_id": "timeout",
			}
		]
	findings: list[dict[str, Any]] = []
	try:
		data = json.loads(as_text(result.stdout))
		for item in data.get("results", []):
			extra = item.get("extra", {})
			severity = str(extra.get("severity", "WARNING")).upper()
			if severity == "WARNING":
				severity = "MEDIUM"
			findings.append(
				{
					"severity": severity,
					"tool": "SAST",
					"line": item.get("start", {}).get("line", "?"),
					"message": str(extra.get("message", "")).split("\n")[0],
					"check_id": item.get("check_id", ""),
				}
			)
	except Exception:
		if result.returncode != 0:
			detail = (
				(as_text(result.stderr) or as_text(result.stdout) or "unknown error")
				.strip()
				.splitlines()[0]
			)
			return [
				{
					"severity": "LOW",
					"tool": "HOOK",
					"line": "-",
					"message": f"semgrep failed - SAST scan skipped. {detail}",
					"check_id": "runtime",
				}
			]
	return findings


def run_detect_secrets(file_path: Path) -> list[dict[str, Any]]:
	if find_spec("detect_secrets") is None:
		return [
			{
				"severity": "LOW",
				"tool": "HOOK",
				"line": "-",
				"message": (
					"detect-secrets not installed - secret scan skipped. "
					"Install: pip install detect-secrets"
				),
				"check_id": "setup",
			}
		]
	try:
		result = subprocess.run(
			[sys.executable, "-m", "detect_secrets", "scan", file_path.name],
			cwd=str(file_path.parent),
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			timeout=15,
		)
	except subprocess.TimeoutExpired:
		return [
			{
				"severity": "LOW",
				"tool": "HOOK",
				"line": "-",
				"message": "detect-secrets timed out after 15s - secret scan skipped.",
				"check_id": "timeout",
			}
		]
	tool_error = {
		"severity": "LOW",
		"tool": "HOOK",
		"line": "-",
		"message": "Optional scanner failed or returned an invalid report.",
		"check_id": "runtime",
	}
	findings: list[dict[str, Any]] = []
	try:
		data = json.loads(as_text(result.stdout))
		for _filename, secrets in data["results"].items():
			for secret in secrets:
				findings.append(
					{
						"severity": "CRITICAL",
						"tool": "SECRET",
						"line": secret.get("line_number", "?"),
						"message": f"Secret detected: {secret.get('type', 'unknown secret')}",
						"check_id": secret.get("type", "secret"),
					}
				)
	except (ValueError, TypeError, KeyError, AttributeError):
		findings.append(tool_error)
		return findings
	if result.returncode != 0:
		findings.append(tool_error)
	return findings


def run_builtin_secret_scan(file_path: Path) -> list[dict[str, Any]]:
	try:
		content = file_path.read_text(encoding="utf-8", errors="ignore")
	except OSError:
		return []
	findings: list[dict[str, Any]] = []
	for line_number, line in enumerate(content.splitlines(), start=1):
		for name, pattern in BUILTIN_SECRET_PATTERNS:
			if pattern.search(line):
				findings.append(
					{
						"severity": "CRITICAL",
						"tool": "SECRET",
						"line": line_number,
						"message": f"Secret detected: {name}",
						"check_id": name,
					}
				)
	return findings


def git_env() -> dict[str, str]:
	env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
	if not env.get("GIT_CONFIG_GLOBAL") and CODEX_GIT_CONFIG.exists():
		env["GIT_CONFIG_GLOBAL"] = str(CODEX_GIT_CONFIG)
	return env


def scan_security(path: Path, *, heavy: bool = False) -> tuple[bool, str | None]:
	if not path.exists() or not path.is_file():
		return False, None
	extension_key = path.suffix.lower() if path.suffix else path.name.lower()
	if extension_key not in SCANNABLE_EXTENSIONS:
		return False, None
	findings = run_builtin_secret_scan(path)
	if heavy:
		findings = run_semgrep(path) + run_detect_secrets(path) + findings
	if not findings:
		return False, None
	lines = ["Security scan findings", f"File: {path}", ""]
	for finding in findings:
		lines.append(f"[{finding['severity']}] line {finding['line']}: {finding['message']}")
	return True, "\n".join(lines)


def format_python(path: Path) -> tuple[bool, str | None]:
	if path.suffix.lower() != ".py" or not path.exists() or not path.is_file():
		return False, None
	command_base = [sys.executable, "-m", "ruff"]
	config_args = ["--config", str(RUFF_CONFIG)] if RUFF_CONFIG.exists() else []
	try:
		check_result = subprocess.run(
			[
				*command_base,
				"check",
				"--fix",
				"--output-format",
				"concise",
				*config_args,
				str(path),
			],
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			timeout=POST_EDIT_FAST_RUFF_TIMEOUT_SECONDS,
		)
		if check_result.returncode != 0:
			detail = (as_text(check_result.stdout) + as_text(check_result.stderr)).strip()
			return True, (
				f"Ruff check failed\nFile: {path}\n\n"
				f"{detail or 'ruff check returned a non-zero exit code.'}"
			)
		format_result = subprocess.run(
			[*command_base, "format", *config_args, str(path)],
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			timeout=POST_EDIT_FAST_RUFF_TIMEOUT_SECONDS,
		)
		if format_result.returncode != 0:
			detail = (as_text(format_result.stdout) + as_text(format_result.stderr)).strip()
			return True, (
				f"Ruff format failed\nFile: {path}\n\n"
				f"{detail or 'ruff format returned a non-zero exit code.'}"
			)
	except FileNotFoundError:
		return True, f"Ruff is unavailable\nFile: {path}\n\nInstall Ruff before continuing."
	except subprocess.TimeoutExpired:
		return True, f"Ruff timed out\nFile: {path}\n\nThe 10-second limit was exceeded."
	return False, None


def run_post_edit(payload: dict[str, Any]) -> None:
	paths = changed_paths(payload)
	deadline = monotonic() + POST_EDIT_BUDGET_SECONDS
	reports: list[str] = []
	high = False
	for path in paths:
		if monotonic() >= deadline:
			reports.append("Post-edit checks reached the 25s budget; remaining files skipped.")
			break
		ruff_failed, ruff_report = format_python(path)
		if ruff_report:
			reports.append(ruff_report)
		high = high or ruff_failed
		is_high, report = scan_security(path, heavy=POST_EDIT_HEAVY_SECURITY)
		if report:
			reports.append(report)
		high = high or is_high
	if not reports:
		return
	report_text = "\n\n".join(reports)
	if high:
		emit_json({"decision": "block", "reason": report_text})
		sys.exit(2)
	emit_json({"systemMessage": report_text})


def run_git(args: list[str], cwd: str, timeout: int = 5) -> tuple[int, str]:
	try:
		result = subprocess.run(
			["git", *args],
			cwd=cwd,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			timeout=timeout,
			env=git_env(),
		)
		return result.returncode, as_text(result.stdout).strip()
	except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
		return 1, ""


def current_git_branch(cwd: str) -> str | None:
	"""Return the checked-out branch, an empty detached state, or ``None`` on failure."""
	returncode, branch = run_git(["branch", "--show-current"], cwd)
	return branch if returncode == 0 else None


def repo_root() -> str:
	result = subprocess.run(
		["git", "rev-parse", "--show-toplevel"],
		capture_output=True,
		text=True,
		encoding="utf-8",
		errors="replace",
		timeout=10,
		env=git_env(),
	)
	return os.path.normpath(as_text(result.stdout).strip()) if result.returncode == 0 else ""


def venv_python(root: str) -> str | None:
	for candidate in (
		os.path.join(root, ".venv", "Scripts", "python.exe"),
		os.path.join(root, ".venv", "bin", "python"),
	):
		if os.path.isfile(candidate):
			return candidate
	return None


def staged_files(root: str) -> set[str]:
	result = subprocess.run(
		["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
		cwd=root,
		capture_output=True,
		text=True,
		encoding="utf-8",
		errors="replace",
		timeout=10,
		env=git_env(),
	)
	if result.returncode != 0:
		return set()
	return {os.path.abspath(os.path.join(root, rel)) for rel in as_text(result.stdout).splitlines()}


def has_ruff_config(root: str) -> bool:
	for dirpath, dirnames, filenames in os.walk(root):
		dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
		if "ruff.toml" in filenames:
			return True
		if "pyproject.toml" in filenames:
			try:
				if "[tool.ruff]" in Path(dirpath, "pyproject.toml").read_text(
					encoding="utf-8", errors="replace"
				):
					return True
			except OSError:
				pass
	return False


def find_baseline_config(root: str) -> str | None:
	candidate = os.path.join(root, ".github", "ruff-baseline.toml")
	return candidate if os.path.isfile(candidate) else None


def find_project_ruff_dir(root: str) -> str | None:
	for dirpath, dirnames, filenames in os.walk(root):
		dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
		if "ruff.toml" in filenames:
			return dirpath
	return None


def filter_py_files_under(files: set[str], target_dir: str) -> list[str]:
	target_dir_abs = os.path.abspath(target_dir)
	result: list[str] = []
	for abs_path in files:
		if not abs_path.endswith(".py"):
			continue
		if not abs_path.startswith(target_dir_abs + os.sep) and abs_path != target_dir_abs:
			continue
		if os.path.isfile(abs_path):
			result.append(os.path.relpath(abs_path, target_dir_abs))
	return result


def run_ruff_commands(
	commands: tuple[list[str], ...], cwd: str, interpreter: str
) -> tuple[str, str]:
	for command in commands:
		try:
			result = subprocess.run(
				command,
				cwd=cwd,
				capture_output=True,
				text=True,
				encoding="utf-8",
				errors="replace",
				timeout=RUFF_TIMEOUT_SECONDS,
			)
		except FileNotFoundError:
			return (
				"block",
				f"ruff not found ({interpreter} -m ruff). Install it with: pip install ruff.",
			)
		except subprocess.TimeoutExpired:
			return "block", "ruff check timed out after 60s. Commit blocked."
		if result.returncode != 0:
			return "block", (as_text(result.stdout) + as_text(result.stderr)).strip()
	return "pass", ""


def run_ruff_gate(root: str, files: set[str]) -> tuple[str, str]:
	if not has_ruff_config(root):
		return "skip", ""
	interpreter = venv_python(root) or sys.executable
	baseline = find_baseline_config(root)
	if baseline is not None:
		commands = (
			[
				interpreter,
				"-m",
				"ruff",
				"check",
				"--output-format",
				"concise",
				"--config",
				baseline,
				".",
			],
			[
				interpreter,
				"-m",
				"ruff",
				"format",
				"--check",
				"--config",
				baseline,
				".",
			],
		)
		return run_ruff_commands(commands, root, interpreter)
	project_dir = find_project_ruff_dir(root)
	cwd = project_dir or root
	targets = filter_py_files_under(files, cwd)
	if not targets:
		return "skip", ""
	command = [interpreter, "-m", "ruff", "check", "--output-format", "concise", *targets]
	return run_ruff_commands((command,), cwd, interpreter)


def find_test_dir(root: str) -> str | None:
	for current_root, dirs, files in os.walk(root):
		rel = os.path.relpath(current_root, root)
		depth = 0 if rel == "." else rel.count(os.sep) + 1
		if os.path.basename(current_root) == "tests" and any(
			f.startswith("test_") and f.endswith(".py") for f in files
		):
			return current_root
		if depth >= MAX_TEST_DEPTH:
			dirs[:] = []
	return None


def run_pytest_gate(root: str, files: set[str]) -> tuple[str, str]:
	test_dir = find_test_dir(root)
	if not test_dir:
		return "skip", ""
	env = dict(os.environ)
	env["PYTHONIOENCODING"] = "utf-8"
	try:
		result = subprocess.run(
			[
				venv_python(root) or sys.executable,
				"-m",
				"pytest",
				test_dir,
				"-p",
				"no:cacheprovider",
				"--continue-on-collection-errors",
				"-q",
			],
			cwd=root,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			timeout=PYTEST_TIMEOUT_SECONDS,
			env=env,
		)
	except FileNotFoundError:
		return "skip", ""
	except subprocess.TimeoutExpired:
		return "block", "pytest timed out after 300s. Commit blocked."
	if result.returncode == 0:
		return "pass", ""
	if "No module named pytest" in as_text(result.stderr):
		return "skip", ""
	return "block", (as_text(result.stdout) + as_text(result.stderr)).strip()


def find_mypy_root(root: str) -> str | None:
	for dirpath, dirnames, filenames in os.walk(root):
		dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
		if "mypy.ini" in filenames:
			return dirpath
		if "pyproject.toml" in filenames:
			try:
				if "[tool.mypy]" in Path(dirpath, "pyproject.toml").read_text(
					encoding="utf-8", errors="replace"
				):
					return dirpath
			except OSError:
				pass
	return None


def run_mypy_gate(root: str, files: set[str]) -> tuple[str, str]:
	mypy_root = find_mypy_root(root)
	if not mypy_root:
		return "skip", ""
	targets = filter_py_files_under(files, mypy_root)
	if not targets:
		return "skip", ""
	interpreter = venv_python(root) or sys.executable
	try:
		result = subprocess.run(
			[interpreter, "-m", "mypy", *targets],
			cwd=mypy_root,
			capture_output=True,
			text=True,
			encoding="utf-8",
			errors="replace",
			timeout=MYPY_TIMEOUT_SECONDS,
		)
	except FileNotFoundError:
		return "skip", ""
	except subprocess.TimeoutExpired:
		return "block", "mypy timed out after 120s. Commit blocked."
	if result.returncode == 0:
		return "pass", ""
	return "block", (as_text(result.stdout) + as_text(result.stderr)).strip()


GATES: dict[str, Callable[[str, set[str]], tuple[str, str]]] = {
	"ruff": run_ruff_gate,
	"pytest": run_pytest_gate,
	"mypy": run_mypy_gate,
}


def run_gate_safely(
	name: str,
	fn: Callable[[str, set[str]], tuple[str, str]],
	root: str,
	files: set[str],
) -> tuple[str, str]:
	try:
		return fn(root, files)
	except Exception as exc:
		return "block", f"{name} gate crashed: {exc}"


def run_shared_quality_gate(root: str) -> tuple[str, str]:
	"""Run the immutable v2 release declared by the repository."""
	if not Path(root, QUALITY_GATE_MANIFEST).is_file():
		return "skip", ""
	try:
		result = run_released_quality_gate(Path(root), staged=True, capture_output=True)
	except (OSError, ValueError) as error:
		return "block", f"quality-gate is unchecked: {error}"
	if result.returncode == 0:
		return "pass", ""
	return "block", (as_text(result.stdout) + as_text(result.stderr)).strip()


def summarize_gate_detail(detail: str | None) -> str:
	"""Keep a failure reason useful when the command runner drops hook stderr."""
	normalized = " ".join(as_text(detail).split())
	if not normalized:
		return "The gate failed without diagnostic output."
	if len(normalized) <= GATE_SUMMARY_DETAIL_LIMIT:
		return normalized
	return f"{normalized[: GATE_SUMMARY_DETAIL_LIMIT - 3]}..."


def gate_recovery_action(name: str, detail: str | None) -> str:
	lower_detail = as_text(detail).lower()
	if name == "pytest" and (
		"temporary directory" in lower_detail or "permissionerror" in lower_detail
	):
		return "Set writable TMP and TEMP values for the global pre-commit hook, then retry."
	return "Fix the reported failure, then retry the commit."


def format_git_gate_summary(name: str, detail: str | None) -> str:
	"""Return the actionable failure report that command runners expose to agents."""
	return "\n".join(
		[
			f"COMMIT BLOCKED: {name.upper()}",
			"Cause:",
			summarize_gate_detail(detail),
			"Action:",
			gate_recovery_action(name, detail),
		]
	)


def emit_gate_report(message: str | None, *, stream: TextIO | None = None) -> None:
	"""Print a gate report without failing on a legacy Windows console encoding."""
	target = sys.stdout if stream is None else stream
	encoding = target.encoding or "utf-8"
	safe_message = as_text(message).encode(encoding, errors="backslashreplace").decode(encoding)
	print(safe_message, file=target)


def run_git_pre_commit() -> None:
	root = repo_root()
	if not root:
		sys.exit(0)
	files = staged_files(root)
	if not files:
		sys.exit(0)
	shared_status, shared_detail = run_shared_quality_gate(root)
	if shared_status == "pass":
		sys.exit(0)
	if shared_status == "block":
		report = format_git_gate_summary("quality-gate", shared_detail)
		emit_gate_report(report)
		emit_gate_report(
			f"QUALITY-GATE FAILED - COMMIT BLOCKED\n{shared_detail}", stream=sys.stderr
		)
		sys.exit(1)
	with ThreadPoolExecutor(max_workers=3) as executor:
		futures = {
			name: executor.submit(run_gate_safely, name, fn, root, files)
			for name, fn in GATES.items()
		}
		results = {name: future.result() for name, future in futures.items()}
	failed = [name for name, (status, _) in results.items() if status == "block"]
	if not failed:
		sys.exit(0)
	summaries = [format_git_gate_summary(name, results[name][1]) for name in failed]
	full_reports = [
		f"{name.upper()} FAILED - COMMIT BLOCKED\n{results[name][1]}" for name in failed
	]
	emit_gate_report("\n\n".join(summaries))
	emit_gate_report("\n\n".join(full_reports), stream=sys.stderr)
	emit_gate_report("Fix the findings before commit.", stream=sys.stderr)
	sys.exit(1)


def main() -> None:
	mode = sys.argv[1] if len(sys.argv) > 1 else ""
	payload = load_payload()
	if mode == "pre_tool_use":
		run_safety(payload)
	elif mode == "post_edit":
		run_post_edit(payload)
	elif mode == "git_pre_commit":
		run_git_pre_commit()


if __name__ == "__main__":
	main()
