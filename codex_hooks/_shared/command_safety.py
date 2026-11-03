"""Classify destructive shell commands before Codex executes them."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class CommandViolation:
	"""A matched user-owned safety rule."""

	rule_id: str
	label: str


@dataclass(frozen=True)
class CommandRule:
	"""A declarative destructive-command rule."""

	violation: CommandViolation
	pattern: re.Pattern[str]


def _compile(pattern: str) -> re.Pattern[str]:
	return re.compile(pattern, re.IGNORECASE)


_SEGMENT_TAIL = r"[^;&|\r\n]*"
_GIT = (
	r"(?<![\w.-])git(?:\.exe)?"
	r"(?:\s+(?:-C\s+\S+|-c\s+\S+|--(?:git-dir|work-tree)(?:=\S+|\s+\S+)"
	r"|--(?:no-pager|paginate|literal-pathspecs|no-optional-locks)))*\s+"
)
_SHARED_BRANCH = r"(?:[\s/:+])(?:main|master|production)(?=\s|$)"

_DOWNLOAD_PIPE_RE = _compile(
	r"(?<![\w.-])(?:curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod|irm)(?:\.exe)?\b"
	r"[^|;&\r\n]*\|\s*(?:(?:sudo|env)\s+)*"
	r"(?:ba|da|z|fi|k)?sh\b|"
	r"(?<![\w.-])(?:curl|wget|Invoke-WebRequest|iwr|Invoke-RestMethod|irm)(?:\.exe)?\b"
	r"[^|;&\r\n]*\|\s*(?:powershell|pwsh|Invoke-Expression|iex|python3?|node)\b"
)

_TEXT_ONLY_COMMAND_RE = _compile(
	r"^\s*(?:echo|printf|Write-Output|grep|rg|findstr|Select-String|sls)\b|"
	rf"^\s*{_GIT}commit\b"
)

_PLAIN_TEXT_COMMAND_RE = _compile(
	r"^\s*(?:echo|printf|Write-Output|grep|rg|findstr|Select-String|sls)\b"
)
_GIT_START_RE = _compile(
	r"(?<![\w./\\-])git(?:\.exe)?(?![\w.-])|"
	r"(?<=[/\\])git(?:\.exe)?(?=(?:[\"']\s|\s))"
)
_SHELL_TOKEN_RE = re.compile(r"""(?:[^\s"']+|"[^"]*"|'[^']*')+""")
_MAIN_REF_RE = _compile(r"^(?:refs/heads/)?main$")
_GIT_GLOBAL_OPTIONS_WITH_VALUE = {
	"-c",
	"--exec-path",
	"--git-dir",
	"--namespace",
	"--super-prefix",
	"--work-tree",
}
_PUSH_OPTIONS_WITH_VALUE = {
	"--exec",
	"--push-option",
	"--receive-pack",
	"--repo",
	"-o",
}
_COMPACT_GIT_OPTION_PREFIX_LENGTH = 2
_PUSH_REPOSITORY_AND_REF_COUNT = 2

BranchLookup = Callable[[str | None], str | None]


@dataclass(frozen=True)
class GitInvocation:
	"""Parsed Git invocation needed by the main-branch policy."""

	subcommand: str
	args: tuple[str, ...]
	repository_path: str | None
	unsupported_repository_selector: bool = False


_RULES = (
	CommandRule(
		CommandViolation("git-reset-hard", "discard all local changes"),
		_compile(rf"{_GIT}reset\b(?={_SEGMENT_TAIL}(?<!\S)--hard(?=\s|$))"),
	),
	CommandRule(
		CommandViolation("recursive-or-forced-delete", "recursive or forced file deletion"),
		_compile(
			r"(?<![\w.-])rm(?:\.exe)?\b"
			r"(?=[^;&|\r\n]*(?:--(?:recursive|force)(?=\s|$)|-[A-Za-z]*[rf][A-Za-z]*(?=\s|$)))"
		),
	),
	CommandRule(
		CommandViolation("git-clean-force", "delete untracked files"),
		_compile(
			rf"{_GIT}clean\b(?={_SEGMENT_TAIL}"
			r"(?:--force(?=\s|$)|-[A-Za-z]*f[A-Za-z]*(?=\s|$)))"
		),
	),
	CommandRule(
		CommandViolation("git-discard-worktree", "discard the working tree"),
		_compile(rf"{_GIT}(?:checkout|restore)\b{_SEGMENT_TAIL}(?:--\s+)?\.(?=\s|$)"),
	),
	CommandRule(
		CommandViolation("git-filter-history", "rewrite repository history"),
		_compile(rf"{_GIT}filter-(?:branch|repo)\b"),
	),
	CommandRule(
		CommandViolation("git-force-push-shared", "force-push to a shared branch"),
		_compile(
			rf"{_GIT}push\b"
			rf"(?={_SEGMENT_TAIL}(?:--force(?:-with-lease)?(?:=\S+)?(?=\s|$)"
			rf"|-f(?=\s|$)|\+\S+))(?={_SEGMENT_TAIL}{_SHARED_BRANCH})"
		),
	),
	CommandRule(
		CommandViolation("drop-database-object", "drop a database object"),
		_compile(r"\bdrop\s+(?:table|database|schema|index)\b"),
	),
	CommandRule(
		CommandViolation("truncate-table", "truncate a database table"),
		_compile(r"\btruncate\s+(?:table\s+)?[\"`\w]"),
	),
	CommandRule(
		CommandViolation("wipe-datastore", "wipe a datastore"),
		_compile(r"(?<![\w.-])flush(?:all|db)(?=\s|$)"),
	),
	CommandRule(
		CommandViolation("delete-cloud-resource", "delete or terminate an AWS resource"),
		_compile(r"(?<![\w.-])aws(?:\.exe)?\b[^;&|\r\n]*\b(?:delete|terminate)-[\w-]+\b"),
	),
	CommandRule(
		CommandViolation("delete-cluster-resource", "delete a Kubernetes resource"),
		_compile(r"(?<![\w.-])kubectl(?:\.exe)?\b[^;&|\r\n]*\bdelete\b"),
	),
	CommandRule(
		CommandViolation("destroy-infrastructure", "destroy managed infrastructure"),
		_compile(r"(?<![\w.-])(?:terraform|tofu)(?:\.exe)?\b[^;&|\r\n]*\bdestroy\b"),
	),
	CommandRule(
		CommandViolation("destructive-docker-command", "delete or prune Docker state"),
		_compile(
			r"(?<![\w.-])docker(?:\.exe)?\b[^;&|\r\n]*"
			r"(?:\b(?:system|builder|container|image|volume|network)\s+(?:prune|rm)\b"
			r"|\bcompose\b[^;&|\r\n]*\b(?:down|rm)\b|\b(?:rmi|rm)\b)"
		),
	),
	CommandRule(
		CommandViolation("world-writable-permissions", "grant world-writable permissions"),
		_compile(r"(?<![\w.-])chmod(?:\.exe)?\b[^;&|\r\n]*(?:\b777\b|a\+rwx)"),
	),
	CommandRule(
		CommandViolation("raw-block-device-write", "write directly to a block device"),
		_compile(r"(?:>|\bof=)\s*/dev/(?:sd|nvme|hd|disk|vd)\S*"),
	),
	CommandRule(
		CommandViolation("raw-disk-operation", "clear or format a Windows disk or volume"),
		_compile(r"(?<![\w.-])(?:Clear-Disk|Format-Volume|diskpart)(?:\.exe)?\b"),
	),
)


def _shell_segments(command: str) -> list[str]:
	"""Split common shell chains while preserving quoted wrapper commands."""
	segments: list[str] = []
	buffer: list[str] = []
	quote: str | None = None
	escaped = False
	for char in command:
		if escaped:
			buffer.append(char)
			escaped = False
			continue
		if char == "\\" and quote == '"':
			buffer.append(char)
			escaped = True
			continue
		if char in {"'", '"'}:
			buffer.append(char)
			if quote is None:
				quote = char
			elif quote == char:
				quote = None
			continue
		if quote is None and char in ";&|\r\n":
			segment = "".join(buffer).strip()
			if segment:
				segments.append(segment)
			buffer.clear()
			continue
		buffer.append(char)
	segment = "".join(buffer).strip()
	if segment:
		segments.append(segment)
	return segments


def _is_text_only_command(segment: str) -> bool:
	if "$(" in segment or "`" in segment:
		return False
	return bool(_TEXT_ONLY_COMMAND_RE.search(segment))


def _shell_tokens(command: str) -> list[str]:
	return [token.strip("'\"") for token in _SHELL_TOKEN_RE.findall(command)]


def _git_invocations(segment: str) -> list[GitInvocation]:  # noqa: C901
	invocations: list[GitInvocation] = []
	matches = list(_GIT_START_RE.finditer(segment))
	if matches and not segment[: matches[0].start()].strip():
		# Once the segment starts with Git, later matches belong to its arguments.
		# In particular, a commit message must not be parsed as another command.
		matches = matches[:1]
	for match in matches:
		tokens = _shell_tokens(segment[match.end() :])
		repository_path: str | None = None
		unsupported_selector = False
		index = 0
		while index < len(tokens):
			token = tokens[index]
			lowered = token.lower()
			if token == "-C":  # noqa: S105 -- Git working-directory option
				if index + 1 >= len(tokens):
					break
				repository_path = tokens[index + 1]
				index += 2
				continue
			if token.startswith("-C") and len(token) > _COMPACT_GIT_OPTION_PREFIX_LENGTH:
				repository_path = token[2:]
				index += 1
				continue
			if lowered == "-c":
				if index + 1 >= len(tokens):
					break
				if tokens[index + 1].lower().startswith("alias."):
					return [GitInvocation("", (), repository_path, True)]
				index += 2
				continue
			if lowered in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
				index += 2
				unsupported_selector = unsupported_selector or lowered in {
					"--git-dir",
					"--work-tree",
				}
				continue
			if lowered.startswith("-c"):
				if lowered[2:].startswith("alias."):
					return [GitInvocation("", (), repository_path, True)]
				index += 1
				continue
			if lowered.startswith(("--git-dir=", "--work-tree=")):
				unsupported_selector = True
				index += 1
				continue
			if token.startswith("-"):
				index += 1
				continue
			invocations.append(
				GitInvocation(
					token.lower(),
					tuple(tokens[index + 1 :]),
					repository_path,
					unsupported_selector,
				)
			)
			break
	return invocations


def _is_main_ref(value: str) -> bool:
	return bool(_MAIN_REF_RE.fullmatch(value.lstrip("+")))


def _references_main(args: tuple[str, ...]) -> bool:
	return any(_is_main_ref(arg) for arg in args if not arg.startswith("-"))


def _push_targets_main(args: tuple[str, ...]) -> bool:
	if any(arg.lower() in {"--all", "--mirror"} for arg in args):
		return True
	delete_mode = any(arg.lower() in {"--delete", "-d"} for arg in args)
	positionals: list[str] = []
	index = 0
	while index < len(args):
		arg = args[index]
		lowered = arg.lower()
		if lowered in _PUSH_OPTIONS_WITH_VALUE:
			index += 2
			continue
		if any(lowered.startswith(f"{option}=") for option in _PUSH_OPTIONS_WITH_VALUE):
			index += 1
			continue
		if not arg.startswith("-"):
			positionals.append(arg)
		index += 1
	if len(positionals) < _PUSH_REPOSITORY_AND_REF_COUNT:
		return False
	for refspec in positionals[1:]:
		if delete_mode and _is_main_ref(refspec):
			return True
		destination = refspec.rsplit(":", 1)[-1]
		if _is_main_ref(destination):
			return True
	return False


def _push_is_forced(args: tuple[str, ...]) -> bool:
	return any(
		arg.lower() in {"--force", "--mirror", "-f"}
		or arg.lower().startswith("--force-with-lease")
		or (not arg.startswith("-") and arg.startswith("+"))
		for arg in args
	)


def _push_deletes_main(args: tuple[str, ...]) -> bool:
	delete_mode = any(arg.lower() in {"--delete", "-d"} for arg in args)
	null_refspec = any(arg.startswith(":") and _is_main_ref(arg.rsplit(":", 1)[-1]) for arg in args)
	return null_refspec or (delete_mode and _push_targets_main(args))


def _branch_destroys_main(args: tuple[str, ...]) -> bool:
	positionals = [arg for arg in args if not arg.startswith("-")]
	if any(arg in {"--delete", "-d", "-D"} for arg in args):
		return any(_is_main_ref(arg) for arg in positionals)
	if any(arg in {"--force", "-f"} for arg in args):
		return bool(positionals and _is_main_ref(positionals[0]))
	if any(arg in {"--move", "-m", "-M"} for arg in args):
		return bool(positionals and _is_main_ref(positionals[0])) or (
			"-M" in args and len(positionals) > 1 and _is_main_ref(positionals[1])
		)
	return False


def _checkout_resets_main(args: tuple[str, ...]) -> bool:
	return any(arg in {"-B", "-C"} for arg in args) and _references_main(args)


def _checkout_discards_worktree(subcommand: str, args: tuple[str, ...]) -> bool:
	if any(arg.lower() in {"--discard-changes", "--force", "-f"} for arg in args):
		return True
	if subcommand == "restore":
		return "--staged" not in args or "--worktree" in args
	return "--" in args and bool(args[args.index("--") + 1 :])


def _current_main_violation(
	invocation: GitInvocation, branch_lookup: BranchLookup | None
) -> CommandViolation | None:
	if branch_lookup is None:
		return None
	branch = branch_lookup(invocation.repository_path)
	if branch is None:
		return CommandViolation(
			"git-branch-state-unknown",
			"run a destructive Git operation while the current branch could not be verified",
		)
	if branch.casefold() == "main":
		return CommandViolation("git-main-protected", "destructively modify the main branch")
	return None


def _git_policy_violation(  # noqa: C901
	invocation: GitInvocation, branch_lookup: BranchLookup | None
) -> CommandViolation | None:
	protected = CommandViolation("git-main-protected", "destructively modify the main branch")
	subcommand = invocation.subcommand
	args = invocation.args
	if not subcommand or invocation.unsupported_repository_selector:
		return None
	if subcommand in {"filter-branch", "filter-repo"}:
		return CommandViolation("git-filter-history", "rewrite repository history")
	if subcommand == "prune":
		return CommandViolation("git-prune", "delete unreachable Git objects")
	if subcommand == "reflog" and args and args[0].lower() in {"delete", "expire"}:
		return CommandViolation("git-reflog-delete", "delete Git recovery history")
	if subcommand == "stash" and args and args[0].lower() in {"clear", "drop"}:
		return CommandViolation("git-stash-delete", "delete stashed changes")
	if subcommand == "tag" and any(arg.lower() in {"--delete", "-d"} for arg in args):
		return CommandViolation("git-tag-delete", "delete a Git tag")
	if subcommand == "remote" and args and args[0].lower() in {"remove", "rm"}:
		return CommandViolation("git-remote-remove", "delete a Git remote configuration")
	if subcommand in {"checkout", "restore", "switch"} and _checkout_discards_worktree(
		subcommand, args
	):
		return CommandViolation("git-discard-worktree", "discard working-tree changes")
	if subcommand == "branch" and _branch_destroys_main(args):
		return protected
	if subcommand in {"checkout", "switch"} and _checkout_resets_main(args):
		return protected
	if subcommand == "push" and _push_targets_main(args):
		if _push_is_forced(args):
			return CommandViolation("git-force-push-shared", "force-push to the main branch")
		if _push_deletes_main(args):
			return protected
	if subcommand == "push" and any(arg.lower() == "--prune" for arg in args):
		return CommandViolation("git-push-prune", "delete remote refs, potentially including main")
	if subcommand == "update-ref":
		return protected if "--stdin" in args or _references_main(args) else None
	if subcommand == "refs" and args and args[0].lower() == "delete" and _references_main(args):
		return protected
	contextual_destructive = (
		subcommand in {"rebase", "reset"}
		or (subcommand == "commit" and any(arg.lower() == "--amend" for arg in args))
		or (subcommand == "pull" and any(arg.lower().startswith("--rebase") for arg in args))
		or (subcommand == "push" and _push_is_forced(args))
	)
	return _current_main_violation(invocation, branch_lookup) if contextual_destructive else None


def evaluate_command(
	command: str, *, branch_lookup: BranchLookup | None = None
) -> CommandViolation | None:
	"""Return the first destructive-command violation, or ``None`` when allowed."""
	if not command.strip():
		return None
	if _DOWNLOAD_PIPE_RE.search(command):
		return CommandViolation(
			"download-piped-to-interpreter",
			"pipe downloaded content directly into an interpreter",
		)
	for segment in _shell_segments(command):
		if _PLAIN_TEXT_COMMAND_RE.search(segment) and "$(" not in segment and "`" not in segment:
			continue
		for invocation in _git_invocations(segment):
			if violation := _git_policy_violation(invocation, branch_lookup):
				return violation
		if _is_text_only_command(segment):
			continue
		for rule in _RULES:
			if rule.pattern.search(segment):
				return rule.violation
	return None
