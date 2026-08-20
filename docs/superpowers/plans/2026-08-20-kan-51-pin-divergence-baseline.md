# KAN-51 — Pin the divergence monitor's baseline explicitly

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** make the baseline the nightly divergence monitor judges against a
configuration fact rather than a filename-recency accident, and make every way
that pin can be wrong loud instead of silent.

**Architecture:** the pin is a path in `config/default.yaml`
(`divergence.baseline_pin`). A tiny resolver (`scripts/ops/baseline_pin.py`)
turns it into an absolute path for shell callers. `run_divergence.sh` passes it
as `--backtest <pin> --pinned`; `--pinned` is the flag that swaps *tolerant*
ad-hoc semantics for *pinned* semantics inside `scripts/divergence_monitor.py`
— no recency fallback, a missing pin and a sleeve-shape mismatch both become
exit 3 with a named reason. `run_backtest_refresh.sh` excludes the pin from its
90-day prune, and skips the prune entirely rather than risk deleting it.

**Tech Stack:** Python 3.12, pydantic (`shared/config.py`), argparse, bash
(launchd wrappers), pytest (`asyncio_mode="auto"`).

**Spec:** the JIRA issue <https://huiliang.atlassian.net/browse/KAN-51> is the
spec. Context: `docs/operations/backtest-baseline.md`.

## Global Constraints

- Exit-code contract stays `0/1/2/3/4` with the same meanings; no *existing*
  path changes which code it returns. New failures reuse `3`
  (`EXIT_BASELINE_NOT_COMPARABLE`) because they are the same outage: the
  monitor cannot judge.
- A missing pin must NEVER fall back to recency. Silent fallback is the
  2026-08-13 failure pattern.
- Recency selection (`find_latest_backtest_json`) stays as the documented
  default for ad-hoc local runs (i.e. runs without `--pinned`).
- No schema change. `divergence_daily.baseline_id` is already written from the
  artifact basename; the pin only changes *which* artifact that is.
- Out of scope: the Rung-0 baseline artifact (P2-24, blocked on KAN-59), the
  coverage floor, `is_like_for_like`'s existing requirements, and the KAN-56
  staleness threshold.
- Every module starts with `from __future__ import annotations`.
- Verified precondition (2026-08-20, `portfolio_config` on the paper DB): the
  live book is exactly the six sleeves in the current de-facto baseline
  `output/backtest_multi_20260819_183451.json`, whose `coverage.state` is
  already `BLOCKED`. So shipping that path as the default pin is a behavioural
  no-op — the monitor exits 3 tonight either way.

---

## File structure

| File | Responsibility |
|---|---|
| `shared/config.py` | new `DivergenceConfig.baseline_pin: str \| None`, wired onto `AppConfig` |
| `config/default.yaml` | the pin of record, with the re-pin pointer |
| `scripts/ops/baseline_pin.py` (new) | resolve the pin for shell callers; `ALGO_BASELINE_PIN` env override wins |
| `scripts/divergence_monitor.py` | `--pinned` semantics: no fallback, `BASELINE_PIN_MISSING`, `BASELINE_SHAPE_MISMATCH`, `load_backtest_sleeves` |
| `scripts/ops/divergence_alert.py` | exit-3 alert names the pin failure instead of saying "not comparable" |
| `deploy/launchd/run_divergence.sh` | resolve + pass `--backtest <pin> --pinned` |
| `deploy/launchd/run_backtest_refresh.sh` | prune cannot delete the pin |
| `docs/operations/backtest-baseline.md` | what the pin is, where it lives, how to re-pin |

---

### Task 1: the config field and the resolver

**Files:**
- Modify: `shared/config.py`
- Modify: `config/default.yaml`
- Create: `scripts/ops/baseline_pin.py`
- Test: `tests/ops/test_baseline_pin.py`

**Interfaces:**
- Produces: `DivergenceConfig(baseline_pin: str | None)`, `AppConfig.divergence`;
  `scripts.ops.baseline_pin.resolve_pin(config_path: str = "config/default.yaml") -> str | None`
  returning an **absolute** path (relative pins resolve against the cwd), and a
  `main(argv) -> int` that prints it and returns 0, or prints nothing to stdout
  and returns 1 when unset.

- [ ] **Step 1:** failing tests — env override wins; a relative config pin comes
      back absolute against cwd; an unset pin returns `None` / exit 1 with empty
      stdout; an unreadable config returns `None` rather than raising.
- [ ] **Step 2:** run `pytest tests/ops/test_baseline_pin.py -v` — expect
      ModuleNotFoundError.
- [ ] **Step 3:** add `DivergenceConfig`, the `divergence:` block in
      `config/default.yaml` pinned to `output/backtest_multi_20260819_183451.json`,
      and `scripts/ops/baseline_pin.py`.
- [ ] **Step 4:** run the test file — expect PASS.

### Task 2: `--pinned` refuses a missing pin (AC2, AC3)

**Files:**
- Modify: `scripts/divergence_monitor.py`
- Test: `tests/scripts/test_divergence_monitor.py`

