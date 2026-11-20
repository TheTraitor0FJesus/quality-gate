"""Feature test partition."""

from __future__ import annotations

from distribution_runtime_feature_split72_support import (
	EXIT_UNCHECKED as EXIT_UNCHECKED,
)
from distribution_runtime_feature_split72_support import (
	DistributionError as DistributionError,
)
from distribution_runtime_feature_split72_support import (
	GateOperation as GateOperation,
)
from distribution_runtime_feature_split72_support import (
	Iterator as Iterator,
)
from distribution_runtime_feature_split72_support import (
	Path as Path,
)
from distribution_runtime_feature_split72_support import (
	PolicyCache as PolicyCache,
)
from distribution_runtime_feature_split72_support import (
	RuntimeManager as RuntimeManager,
)
from distribution_runtime_feature_split72_support import (
	RuntimeUnavailable as RuntimeUnavailable,
)
from distribution_runtime_feature_split72_support import (
	_release as _release,
)
from distribution_runtime_feature_split72_support import (
	contextmanager as contextmanager,
)
from distribution_runtime_feature_split72_support import (
	distribution as distribution,
)
from distribution_runtime_feature_split72_support import (
	hashlib as hashlib,
)
from distribution_runtime_feature_split72_support import (
	load_manifest as load_manifest,
)
from distribution_runtime_feature_split72_support import (
	main as main,
)
from distribution_runtime_feature_split72_support import (
	os as os,
)
from distribution_runtime_feature_split72_support import (
	prepare_bootstrap as prepare_bootstrap,
)
from distribution_runtime_feature_split72_support import (
	pytest as pytest,
)
from distribution_runtime_feature_split72_support import (
	runtime_fingerprint as runtime_fingerprint,
)
from distribution_runtime_feature_split72_support import (
	runtime_identity as runtime_identity,
)
from distribution_runtime_feature_split72_support import (
	runtime_root as runtime_root,
)
from distribution_runtime_feature_split72_support import (
	sys as sys,
)
from distribution_runtime_feature_split72_support import (
	tempfile as tempfile,
)
from distribution_runtime_feature_split72_support import (
	time as time,
)
from distribution_runtime_feature_split72_support import (
	zipfile as zipfile,
)


def test_failed_runtime_dependency_install_leaves_no_partial_runtime_and_can_retry(
	runtime_root: Path,
) -> None:
	manifest = load_manifest(Path(__file__).parent / "fixtures" / "valid")
	component = manifest.python[0]
	component = component.__class__(
		component.name,
		component.path,
		f"{sys.version_info.major}.{sys.version_info.minor}",
		(),
		(),
		False,
		"runtime retry test has no test suite",
		component.timeout_seconds,
	)
	manager = RuntimeManager(runtime_root / "cache")
	identity = runtime_identity(runtime_root, manifest, component)
	staging_parents: list[Path] = []

	def fail_install(python: Path, root: Path, inputs: tuple[str, ...]) -> None:
		assert python.is_file()
		staged = python.parent.parent
		assert (staged / "pyvenv.cfg").is_file()
		assert root == runtime_root
		assert inputs == ()
		staging_parents.append(staged.parent)
		raise RuntimeUnavailable("simulated dependency installation failure")

	with pytest.raises(RuntimeUnavailable, match="^simulated dependency installation failure$"):
		manager.ensure(
			runtime_root,
			manifest,
			component,
			python_executable=Path(sys.executable),
			install=fail_install,
		)

	failed = manager.inspect(runtime_root, identity)
	assert not failed.current
	assert failed.reason == "runtime is missing"
	assert not failed.path.exists()
	assert len(staging_parents) == 1
	assert staging_parents[0].parent == failed.path.parent
	assert not staging_parents[0].exists()
	successful_installs: list[Path] = []

	def successful_install(python: Path, root: Path, inputs: tuple[str, ...]) -> None:
		assert python.is_file()
		assert root == runtime_root
		assert inputs == ()
		successful_installs.append(python)

	assert runtime_identity(runtime_root, manifest, component) == identity
	prepared = manager.ensure(
		runtime_root,
		manifest,
		component,
		python_executable=Path(sys.executable),
		install=successful_install,
	)
	current = manager.inspect(runtime_root, identity)
	assert len(successful_installs) == 1
	assert prepared.current
	assert prepared.path == failed.path
	assert current.current
	assert current.python is not None
	assert current.python.is_file()


def test_sync_cli_requires_an_explicit_source_and_installs_release(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	monkeypatch.setattr(
		sys,
		"argv",
		[
			"quality-gate",
			"sync",
			"--source",
			str(source),
			"--cache-dir",
			str(tmp_path / "cache"),
		],
	)

	assert main() == 0
	assert "policy release synced: v2.0.0" in capsys.readouterr().out


def test_prepare_can_select_an_explicit_self_hosting_bootstrap_release(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	cache.sync(source)

	prepared = prepare_bootstrap(
		Path(__file__).parent / "fixtures" / "no-python",
		cache_dir=cache.root,
		policy_release="v2.0.0",
	)

	assert prepared.manifest.policy_release == "v2.0.0"
	assert prepared.release_manifest.version == "v2.0.0"

	with pytest.raises(DistributionError, match="vMAJOR.MINOR.PATCH"):
		prepare_bootstrap(
			Path(__file__).parent / "fixtures" / "no-python",
			cache_dir=cache.root,
			policy_release="not-a-release",
		)


def test_doctor_blocks_when_declared_policy_is_not_cached(
	tmp_path: Path,
	monkeypatch: pytest.MonkeyPatch,
	capsys: pytest.CaptureFixture[str],
) -> None:
	root = tmp_path / "repository"
	root.mkdir()
	manifest = Path(__file__).parent / "fixtures" / "no-python" / "quality-gate.toml"
	(root / "quality-gate.toml").write_bytes(manifest.read_bytes())
	monkeypatch.setattr(
		sys,
		"argv",
		[
			"quality-gate",
			"--root",
			str(root),
			"doctor",
			"--cache-dir",
			str(tmp_path / "cache"),
		],
	)

	assert main() == EXIT_UNCHECKED
	assert "doctor: unchecked" in capsys.readouterr().out
