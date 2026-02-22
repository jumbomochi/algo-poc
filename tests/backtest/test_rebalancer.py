from __future__ import annotations

from scripts.run_backtest import simulate_rebalancer


def test_rebalancer_with_equal_performance():
    """Two strategies with equal linear growth. Final weights should stay
    close to initial (within 0.01 of 0.50/0.50)."""
    n_days = 252
    # Both strategies grow linearly from 100 to 120 (same performance)
    curve_a = [100.0 + 20.0 * i / (n_days - 1) for i in range(n_days)]
    curve_b = [100.0 + 20.0 * i / (n_days - 1) for i in range(n_days)]

    result = simulate_rebalancer(
        strategy_curves={"alpha": curve_a, "beta": curve_b},
        initial_weights={"alpha": 0.50, "beta": 0.50},
    )

    # Weights should remain close to equal
    last_weights = result["weights_history"][-1]["weights"]
    assert abs(last_weights["alpha"] - 0.50) < 0.01
    assert abs(last_weights["beta"] - 0.50) < 0.01


def test_rebalancer_shifts_toward_outperformer():
    """Strategy 'alpha' grows ~28%, 'beta' grows ~2.5%. After rebalancing,
    alpha's weight should be > 0.50."""
    import random

    rng = random.Random(42)
    n_days = 252
    # alpha: high daily drift with some noise -> higher Sharpe
    curve_alpha = [100.0]
    for _ in range(n_days - 1):
        curve_alpha.append(curve_alpha[-1] * (1 + 0.001 + rng.gauss(0, 0.005)))
    # beta: low daily drift with same noise -> lower Sharpe
    curve_beta = [100.0]
    for _ in range(n_days - 1):
        curve_beta.append(curve_beta[-1] * (1 + 0.0001 + rng.gauss(0, 0.005)))

    result = simulate_rebalancer(
        strategy_curves={"alpha": curve_alpha, "beta": curve_beta},
        initial_weights={"alpha": 0.50, "beta": 0.50},
        ceiling_pct=0.70,
    )

    last_weights = result["weights_history"][-1]["weights"]
    assert last_weights["alpha"] > 0.50


def test_rebalancer_respects_floor_and_ceiling():
    """Extreme difference over 250 days. With floor=0.10, special_floor
    for beta=0.15, ceiling=0.25. Assert no weight entry violates
    constraints."""
    n_days = 250
    # alpha: massive growth
    curve_alpha = [100.0 * (1.001) ** i for i in range(n_days)]
    # beta: declining
    curve_beta = [100.0 * (0.999) ** i for i in range(n_days)]
    # gamma: flat
    curve_gamma = [100.0 for _ in range(n_days)]
    # delta: moderate growth
    curve_delta = [100.0 * (1.0005) ** i for i in range(n_days)]

    result = simulate_rebalancer(
        strategy_curves={
            "alpha": curve_alpha,
            "beta": curve_beta,
            "gamma": curve_gamma,
            "delta": curve_delta,
        },
        initial_weights={
            "alpha": 0.25,
            "beta": 0.25,
            "gamma": 0.25,
            "delta": 0.25,
        },
        floor_pct=0.10,
        ceiling_pct=0.25,
        special_floors={"beta": 0.15},
    )

    for entry in result["weights_history"]:
        weights = entry["weights"]
        for name, w in weights.items():
            floor = 0.15 if name == "beta" else 0.10
            assert w >= floor - 1e-9, (
                f"Weight for {name} = {w} below floor {floor} at day {entry['day_index']}"
            )
            assert w <= 0.25 + 1e-9, (
                f"Weight for {name} = {w} above ceiling 0.25 at day {entry['day_index']}"
            )


def test_rebalancer_output_length_matches_input():
    """Single strategy, verify rebalanced_values has same length as input."""
    n_days = 150
    curve = [100.0 + 0.1 * i for i in range(n_days)]

    result = simulate_rebalancer(
        strategy_curves={"only": curve},
        initial_weights={"only": 1.0},
    )

    assert len(result["rebalanced_values"]) == n_days
