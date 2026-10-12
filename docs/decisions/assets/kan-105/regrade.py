"""KAN-105: re-grade live against the shadow under the old and new seeding. READ ONLY.

Reproduces docs/decisions/shadow-seeding-2026-10.md, "Re-grade". Reads the
inputs ``extract_inputs.py`` wrote (bars, ``equity_snapshots``) and the
fundamentals/earnings caches; writes nothing but its stdout and
``regrade.json`` in ``--inputs``.

For every window end it rebuilds what the 05:15 run would have produced for
the window ending on that session, two ways:

* ``old`` — the pre-KAN-105 replay, reproduced inline: one year of bars, the
  sleeve's signal function fed every bar, buys refused before the window
  (``trade_start_date=window_start``), seeded at live's NAV there. The
  investigation validated this reproduction exactly against the
  10-06..10-09 shadow artifacts.
* ``A<N>`` — this PR's ``backtest.shadow_series.replay_window_detail`` at
  warm-up ``N`` on two years of bars (the new fetch), exactly as
  ``scripts/run_paper.produce_shadow_artifact`` calls it. ``Amax`` is the
  longest warm-up the bars allow (the indicator lookback still enforced).

and grades each against live (``equity_snapshots`` by ``session_date``, later
run wins, from ``divergence.live_history_from``) with the monitor's own
functions over a 30-session window.

quality_value / earnings_drift are not replayed in production (KAN-109). They
are graded here on whatever caches ``--fundamentals-cache`` /
``--earnings-cache`` point at — a STRUCTURAL check of the seeding, not
strategy evidence.

Run from the repo root::

    PYTHONPATH=. python docs/decisions/assets/kan-105/regrade.py \\
        --inputs /tmp/kan105 --ends 2026-09-23:2026-10-09 --warmups 160
"""
from __future__ import annotations

import argparse
import json
import pickle
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO))

from backtest.costs import CostModel  # noqa: E402
from backtest.divergence import (  # noqa: E402  pure functions, no I/O
    align_and_window,
    classify_status,
    compute_divergence,
    correlation,
    daily_returns,
    window_return,
)
from backtest.runner import BacktestRunner, collect_sorted_dates  # noqa: E402
from backtest.shadow_series import (  # noqa: E402
    SHADOW_LOOKBACK_SESSIONS,
    replay_window_detail,
)
from backtest.simulator import SimulatedExecutor  # noqa: E402
from shared.session_dating import equity_by_session  # noqa: E402

SLEEVES = ["momentum", "sector_rotation", "thematic_momentum",
           "quality_value", "earnings_drift", "tail_risk_hedge"]
DATA_SLEEVES = {"quality_value", "earnings_drift"}
BOUNDARY = date(2026, 8, 1)  # config/default.yaml divergence.live_history_from
WINDOW = 30
CAPITAL = 100_000.0  # production's shadow build capital (validated in the study)


def _lookups(fundamentals_dir: str | None, earnings_dir: str | None):
    from scripts.fetch_earnings import build_earnings_lookup
    from scripts.fetch_fundamentals import build_fundamentals_lookup
    from shared.data_cache import EARNINGS_WINDOW_DAYS, load_data_caches

    empty = (build_fundamentals_lookup({}),
             build_earnings_lookup({}, window_days=EARNINGS_WINDOW_DAYS))
    fundamentals = (load_data_caches(fundamentals_dir).fundamentals.data or {}
                    if fundamentals_dir else {})
    earnings = (load_data_caches(earnings_dir).earnings.data or {}
                if earnings_dir else {})
    cached = (build_fundamentals_lookup(fundamentals),
              build_earnings_lookup(earnings, window_days=EARNINGS_WINDOW_DAYS))
    return empty, cached


def _portfolio(sleeve, bars, regime, lookups):
    """A FRESH sleeve: its signal function is stateful, so one per replay."""
    from scripts.run_paper import build_portfolios

    empty, cached = lookups
    fl, el = cached if sleeve in DATA_SLEEVES else empty
    return build_portfolios(capital=CAPITAL, bars_by_ticker=bars,
                            regime_by_date=regime, fundamentals_lookup=fl,
                            earnings_lookup=el, portfolio_contexts=None)[sleeve]


def _truncate(bars, start: date, end: date):
    return {t: [b for b in rows if start <= b["date"] <= end]
            for t, rows in bars.items()}