**Interfaces:**
- Produces: module constants `BASELINE_PIN_MISSING = "BASELINE_PIN_MISSING"` and
  `BASELINE_SHAPE_MISMATCH = "BASELINE_SHAPE_MISMATCH"`; `--pinned` argparse flag.

- [ ] **Step 1:** failing tests, using the file's existing `_comparable_backtest`
      / `_evidence_db` / `_run_monitor_main` helpers:
      - a newer `backtest_multi_*.json` sitting in the cwd's `output/` does not
        displace the pin (assert on the report's `backtest_source`);
      - `--pinned` with an absent path returns 3 and prints
        `BASELINE_PIN_MISSING`, and writes no report;
      - `--pinned` with no `--backtest` at all returns 3, not a recency run;
      - without `--pinned`, an absent `--backtest` still returns 2 (unchanged).
- [ ] **Step 2:** run those node ids — expect FAIL.
- [ ] **Step 3:** add the flag and the resolution branch in `main()`.
- [ ] **Step 4:** run them — expect PASS.

### Task 3: shape mismatch (AC4) and the baseline_id assertion (AC5)

**Files:**
- Modify: `scripts/divergence_monitor.py`
- Test: `tests/scripts/test_divergence_monitor.py`

**Interfaces:**
- Produces: `load_backtest_sleeves(backtest_path: str) -> set[str]`;
  `shape_mismatch_reason(baseline: set[str], live: set[str]) -> str | None`.

- [ ] **Step 1:** failing tests — a pinned baseline with a sleeve the live book
      does not have (and vice versa) returns 3, prints
      `BASELINE_SHAPE_MISMATCH` naming both sides, persists no
      `divergence_daily` row and writes no report; a matching pin still scores
      normally and every row carries `baseline_id == <pin basename>`.
- [ ] **Step 2:** run — expect FAIL.
- [ ] **Step 3:** implement; run the check after `state` loads and before any
      report is built, on the full live set (so `--portfolio` cannot mask it).
- [ ] **Step 4:** run — expect PASS.

### Task 4: the alert names the reason

**Files:**
- Modify: `scripts/ops/divergence_alert.py`
- Test: `tests/deploy/test_divergence_alerting.py`

- [ ] **Step 1:** failing test — `render_alert(3, None, log_tail)` where the tail
      carries a `BASELINE_PIN_MISSING:` line mentions the pin rather than only
      "not comparable"; same for `BASELINE_SHAPE_MISMATCH`; with neither token
      the existing exit-3 text is unchanged.
- [ ] **Step 2:** run — expect FAIL.
- [ ] **Step 3:** add `_pin_failure_line(log_tail)`, consulted first in the
      exit-3 branch.
- [ ] **Step 4:** run — expect PASS.

### Task 5: the wrapper passes the pin (AC1)

**Files:**
- Modify: `deploy/launchd/run_divergence.sh`
- Test: `tests/deploy/test_divergence_alerting.py`

- [ ] **Step 1:** failing tests — the monitor's argv contains `--backtest <pin>`
      and `--pinned`; a resolver that produces nothing still runs the monitor
      (which then exits 3) rather than silently omitting `--backtest`; the log
      records the resolved pin. Extend `_drive_wrapper` to record the monitor's
      argv and to dispatch `baseline_pin.py` to the real interpreter.
- [ ] **Step 2:** run — expect FAIL.
- [ ] **Step 3:** edit the wrapper.
- [ ] **Step 4:** run — expect PASS. Re-run the whole file: the exit-code
      branches must be untouched.

### Task 6: the refresh cannot delete the pin (AC6)

**Files:**
- Modify: `deploy/launchd/run_backtest_refresh.sh`
- Test: `tests/deploy/test_backtest_refresh_snapshot.py`

- [ ] **Step 1:** failing test — stage a pinned artifact and an unpinned one in
      the stub tree, both with 200-day-old mtimes, set `ALGO_BASELINE_PIN`, drive
      a successful refresh: the pin's bytes are unchanged and it still exists,
      the unpinned one is pruned. Second test: with no pin resolvable the prune
      is skipped and the log says why.
- [ ] **Step 2:** run — expect FAIL (pin deleted).
- [ ] **Step 3:** resolve the pin in the wrapper; prune with `! -samefile "$PIN"`,
      or skip the prune entirely when the pin cannot be resolved.
- [ ] **Step 4:** run — expect PASS.

### Task 7: docs (AC7) and the full suite (AC8)

**Files:**
- Modify: `docs/operations/backtest-baseline.md`

- [ ] **Step 1:** add a "The pinned baseline of record" section: what the pin is,
      where it lives, the two named refusals, the re-pin procedure at a rung
      change, the refresh-prune guarantee, and the standing note that a pinned
      baseline no longer moves with the weekly refresh so KAN-56 staleness now
      measures "has not been re-pinned".
- [ ] **Step 2:** run `pytest` and quote the output.
- [ ] **Step 3:** commit.
