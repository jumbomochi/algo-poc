# Dependency lockfiles

`pyproject.toml` is the source of truth for what this project depends on.
`requirements.lock` and `requirements-dev.lock` are **generated** from it and
are what actually gets installed — every service Dockerfile installs from
`requirements.lock`, and CI installs from `requirements-dev.lock`.

## Regenerating

Run both commands from the repo root, after any change to the dependency
lists in `pyproject.toml`:

```bash
uv pip compile pyproject.toml --universal --python-version 3.12 -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 -o requirements-dev.lock
```

Commit both files. `.github/workflows/security.yml`'s `lockfile-freshness`
job re-runs exactly these two commands and fails if the result differs from
what is committed.

**Use the commands verbatim.** Every part of them matters, and the output
records the command it was generated with — even changing `-o
requirements.lock` to an absolute path changes the file's header line and so
fails the CI check.

## Why each flag

The recipe these commands replaced (bare `uv pip compile pyproject.toml -o
requirements.lock`) left three independent sources of nondeterminism open.
Each was reproduced before this recipe was adopted; together they kept
`security.yml` red from 2026-08-09 to 2026-08-14 (KAN-36).

| Axis | Without the flag | With it |
|---|---|---|
| **Interpreter** | Resolution follows whatever `python3` happens to be. A dev on 3.13 and CI/containers on 3.12 differ by a couple of `typing-extensions` annotation lines. | `--python-version 3.12` matches CI and every `FROM python:3.12-slim` service image. |
| **Platform** | Markers are evaluated for the *host*. On darwin arm64 `platform_machine == "arm64"`, so SQLAlchemy's `greenlet` dependency evaluates false and vanishes from the lockfile — which is how `greenlet` came to be missing from all 9 container images. | `--universal` resolves platform-independently and emits every dependency with its marker, so pip decides at install time. |
| **Time** | This is the subtle one, and it lives in the CI job rather than the flags — see below. | Compile **in place**. |

`--universal` and `--python-platform` are mutually exclusive in uv.
`--universal` is the right choice here because it is platform-independent by
construction rather than by naming one target.

### The time axis: compile in place

`uv pip compile` treats pins already present in the **output file** as
preferences. So:

- Compiling **in place**, over the committed lockfile, keeps existing pins
  unless `pyproject.toml` no longer permits them. The check means "the
  lockfiles are consistent with `pyproject.toml`" — a stable property.
- Compiling **to a fresh path** (`-o /tmp/requirements.lock`, as the job used
  to) gives uv nothing to prefer, so it resolves newest-available every run.
  Any upstream release of any of ~60 transitive dependencies turns CI red,
  with no change to this repo. That is what happened.

This is the same semantics as `uv lock --check` or `poetry lock --check`.
Upgrades are then always deliberate — see below — rather than something that
happens to you on a Tuesday.

### Known sensitivity: the uv version

`setup-uv` installs the latest uv, and uv's output format is not frozen
across releases. In practice this is stable — the committed lockfiles were
verified byte-identical between uv 0.9.18 on darwin arm64 and uv 0.12.4 in
`python:3.12-slim`, three minor versions apart — but a future release that
changes the generated file would turn the freshness job red until the
lockfiles are regenerated. If that happens, regenerate with the commands
above and commit; the failure is cosmetic, not a dependency change. Pin
`version:` on the `setup-uv` step if it ever becomes more than a rare
annoyance.

## Upgrading a dependency on purpose

To take a newer version of one package within its existing range:

```bash
uv pip compile pyproject.toml --universal --python-version 3.12 \
  --upgrade-package alembic -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.12 \
  --upgrade-package alembic -o requirements-dev.lock
```

`--upgrade` (no package name) refreshes everything; prefer the targeted form
so a routine bump does not arrive bundled with 40 unrelated ones.

To move a package past its upper bound, edit the range in `pyproject.toml`
first, then regenerate.

## Version ceiling policy

The upper bounds in `pyproject.toml` (`numpy>=1.26,<2.0`, `pandas>=2.0,<3.0`,
and so on) are deliberate. This system trades real money, and major versions
of the numeric stack change behaviour rather than just API surface — numpy
2.x, for instance, changed dtype promotion rules. Crossing a major boundary
is an evaluation with its own story, not dependency hygiene.

Accordingly `.github/dependabot.yml` carries, on the **pip** ecosystem entry
only:

```yaml
ignore:
  - dependency-name: "*"
    update-types: ["version-update:semver-major"]
```

This is the form currently in effect. It suppresses Dependabot's
range-widening PRs (`numpy>=1.26,<2.0` → `>=1.26,<3.0` and friends), which
are classified by the new version's bump type. If any of those reappear after
a scheduled Monday run, replace the wildcard with per-dependency entries
naming `numpy`, `pandas`, `redis`, `plotly`, `praw`, and `structlog` with no
`update-types` key, and update this paragraph to say so. `pip-audit` gates
CVEs on both lockfiles either way, so nothing security-relevant hides behind
this rule.

The docker ecosystem entries are deliberately left un-ignored, but note that
base-image bumps past `python:3.12-slim` are blocked in practice: numpy
1.26.4 publishes wheels only for cp39–cp312, so a 3.13/3.14 base image fails
every service build. That unblocks itself only behind a numpy major upgrade.

## What CI checks

`.github/workflows/security.yml`:

- **`pip-audit`** — CVE scan against both lockfiles, on push/PR and weekly.
- **`lockfile-freshness`** — recompiles both files in place and runs
  `git diff --exit-code` on them, printing the actual delta when they drift.

`.github/workflows/tests.yml` runs the full pytest suite on Python 3.12 with
no service containers, installing from `requirements-dev.lock` — so the
lockfiles are exercised by the same suite that gates every PR.