def _old_replay(pf, bars, seed, ws):
    """The pre-KAN-105 ``replay_window`` (no flows): every bar reaches the
    signal fn, buys refused before ws, no normalisation."""
    result = BacktestRunner(SimulatedExecutor(CostModel()),
                            initial_capital=seed).run(
        bars, pf.signals_fn, pf.risk_engine, trade_start_date=ws)
    eod = dict(zip(result.dates, result.portfolio_values[1:]))
    return {d: v for d, v in eod.items() if d >= ws}


def _grade(live, shadow):
    dates, lv, sv = align_and_window(live, shadow, WINDOW)
    if len(dates) < 2:
        return {"n": len(dates), "status": "NO_DATA"}
    lr, sr = window_return(lv), window_return(sv)
    absolute, relative = compute_divergence(lr, sr)
    return {"n": len(dates), "start": dates[0].isoformat(),
            "end": dates[-1].isoformat(), "live": lr, "shadow": sr,
            "abs": absolute, "rel": relative,
            "corr": correlation(daily_returns(lv), daily_returns(sv)),
            "status": classify_status(relative, absolute)}


def _live(snapshots, sleeve, run: date):
    rows = [r for r in snapshots if r["portfolio"] == sleeve
            and date.fromisoformat(str(r["date"])[:10]) <= run]
    equity = equity_by_session(rows)
    exposure: dict[date, tuple[date, float]] = {}
    for r in rows:
        if not r["session_date"] or not r["equity"]:
            continue
        session = date.fromisoformat(str(r["session_date"])[:10])
        written = date.fromisoformat(str(r["date"])[:10])
        if session not in exposure or written >= exposure[session][0]:
            exposure[session] = (written,
                                 float(r["market_value"]) / float(r["equity"]))
    return equity, {d: v for d, (_, v) in exposure.items()}


def main() -> None:
    from scripts.run_backtest import compute_regime_by_date

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--ends", default="2026-09-23:2026-10-09",
                        help="first:last window-end session, inclusive")
    parser.add_argument("--warmups", default="160",
                        help="comma-separated N values; 'max' allowed")
    parser.add_argument("--fundamentals-cache",
                        default=str(Path.home() / "GitHub/algo-poc/data/cache"))
    parser.add_argument("--earnings-cache",
                        default=str(Path.home() / "algo-poc-deploy/data/cache"))
    args = parser.parse_args()

    inputs = Path(args.inputs)
    with open(inputs / "bars.pkl", "rb") as fh:
        bars_all = pickle.load(fh)
    snapshots = json.loads((inputs / "snapshots.json").read_text())
    lookups = _lookups(args.fundamentals_cache, args.earnings_cache)
    first, last = (date.fromisoformat(x) for x in args.ends.split(":"))
    warmups = [w.strip() for w in args.warmups.split(",")]
    newest_bar = max(rows[-1]["date"] for rows in bars_all.values() if rows)

    momentum_live, _ = _live(snapshots, "momentum", date.today())
    ends = [d for d in momentum_live if first <= d <= min(last, newest_bar)]
    out: dict = {}
    for end in ends:
        run = end + timedelta(days=1)  # the SGT run that valued ``end``
        one_year = _truncate(bars_all, run - timedelta(days=365), end)
        two_years = _truncate(bars_all, run - timedelta(days=730), end)
        regime_1y = compute_regime_by_date(one_year)
        regime_2y = compute_regime_by_date(two_years)
        sessions_2y = collect_sorted_dates(two_years)
        for sleeve in SLEEVES:
            live_all, exposure = _live(snapshots, sleeve, run)
            live_all = {d: v for d, v in live_all.items() if d <= end}
            if len(live_all) < WINDOW:
                continue
            ws = sorted(live_all)[-WINDOW:][0]
            seed = live_all[ws]
            live = {d: v for d, v in live_all.items() if d >= BOUNDARY}
            live_exp = [exposure[d] for d in sorted(exposure) if ws <= d <= end]
            row = {"ws": ws.isoformat(), "seed": seed,
                   "live_exposure": [live_exp[0], live_exp[-1],
                                     sum(live_exp) / len(live_exp)]
                   if live_exp else None}
            pf = _portfolio(sleeve, one_year, regime_1y, lookups)
            old = _old_replay(pf, one_year, seed, ws)
            # 0 by construction, not measured: the old replay refused every
            # buy before ws, and a buy decided at ws's close fills at ws+1's
            # open at the earliest, so the book holds only cash at ws's close.
            row["old"] = {**_grade(live, {d: v for d, v in old.items()
                                          if d in live}),
                          "invested_at_ws": 0.0}
            for w in warmups:
                n = (sessions_2y.index(ws) - SHADOW_LOOKBACK_SESSIONS
                     if w == "max" else int(w))
                pf = _portfolio(sleeve, two_years, regime_2y, lookups)
                detail = replay_window_detail(
                    bars_by_ticker=two_years, signals_fn=pf.signals_fn,
                    risk_engine=pf.risk_engine, seed_nav=seed,
                    window_start=ws, warmup_sessions=n,
                )
                curve = {d: v for d, v in detail.curve.items() if d in live}
                row[f"A{w}"] = {**_grade(live, curve),
                                "invested_at_ws": detail.invested_at_start,
                                "warm_from": detail.warm_from.isoformat(),
                                "curve_ws": detail.curve.get(ws)}
            out.setdefault(sleeve, {})[end.isoformat()] = row
        print(f"{end} graded", file=sys.stderr, flush=True)

    (inputs / "regrade.json").write_text(json.dumps(out, indent=1, default=str))
    _print(out, ["old"] + [f"A{w}" for w in warmups])


