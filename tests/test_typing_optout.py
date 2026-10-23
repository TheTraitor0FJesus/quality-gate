"""The public schema and full Gate preserve explicit typing opt-outs."""

import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from quality_gate import runner
from quality_gate.contracts import ValidationError, parse_manifest
from quality_gate.runner import load_components
from quality_gate.runtime import runtime_identity


def manifest_text(version: str = "v3.1.0", flag: str = "false") -> str:
	return f'''[quality]
schema = 2
policy_release = "{version}"
[repository]
name = "typing-optout"
domains = ["repository", "python"]
required_documents = ["quality-gate.toml"]
[[python]]
name = "app"
path = "app"
python_version = "3.12"
dependency_inputs = []
test_paths = []
tests_applicable = false
tests_reason = "No tests for this disposable contract fixture."
typecheck = {flag}
'''


def test_new_policy_optout_reaches_full_gate_and_runtime_identity(tmp_path: Path) -> None:
	(tmp_path / "app").mkdir()
	(tmp_path / "quality-gate.toml").write_text(manifest_text(), encoding="utf-8")
	manifest = parse_manifest(manifest_text())
	assert manifest.python[0].typecheck is False
	assert load_components(tmp_path)[0].typecheck is False
	identity = runtime_identity(tmp_path, manifest, manifest.python[0])
	assert identity.component_contract["typecheck"] is False


@pytest.mark.parametrize("flag", ["true", "false"])
def test_old_policy_rejects_new_field_instead_of_ignoring_optout(flag: str) -> None:
	with pytest.raises(ValidationError, match="typecheck"):
		parse_manifest(manifest_text("v2.4.2", flag))


def test_old_manifest_default_preserves_runtime_fingerprint_contract(tmp_path: Path) -> None:
	manifest = parse_manifest(manifest_text("v2.4.2").replace("typecheck = false\n", ""))
	assert manifest.python[0].typecheck is True
	identity = runtime_identity(tmp_path, manifest, manifest.python[0])
	assert "typecheck" not in identity.component_contract


def test_full_gate_reports_optout_and_keeps_other_python_checks(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	(tmp_path / "app").mkdir()
	(tmp_path / "app/source.py").write_text('value: int = "typing failure"\n', encoding="utf-8")
	(tmp_path / "quality-gate.toml").write_text(manifest_text(), encoding="utf-8")
	monkeypatch.setattr(
		runner, "candidate_snapshot", lambda root: nullcontext(SimpleNamespace(root=root))
	)
	monkeypatch.setattr(
		runner,
		"prepare",
		lambda *args, **kwargs: SimpleNamespace(
			policy_root=runner.POLICY_DIR.parent.parent,
			runtimes=(SimpleNamespace(python=Path(sys.executable), current=True),),
		),
	)
	commands: list[list[str]] = []

	def execute(
		command: list[str], root: Path, environment: dict[str, str], *, timeout: float
	) -> None:
		commands.append(command)

	monkeypatch.setattr(runner, "run", execute)
	verdict = runner.check(tmp_path)
	mypy = [result for result in verdict.results if result.check_id == "python.component_1.mypy"]
	assert len(mypy) == 1 and mypy[0].status is runner.Status.NOT_APPLICABLE
	assert any("ruff" in command for command in commands)
	assert all("mypy" not in command for command in commands)
