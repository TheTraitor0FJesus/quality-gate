# Architecture map

Use this map before non-trivial codebase questions, design work, or code changes. Verify relevant claims in code.

## Navigation

- **CLI and orchestration** — `quality_gate/cli.py` parses commands; `quality_gate/contracts.py` validates schema 2 and defines verdicts; `quality_gate/migration.py` prints read-only schema 1 proposals; `quality_gate/snapshot.py` materializes the read-only Git index candidate; `quality_gate/runner.py` executes checks against that candidate.
- **Shared policy** — `quality_gate/policy/ruff.toml` and `quality_gate/policy/mypy.ini` define the common static-analysis rules.
- **Local runtime** — `.venv/` contains the untracked Python environment used by the global Git hook.
- **CI integration** — `.github/workflows/quality.yml` is the reusable workflow boundary; later release work must pin consumers to immutable policy artifacts.
- **Project contract** — `quality_gate/contracts.py` is the source of truth for schema 2 repository, component, verdict, and waiver models; `templates/quality-gate.toml` is the manifest shape generated for a project by `$setup-repo`.

## Flows

- **Local and CI checking** — `quality-gate check` → `quality_gate/snapshot.py` → `quality_gate/runner.py` → validated manifest → structured verdict → Ruff, mypy, pytest.
- **Project bootstrap** — `$setup-repo` → `quality-gate.toml` and a CI caller → reusable `quality.yml`.
- **Policy rollout** — validated `quality-gate` change → immutable release artifact → explicitly synchronized local and CI runtimes.