def _pp(x):
    return "—" if x is None else f"{x * 100:+.2f}"


def _print(out, variants) -> None:
    latest = max(e for rows in out.values() for e in rows)
    first_row = out["momentum"][latest]
    print(f"\n### Latest window: {first_row['ws']}..{latest}\n")
    print("| sleeve | live | live invested ws/end/mean | "
          + " | ".join(f"{v}: shadow / gap pp / corr / status / invested@ws"
                       for v in variants) + " |")
    print("|---|---|---|" + "---|" * len(variants))
    for sleeve, rows in out.items():
        row = rows.get(latest)
        if row is None:
            continue
        le = row["live_exposure"]
        cells = []
        for v in variants:
            g = row[v]
            corr = "—" if g.get("corr") is None else f"{g['corr']:+.2f}"
            cells.append(f"{_pp(g.get('shadow'))}% / {_pp(g.get('abs'))} / "
                         f"{corr} / {g['status']} / "
                         f"{(g['invested_at_ws'] or 0) * 100:.0f}%")
        live_ret = row[variants[0]].get("live")
        print(f"| {sleeve} | {_pp(live_ret)}% | "
              + (f"{le[0]*100:.0f}% / {le[1]*100:.0f}% / {le[2]*100:.0f}%"
                 if le else "—")
              + " | " + " | ".join(cells) + " |")

    ends = sorted({e for rows in out.values() for e in rows})
    print(f"\n### All windows ({ends[0]}..{ends[-1]}, {len(ends)} ends; "
          "status string ordered by window end, . = OK)\n")
    print("| sleeve | " + " | ".join(
        f"{v}: statuses / BREACH / mean abs gap / day-to-day shadow change "
        "mean, max" for v in variants) + " |")
    print("|---|" + "---|" * len(variants))
    glyph = {"OK": ".", "WARNING": "W", "BREACH": "B", "NO_DATA": "-"}
    for sleeve, rows in out.items():
        cells = []
        for v in variants:
            seq = [rows[e][v] for e in ends if e in rows]
            statuses = "".join(glyph[g["status"]] for g in seq)
            gaps = [abs(g["abs"]) for g in seq if g.get("abs") is not None]
            rets = [g["shadow"] for g in seq if g.get("shadow") is not None]
            steps = [abs(b - a) for a, b in zip(rets, rets[1:])]
            cells.append(
                f"`{statuses}` / {statuses.count('B')} / "
                f"{sum(gaps) / len(gaps) * 100:.2f} pp / "
                + (f"{sum(steps) / len(steps) * 100:.2f}, "
                   f"{max(steps) * 100:.2f} pp" if steps else "—"))
        print(f"| {sleeve} | " + " | ".join(cells) + " |")

    if "Amax" not in variants:
        return
    warm = [v for v in variants if v.startswith("A") and v != "Amax"]
    print("\n### Residual: mean |window shadow return(N) - (N = max)| over "
          "all ends, pp (BREACH count)\n")
    print("| sleeve | " + " | ".join(v[1:] for v in warm) + " | max |")
    print("|---|" + "---|" * (len(warm) + 1))
    for sleeve, rows in out.items():
        cells = []
        for v in warm + ["Amax"]:
            diffs = [abs(r[v]["shadow"] - r["Amax"]["shadow"])
                     for r in rows.values()
                     if r[v].get("shadow") is not None
                     and r["Amax"].get("shadow") is not None]
            breaches = sum(1 for r in rows.values()
                           if r[v]["status"] == "BREACH")
            cells.append(f"{sum(diffs) / len(diffs) * 100:.2f} ({breaches})")
        print(f"| {sleeve} | " + " | ".join(cells) + " |")


if __name__ == "__main__":
    main()
