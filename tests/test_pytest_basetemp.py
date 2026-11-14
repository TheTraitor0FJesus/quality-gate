"""The Windows pytest base stays short, isolated, and repository-local."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import conftest
import pytest


def _config(basetemp: Path | None = None) -> tuple[SimpleNamespace, list]:
	cleanups: list = []
	config = SimpleNamespace(
		option=SimpleNamespace(basetemp=basetemp), add_cleanup=cleanups.append
	)
	return config, cleanups


def test_windows_automatic_bases_are_unique_and_cleaned(tmp_path: Path) -> None:
	(tmp_path / ".git").mkdir()
	nested = tmp_path / "temp" / "snapshot" / "tests"
	nested.mkdir(parents=True)
	first, first_cleanups = _config()
	second, second_cleanups = _config()
	conftest._configure_short_basetemp(first, nested, windows=True)
	conftest._configure_short_basetemp(second, nested, windows=True)
	first_path = Path(first.option.basetemp)
	second_path = Path(second.option.basetemp)
	assert first_path != second_path
	assert first_path.is_relative_to(tmp_path / "temp")
	assert second_path.is_relative_to(tmp_path / "temp")
	first_path.mkdir()
	(first_path / "data").write_text("test", encoding="utf-8")
	first_cleanups[0]()
	assert not first_path.parent.exists()
	assert second_path.parent.exists()
	second_cleanups[0]()
	assert not second_path.parent.exists()


def test_explicit_basetemp_and_non_windows_are_untouched(tmp_path: Path) -> None:
	explicit = tmp_path / "provided"
	config, cleanups = _config(explicit)
	conftest._configure_short_basetemp(config, tmp_path, windows=True)
	assert config.option.basetemp == explicit
	assert not cleanups
	config, cleanups = _config()
	conftest._configure_short_basetemp(config, tmp_path, windows=False)
	assert config.option.basetemp is None
	assert not cleanups


@pytest.fixture
def short_repository() -> Iterator[Path]:
	root = Path(__file__).resolve().parents[1] / "temp"
	root.mkdir(exist_ok=True)
	with tempfile.TemporaryDirectory(prefix="p72-", dir=root) as directory:
		yield Path(directory)


def test_pytest_uses_and_cleans_automatic_base_on_windows(short_repository: Path) -> None:
	if os.name != "nt":
		return
	repository = short_repository
	tests = repository / "tests"
	tests.mkdir(parents=True)
	(repository / ".git").mkdir()
	shutil.copyfile(Path(conftest.__file__), tests / "conftest.py")
	(tests / "test_probe.py").write_text(
		"from pathlib import Path\n"
		"import tempfile\n"
		"from quality_gate.release import _checked_windows_workspace_path\n"
		"from quality_gate.temp_workspace import temporary_workspace\n"
		"def test_probe(tmp_path, pytestconfig):\n"
		"    with temporary_workspace(tmp_path):\n"
		"        final = _checked_windows_workspace_path(Path(tempfile.gettempdir()))\n"
		"        Path.cwd().joinpath('observed.txt').write_text(\n"
		"            str(pytestconfig._tmp_path_factory.getbasetemp()) + '\\n' + str(final))\n",
		encoding="utf-8",
	)
	environment = os.environ.copy()
	environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
	result = subprocess.run(
		[sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests"],
		cwd=repository,
		env=environment,
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	base, final = (repository / "observed.txt").read_text(encoding="utf-8").splitlines()
	(repository / "observed-auto.txt").write_text(f"{base}\n{final}\n", encoding="utf-8")
	assert Path(base).is_relative_to(repository / "temp")
	assert len(final) <= 180
	assert not Path(base).parent.exists()
	explicit = repository / "temp" / "chosen"
	result = subprocess.run(
		[
			sys.executable,
			"-m",
			"pytest",
			"-p",
			"no:cacheprovider",
			"-q",
			f"--basetemp={explicit}",
			"tests",
		],
		cwd=repository,
		env=environment,
		capture_output=True,
		text=True,
		check=False,
	)
	assert result.returncode == 0, result.stdout + result.stderr
	base, _final = (repository / "observed.txt").read_text(encoding="utf-8").splitlines()
	assert Path(base) == explicit
	assert explicit.exists()
