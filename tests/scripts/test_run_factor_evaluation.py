from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from scripts.run_factor_evaluation import main


def _write_bars(path: Path, n_days=400):
    start = date(2024, 1, 1)
    tickers = {"A": 1.0, "B": 0.6, "C": 0.3, "D": -0.2, "E": -0.5, "F": -0.9}
    bars = {}
    for ticker, drift in tickers.items():
        price = 100.0
        rows = []
        for i in range(n_days):
            price = max(1.0, price + drift)
            rows.append({"date": (start + timedelta(days=i)).isoformat(), "open": price,
                         "high": price + 1, "low": price - 1, "close": price, "volume": 1_000 + i})
        bars[ticker] = rows
    path.write_text(json.dumps({"bars": bars}))


def test_cli_writes_run_card_with_all_factors(tmp_path):
    bars_path = tmp_path / "bars.json"
    _write_bars(bars_path)
    out_dir = tmp_path / "out"
    code = main([
        "--bars-from-json", str(bars_path), "--output-dir", str(out_dir),
        "--horizon", "5", "--outer-folds", "3", "--inner-folds", "2", "--min-names", "3",
    ])
    assert code == 0
    cards = list(out_dir.glob("factor_evaluation_*.json"))
    assert len(cards) == 1
    card = json.loads(cards[0].read_text())
    assert set(card["evaluation"]["factors"]) == {
        "price_momentum_126d", "high_52w", "low_volatility_63d", "liquidity_20d"}
