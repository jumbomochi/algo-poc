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

Worth being precise about what that buys and what it costs. The job **fails**
on:

- a pin that `pyproject.toml` no longer permits (hand-editing `redis==5.3.1`
  to `6.4.0` against `redis>=5.0,<6.0` is corrected back, and the diff shows
  it);
- a dependency missing from a lockfile, or one present that nothing requires
  — including the case that matters most, adding a package to
  `pyproject.toml` and forgetting to regenerate.

It deliberately does **not** fail merely because a newer version exists
upstream. A hand-edit from `coverage==7.15.4` to `7.15.0` is left alone,
because both satisfy the manifest and uv prefers what it finds. That is the
whole point — "is this lockfile a valid solution of this manifest" is a
property of this repo, whereas "is this lockfile the newest solution" is a
property of PyPI on the day you asked, and only the first can be a gate.

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

### How the ceiling is enforced

`.github/dependabot.yml` carries, on the **pip** ecosystem entry only, one
`ignore` entry per upper-bounded dependency, each naming the ceiling that
`pyproject.toml` sets:

```yaml
versioning-strategy: increase-if-necessary
ignore:
  - dependency-name: numpy
    versions: [">=2.0"]
  # ... one per dependency carrying an upper bound
```

Two pieces, doing different jobs:

- **`versioning-strategy: increase-if-necessary`** decides *when* Dependabot
  rewrites the manifest at all. It leaves a requirement alone whenever the
  range already admits the new version, so in-range minors and patches
  produce no PR. Only a ceiling crossing rewrites anything. The value is set
  explicitly because the default, `auto`, resolves to `widen` for a
  `pyproject.toml` with ranges — which is what generated the PRs in the table
  below.
- **The per-dependency `versions` conditions** then decline that crossing.

Each bound mirrors `pyproject.toml`. **Raising a ceiling there means raising
it here too.** Forgetting fails closed — the old bound stays enforced and no
PR appears — which is the safe direction, but it is silent, so the two lists
are checked against each other whenever either moves.

`pip-audit` gates CVEs on both lockfiles independently of all this, so
nothing security-relevant hides behind these rules.

#### Why not the wildcard `update-types` rule

This replaced the form KAN-36 introduced:

```yaml
ignore:
  - dependency-name: "*"
    update-types: ["version-update:semver-major"]   # never fired
```

That rule reads correctly and does nothing. Dependabot has no resolved
"current version" for a *range* requirement in a `pyproject.toml` whose
lockfile it does not manage — `requirements.lock` is not a manifest format
Dependabot parses. With no current version it cannot emit a *version* update,
so it emits a **requirement** update instead, and an `update-types` condition
has no from-version/to-version pair to run a semver comparison against. The
PR titles are the visible tell:

| Form | Title | Classified? |
|---|---|---|
| Requirement update (pip, ranges) | `update praw requirement from <8.0,>=7.7 to >=7.7,<9.0` | no — `update-types` cannot match |
| Version update (docker, actions) | `bump actions/checkout from 4 to 7` | yes |

Every pip PR this repo has ever received is the first form; every docker and
github-actions PR is the second. A `versions` condition filters candidate
versions directly and needs no comparison, which is why it works where
`update-types` could not.

### Declined majors — the precedent

Each of these was opened by Dependabot and closed unmerged. They are recorded
so a reopened PR is answered by a citation rather than a fresh evaluation.
None of them was evaluated on its merits; each was declined under the ceiling
policy above, which makes the default "not automatically" — it does not make
it "never".

| PR | Dependency | Requested widen | Closed |
|---|---|---|---|
| #30 | redis | `>=5.0,<6.0` → `>=5.0,<9.0` | 2026-08-14 |
| #33 | pandas | `>=2.0,<3.0` → `>=2.0,<4.0` | 2026-08-14 |
| #35 | plotly | `>=5.18,<6.0` → `>=5.18,<7.0` | 2026-08-14 |
| #36 | numpy | `>=1.26,<2.0` → `>=1.26,<3.0` | 2026-08-14 |
| #37 | praw | `>=7.7,<8.0` → `>=7.7,<9.0` | 2026-08-14 |
| #38 | structlog | `>=24.0,<25.0` → `>=24.0,<27.0` | 2026-08-14 |
| #43 | numpy | `>=1.26,<2.0` → `>=1.26,<3.0` | 2026-08-14 (repeat of #36) |
| #82 | praw | `>=7.7,<8.0` → `>=7.7,<9.0` | 2026-08-18 (repeat of #37) |

Closing one does not settle it: #43 reappeared about a minute after #42
landed, and #82 is #37 filed again four days later. That is what the ignore
list is for.

Note also that numpy's ceiling is load-bearing for more than numpy. numpy
1.26.4 publishes wheels only for cp39–cp312, so the `python:3.13`/`3.14`
base-image bumps (#21–#32, also closed) fail every service build until numpy
crosses its major first.

#### Green CI on a widening PR is not evidence

This is the non-obvious part, and it is why these PRs cannot be triaged by
looking at the checks. A widen touches `pyproject.toml` only. The lockfile is
not regenerated, `requirements.lock` keeps the old pin, and every service
Dockerfile installs `--no-deps -r requirements.lock` — so the suite runs
against the **version already in use**, not the newly-permitted major.

#82 passed all five checks while exercising praw 7.8.2 — the version it was
not asking about. Its green tells you the range edit did not break the status
quo. It tells you nothing whatsoever about praw 8.x.

Evaluating a major therefore means editing the range, regenerating both
lockfiles (see *Upgrading a dependency on purpose* above), and reading the
suite against the new pin. That is the "explicit evaluation" the ceiling
policy asks for, and it is a story of its own.

### Where dependency PRs are based

Every entry in `.github/dependabot.yml` sets `target-branch: develop`, so no
dependency PR is based on `main`. Before this, none did: every one of the
twenty Dependabot PRs this repo has received targeted `main`, which is
production and carries no branch protection at all (CLAUDE.md, "Branch
Flow"). It never bit only because nineteen of the twenty were closed
unmerged — the twentieth, #31, merged straight to production.

Two consequences worth knowing:

- **Dependabot reads `.github/dependabot.yml` from the default branch**
  (`main`) regardless of `target-branch`. A change to that file therefore has
  no effect until it is promoted develop → main. The manifests it diffs, and
  the PRs it opens, do come from `develop`.
- **Security updates only ever open against the default branch**, so with
  `target-branch` set these entries handle version updates only. `pip-audit`
  is the CVE tripwire here, on both lockfiles, on every push/PR and weekly.

The docker and github-actions entries carry no `ignore` list at all — only
the pip entry does. Their update policy is a separate question from the
version ceilings, and unresolved; `target-branch` is the only thing this
change altered about them.

## What CI checks

`.github/workflows/security.yml`:

- **`pip-audit`** — CVE scan against both lockfiles, on push/PR and weekly.
- **`lockfile-freshness`** — recompiles both files in place and runs
  `git diff --exit-code` on them, printing the actual delta when they drift.

`.github/workflows/tests.yml` runs the full pytest suite on Python 3.12 with
no service containers, installing from `requirements-dev.lock` — so the
lockfiles are exercised by the same suite that gates every PR.
