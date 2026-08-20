"""KAN-51: resolving the pinned divergence baseline for shell callers.

``deploy/launchd/run_divergence.sh`` and
``deploy/launchd/run_backtest_refresh.sh`` are bash, and the pin of record lives
in ``config/default.yaml``. This module is the one seam that turns the config
fact into a path a shell can use, so the wrappers never hardcode it and never
parse YAML themselves.

The resolver's contract is deliberately quiet on failure: it prints nothing to
stdout and returns 1. Being loud about a missing pin is the *monitor's* job
(exit 3, ``BASELINE_PIN_MISSING``) — if this script also decided, the wrapper
would have two places that could disagree about whether the run should happen.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

from scripts.ops.baseline_pin import main, resolve_pin

REPO = Path(__file__).resolve().parents[2]


def _config(tmp_path: Path, pin: object) -> Path:
    """Write a minimal config whose ``divergence`` block holds ``pin``."""
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"divergence": {"baseline_pin": pin}}))
    return path


def test_a_relative_config_pin_resolves_against_the_working_directory(
    tmp_path: Path, monkeypatch
):
    """The wrapper cds to $ALGO_DIR first, so cwd is the deployed checkout.

    Absolute is what the callers need: ``find ... ! -samefile`` in the refresh
    wrapper compares against the path this prints, and a relative one would
    only match by accident.
    """
    monkeypatch.chdir(tmp_path)
    config = _config(tmp_path, "output/backtest_multi_20260819_183451.json")

    assert resolve_pin(str(config)) == str(
        tmp_path / "output" / "backtest_multi_20260819_183451.json"
    )


def test_an_absolute_config_pin_is_returned_unchanged(tmp_path: Path):
    pinned = tmp_path / "elsewhere" / "backtest_multi_20260819_183451.json"
    config = _config(tmp_path, str(pinned))

    assert resolve_pin(str(config)) == str(pinned)


def test_the_env_override_beats_the_config(tmp_path: Path, monkeypatch):
    """``ALGO_BASELINE_PIN`` exists so the wrapper tests can drive a stub tree,
    and so an operator can score one ad-hoc run against another artifact without
    editing the committed pin."""
    config = _config(tmp_path, "output/from_config.json")
    override = tmp_path / "from_env.json"
    monkeypatch.setenv("ALGO_BASELINE_PIN", str(override))

    assert resolve_pin(str(config)) == str(override)


def test_no_pin_configured_resolves_to_none(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ALGO_BASELINE_PIN", raising=False)

    assert resolve_pin(str(_config(tmp_path, None))) is None


def test_an_unreadable_config_resolves_to_none_rather_than_raising(
    tmp_path: Path, monkeypatch
):
    """The refresh wrapper runs this from a tree that may not carry a config at
    all. A traceback on stdout would be read as a path by the caller."""
    monkeypatch.delenv("ALGO_BASELINE_PIN", raising=False)

    assert resolve_pin(str(tmp_path / "absent.yaml")) is None


def test_the_pin_need_not_exist_to_resolve(tmp_path: Path, monkeypatch):
    """Existence is the monitor's judgement, not this script's — the monitor
    turns an absent pin into exit 3 with a named reason, and duplicating that
    test here would let the wrapper silently skip the run instead."""
    monkeypatch.chdir(tmp_path)
    config = _config(tmp_path, "output/never_written.json")

    assert resolve_pin(str(config)) == str(tmp_path / "output" / "never_written.json")


def test_main_prints_the_pin_and_exits_zero(tmp_path: Path, monkeypatch, capsys):
    pinned = tmp_path / "backtest_multi_20260819_183451.json"
    monkeypatch.setenv("ALGO_BASELINE_PIN", str(pinned))

    assert main(["--config", str(_config(tmp_path, None))]) == 0
    assert capsys.readouterr().out.strip() == str(pinned)


def test_main_prints_nothing_and_exits_one_when_unpinned(
    tmp_path: Path, monkeypatch, capsys
):
    """Empty stdout matters more than the exit code: the wrapper substitutes the
    output straight into ``--backtest``, and any diagnostic text on stdout would
    become a path the monitor then reports as a missing pin by the wrong name."""
    monkeypatch.delenv("ALGO_BASELINE_PIN", raising=False)

    assert main(["--config", str(_config(tmp_path, None))]) == 1
    assert capsys.readouterr().out == ""


def test_the_committed_config_declares_a_pin():
    """The mechanism is worthless if the shipped config leaves it unset — the
    nightly job would then alert BASELINE_PIN_MISSING every night. The path
    itself lives under the gitignored ``output/``, so this asserts the pin is
    *declared*, not that the artifact is present in a fresh clone."""
    config = yaml.safe_load((REPO / "config" / "default.yaml").read_text())

    pin = (config.get("divergence") or {}).get("baseline_pin")
    assert isinstance(pin, str) and pin.endswith(".json"), pin


def test_the_script_is_runnable_by_path_from_a_launchd_wrapper(
    tmp_path: Path, monkeypatch
):
    """The wrappers invoke it as ``$VENV scripts/ops/baseline_pin.py``, which
    puts ``scripts/ops/`` — not the repo root — on sys.path. Run it that way so
    a missing sys.path fix-up fails here rather than at 04:45."""
    pinned = tmp_path / "backtest_multi_20260819_183451.json"
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "ops" / "baseline_pin.py")],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={"PATH": "/usr/bin:/bin", "ALGO_BASELINE_PIN": str(pinned)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(pinned)
