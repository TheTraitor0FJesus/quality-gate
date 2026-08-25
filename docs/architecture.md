# Architecture map

Use this map before non-trivial codebase questions, design work, or code changes. Verify relevant claims in code.

## Navigation

- **CLI and orchestration** — `quality_gate/cli.py` parses commands; `quality_gate/contracts.py` validates schema 2 and defines verdicts; `quality_gate/migration.py` prints read-only schema 1 proposals; `quality_gate/snapshot.py` materializes the read-only Git index candidate; `quality_gate/runner.py` executes checks against that candidate; `quality_gate/launcher.py` selects a pinned release and prepares consumer runtimes.
- **Policy distribution** — `quality_gate/distribution.py` validates release manifests and SHA-256 digests, installs releases through a locked same-volume replacement, quarantines corrupt entries, and retains active/previous selections.
- **Local runtime** — `.venv/` contains the untracked Python environment used by the global Git hook; `quality_gate/runtime.py` creates a separate cache runtime per repository and fingerprint of policy, component, Python, and dependency inputs.
- **CI integration** — `.github/workflows/quality.yml` is the reusable workflow boundary; later release work must pin consumers to immutable policy artifacts.
- **Project contract** — `quality_gate/contracts.py` is the source of truth for schema 2 repository, component, verdict, and waiver models; `templates/quality-gate.toml` is the manifest shape generated for a project by `$setup-repo`.

## Flows

- **Local and CI checking** — `quality-gate check` → `quality_gate/snapshot.py` → `quality_gate/runner.py` → validated manifest → structured verdict → Ruff, mypy, pytest.
- **Release and runtime preparation** — `quality-gate sync` → verified immutable release cache → `quality-gate setup` → repository-keyed runtime fingerprint → isolated Python environment; `quality-gate doctor` reports missing prerequisites as unchecked.
- **Project bootstrap** — `$setup-repo` → `quality-gate.toml` and a CI caller → reusable `quality.yml`.
- **Policy rollout** — validated `quality-gate` change → immutable release artifact → explicitly synchronized local and CI runtimes.
