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


def test_sync_installs_verified_release_and_keeps_previous_for_rollback(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")

	cache.sync(source)
	second = tmp_path / "second"
	second.mkdir()
	_release(second, "v2.0.1")
	cache.sync(second)

	assert cache.select("v2.0.1").is_dir()
	assert cache.status()["active"] == "v2.0.1"
	assert cache.rollback().name == "v2.0.0"


@pytest.mark.parametrize("phase", ["inventory", "hash"])
def test_review_policy_validation_is_bounded(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	cache.sync(source)
	selected = cache.select("v2.0.0")
	(directory := selected / "empty").mkdir()
	real_glob = Path.rglob
	real_open = Path.open
	reads = 0

	class SlowReader:
		def __init__(self, stream: object) -> None:
			self.stream = stream

		def read(self, size: int) -> bytes:
			nonlocal reads
			reads += 1
			time.sleep(0.1)
			return self.stream.read(min(size, 1))

	@contextmanager
	def slow_open(path: Path, *args: object, **kwargs: object) -> Iterator[object]:
		with real_open(path, *args, **kwargs) as stream:
			if path.parent == selected and path.suffix == ".whl" and phase == "hash":
				yield SlowReader(stream)
			else:
				yield stream

	def slow_glob(path: Path, pattern: str) -> Iterator[Path]:
		if path == selected and phase == "inventory":
			for _ in range(20):
				time.sleep(0.05)
				yield directory
		yield from real_glob(path, pattern)

	monkeypatch.setattr(Path, "open", slow_open)
	monkeypatch.setattr(Path, "rglob", slow_glob)
	gate = GateOperation(tmp_path, "check", timeout_seconds=0.1, cleanup_seconds=1)
	completed = False
	started = time.monotonic()
	with gate:
		with gate.stage("policy.cache-lookup-validation", "v2.0.0"):
			cache.select("v2.0.0")
			completed = True
	assert gate.timed_out
	assert not completed
	assert time.monotonic() - started < 0.5
	assert selected.is_dir(), "an interrupted validation must not quarantine a valid release"
	if phase == "hash":
		assert reads < 6


def test_prepare_preserves_lexical_policy_cache_root_for_native_cache(tmp_path: Path) -> None:
	repository_root = Path(__file__).parents[1].resolve()
	cache_target = tmp_path / "policy-cache-target"
	cache_target.mkdir()
	release_source = tmp_path / "release-source"
	release_source.mkdir()
	_release(release_source)
	PolicyCache(cache_target).sync(release_source)
	cache_link = tmp_path / "policy-cache-link"
	try:
		cache_link.symlink_to(cache_target, target_is_directory=True)
	except OSError:
		pytest.skip("directory symlinks are unavailable for this temporary directory")

	prepared = prepare_bootstrap(
		repository_root,
		policy_release="v2.0.0",
		cache_dir=cache_link,
		repository_root=repository_root,
	)

	assert prepared.cache_root == cache_link
	assert prepared.cache_root.resolve() == cache_target.resolve()


def test_sync_installs_a_verified_zip_release(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	archive = tmp_path / "release.zip"
	with zipfile.ZipFile(archive, "w") as zipped:
		for path in source.iterdir():
			zipped.write(path, path.name)

	cache = PolicyCache(tmp_path / "cache")

	assert cache.sync(archive).version == "v2.0.0"
	assert cache.select("v2.0.0").is_dir()


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions are not portable")
def test_sync_restores_executable_permissions_for_zip_tools(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	archive = tmp_path / "release.zip"
	with zipfile.ZipFile(archive, "w") as zipped:
		for path in source.iterdir():
			zipped.write(path, path.name)

	cache = PolicyCache(tmp_path / "cache")
	cache.sync(archive)

	assert os.access(cache.select("v2.0.0") / "ruff.exe", os.X_OK)


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions are not portable")
def test_repeat_sync_repairs_existing_zip_tool_permissions(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	archive = tmp_path / "release.zip"
	with zipfile.ZipFile(archive, "w") as zipped:
		for path in source.iterdir():
			zipped.write(path, path.name)

	cache = PolicyCache(tmp_path / "cache")
	cache.sync(archive)
	installed = cache.select("v2.0.0") / "ruff.exe"
	installed.chmod(installed.stat().st_mode & ~0o111)

	cache.sync(archive)

	assert os.access(installed, os.X_OK)


def test_consumer_policy_sync_is_independent_of_lessons(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	(tmp_path / "lessons").mkdir()
	(tmp_path / "lessons" / "incident.md").write_text("not learned\n", encoding="utf-8")

	cache = PolicyCache(tmp_path / "cache")

	assert cache.sync(source).version == "v2.0.0"
	assert cache.status()["active"] == "v2.0.0"


def test_release_manifest_selects_the_policy_wheel_after_dependency_wheels(
	tmp_path: Path,
) -> None:
	source = tmp_path / "source"
	source.mkdir()
	dependency = source / "dependency-1.0-py3-none-any.whl"
	dependency.write_bytes(b"dependency")
	_release(source)
	manifest_path = source / "release.toml"
	manifest_path.write_text(
		manifest_path.read_text(encoding="utf-8").replace(
			'[[release.files]]\npath = "quality_gate-2.0.0-py3-none-any.whl"',
			f'''[[release.files]]
path = "{dependency.name}"
sha256 = "{hashlib.sha256(dependency.read_bytes()).hexdigest()}"
kind = "dependency"

[[release.files]]
path = "quality_gate-2.0.0-py3-none-any.whl"''',
		),
		encoding="utf-8",
	)

	manifest = distribution.load_release_manifest(source)

	assert manifest.wheel is not None
	assert manifest.wheel.path == "quality_gate-2.0.0-py3-none-any.whl"


def test_sync_rejects_corrupt_artifact_without_installing_it(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	(source / "policy.txt").write_text("tampered\n", encoding="utf-8")
	cache = PolicyCache(tmp_path / "cache")

	with pytest.raises(DistributionError, match="checksum mismatch"):
		cache.sync(source)

	assert not (tmp_path / "cache" / "releases" / "v2.0.0").exists()


def test_corrupt_cached_release_is_quarantined(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	cache.sync(source)
	(cache.releases / "v2.0.0" / "policy.txt").write_text("tampered\n", encoding="utf-8")

	with pytest.raises(DistributionError, match="unavailable"):
		cache.select("v2.0.0")

	assert list(cache.quarantine.glob("v2.0.0-*"))


def test_sync_lock_blocks_a_second_mutation(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	cache.root.mkdir()
	cache.lock_path.write_text("other process\n", encoding="ascii")

	with pytest.raises(DistributionError, match="locked"):
		cache.sync(source, lock_timeout_seconds=0)


def test_interrupted_sync_leaves_no_partial_release_and_can_recover(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	real_replace = distribution.os.replace

	def interrupt_staged_release(source_path: str, target_path: str) -> None:
		if Path(target_path).parent == cache.releases:
			raise OSError("simulated replacement interruption")
		real_replace(source_path, target_path)

	monkeypatch.setattr(distribution.os, "replace", interrupt_staged_release)
	try:
		with pytest.raises(OSError, match="interruption"):
			cache.sync(source)
	finally:
		monkeypatch.undo()

	assert not (cache.releases / "v2.0.0").exists()
	assert cache.sync(source).version == "v2.0.0"
	assert cache.select("v2.0.0").is_dir()


def test_prune_requires_confirmation_and_keeps_active_releases(tmp_path: Path) -> None:
	source = tmp_path / "source"
	source.mkdir()
	_release(source)
	cache = PolicyCache(tmp_path / "cache")
	cache.sync(source)
	old = cache.releases / "v9.9.9"
	old.mkdir(parents=True)
	old_timestamp = time.time() - 31 * 24 * 60 * 60
	os.utime(old, (old_timestamp, old_timestamp))

	assert cache.prune(older_than_days=30) == ("v9.9.9",)
	assert old.exists()
	assert cache.prune(confirm=True, older_than_days=30) == ("v9.9.9",)
	assert not old.exists()
	assert cache.select("v2.0.0").is_dir()


def test_runtime_fingerprint_changes_when_dependency_input_changes(tmp_path: Path) -> None:
	manifest_root = Path(__file__).parent / "fixtures" / "valid"
	manifest = load_manifest(manifest_root)
	component = manifest.python[0]
	dependency = tmp_path / "pyproject.toml"
	dependency.write_text("[project]\nname='fixture'\n", encoding="utf-8")
	component = component.__class__(
		component.name,
		component.path,
		component.python_version,
		("pyproject.toml",),
		component.test_paths,
		component.tests_applicable,
		component.tests_reason,
		component.timeout_seconds,
	)
	identity = runtime_identity(
		tmp_path,
		manifest,
		component,
	)
	first = runtime_fingerprint(identity)
	first_digest = identity.dependency_inputs[0]["sha256"]
	dependency.write_text("[project]\nname='changed'\n", encoding="utf-8")
	changed = runtime_fingerprint(runtime_identity(tmp_path, manifest, component))
	assert first != changed
	assert first_digest != hashlib.sha256(dependency.read_bytes()).hexdigest()


def test_runtime_fingerprint_ignores_dependency_line_ending_conversion(tmp_path: Path) -> None:
	manifest_root = Path(__file__).parent / "fixtures" / "valid"
	manifest = load_manifest(manifest_root)
	component = manifest.python[0]
	dependency = tmp_path / "requirements.txt"
	component = component.__class__(
		component.name,
		component.path,
		component.python_version,
		("requirements.txt",),
		component.test_paths,
		component.tests_applicable,
		component.tests_reason,
		component.timeout_seconds,
	)
	dependency.write_bytes(b"package==1.0\r\nother==2.0\r\n")
	windows_fingerprint = runtime_fingerprint(runtime_identity(tmp_path, manifest, component))

	dependency.write_bytes(b"package==1.0\nother==2.0\n")
	git_fingerprint = runtime_fingerprint(runtime_identity(tmp_path, manifest, component))

	assert windows_fingerprint == git_fingerprint


def test_runtime_identity_rejects_dependency_symlink_outside_repository(tmp_path: Path) -> None:
	root = tmp_path / "repository"
	root.mkdir()
	manifest = load_manifest(Path(__file__).parent / "fixtures" / "valid")
	component = manifest.python[0]
	component = component.__class__(
		component.name,
		component.path,
		component.python_version,
		("requirements.txt",),
		component.test_paths,
		component.tests_applicable,
		component.tests_reason,
		component.timeout_seconds,
	)
	content = b"package==1.0\n"
	dependency = root / "requirements.txt"
	dependency.write_bytes(content)
	identity = runtime_identity(root, manifest, component)
	assert identity.dependency_inputs == (
		{"path": "requirements.txt", "sha256": hashlib.sha256(content).hexdigest()},
	)

	external = tmp_path / "external-requirements.txt"
	external.write_bytes(content)
	dependency.unlink()
	try:
		dependency.symlink_to(external)
	except OSError:
		pytest.skip("symbolic links are unavailable on this platform")
	assert dependency.is_symlink()
	assert dependency.resolve() == external.resolve()

	with pytest.raises(
		RuntimeUnavailable,
		match="^dependency input escapes the repository: requirements\\.txt$",
	):
		runtime_identity(root, manifest, component)


def test_missing_dependency_input_is_unchecked(tmp_path: Path) -> None:
	manifest = load_manifest(Path(__file__).parent / "fixtures" / "valid")
	component = manifest.python[0].__class__(
		manifest.python[0].name,
		manifest.python[0].path,
		manifest.python[0].python_version,
		("missing-lock.txt",),
		(),
		False,
		"missing input test",
		manifest.python[0].timeout_seconds,
	)

	with pytest.raises(RuntimeUnavailable, match="dependency input is unavailable"):
		runtime_identity(tmp_path, manifest, component)


def test_missing_python_executable_is_unchecked(runtime_root: Path) -> None:
	manifest = load_manifest(Path(__file__).parent / "fixtures" / "valid")
	component = manifest.python[0].__class__(
		manifest.python[0].name,
		manifest.python[0].path,
		manifest.python[0].python_version,
		(),
		(),
		False,
		"missing Python test",
		manifest.python[0].timeout_seconds,
	)
	manager = RuntimeManager(runtime_root / "cache")

	with pytest.raises(RuntimeUnavailable, match="Python executable is unavailable"):
		manager.ensure(
			runtime_root,
			manifest,
			component,
			python_executable=runtime_root / "missing-python",
		)


def test_runtime_inspection_is_unchecked_until_metadata_and_python_exist(tmp_path: Path) -> None:
	manifest_root = Path(__file__).parent / "fixtures" / "valid"
	manifest = load_manifest(manifest_root)
	component = manifest.python[0]
	component = component.__class__(
		component.name,
		component.path,
		f"{sys.version_info.major}.{sys.version_info.minor}",
		(),
		component.test_paths,
		component.tests_applicable,
		component.tests_reason,
		component.timeout_seconds,
	)
	manager = RuntimeManager(tmp_path / "cache")

	inspection = manager.inspect(tmp_path, runtime_identity(tmp_path, manifest, component))

	assert not inspection.current
	assert inspection.reason == "runtime is missing"


def test_setup_creates_an_isolated_runtime_and_identity_change_makes_it_stale(
	runtime_root: Path,
) -> None:
	manifest_root = Path(__file__).parent / "fixtures" / "valid"
	manifest = load_manifest(manifest_root)
	component = manifest.python[0]
	component = component.__class__(
		component.name,
		component.path,
		f"{sys.version_info.major}.{sys.version_info.minor}",
		(),
		(),
		False,
		"runtime setup test has no test suite",
		component.timeout_seconds,
	)
	manager = RuntimeManager(runtime_root / "cache")

	prepared = manager.ensure(
		runtime_root,
		manifest,
		component,
		python_executable=Path(sys.executable),
	)
	changed = component.__class__(
		component.name,
		component.path,
		component.python_version,
		(),
		(),
		False,
		component.tests_reason,
		component.timeout_seconds + 1,
	)
	stale = manager.inspect(runtime_root, runtime_identity(runtime_root, manifest, changed))

	assert prepared.current
	assert prepared.python is not None
	assert not stale.current
	assert stale.reason == "runtime is missing"
