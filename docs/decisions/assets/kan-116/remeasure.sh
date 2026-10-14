#!/bin/bash
# KAN-116: re-run the 10-year comparisons of docs/decisions/backtest-position-source-2026-10.md.
#
#   remeasure.sh <before code root> <after code root> <out dir>
#
# <before> is `git archive 6b93d6a | tar -x -C <dir>`; <after> is a checkout of the
# KAN-116 change. Every run loads bars an earlier refresh already fetched
# (--bars-from-json), so IB is never contacted, and writes only under <out dir>.
# Inputs are read, never written. Override their locations with the variables below.
set -u
BEFORE=$1; AFTER=$2; OUT=$3
DEPLOY=${ALGO_DEPLOY_DIR:-$HOME/algo-poc-deploy}
MAIN=${ALGO_MAIN_CHECKOUT:-$HOME/GitHub/algo-poc}
MEMBERSHIP=${ALGO_MEMBERSHIP_SNAPSHOT:-$DEPLOY/data/universe/sp500_membership.json}

run() {  # label root bars cache [extra args...]
    local label=$1 root=$2 bars=$3 cache=$4; shift 4
    mkdir -p "$OUT/$label"
    (cd "$root" && ALGO_DATA_CACHE_DIR=$cache PYTHONPATH=$root python scripts/run_backtest.py \
        --years 10 "${@:---capital=100000}" --universe-snapshots "$MEMBERSHIP" \
        --allow-degraded-data --bars-from-json "$bars" --output-dir "$OUT/$label" \
        > "$OUT/$label/run.log" 2>&1)
    echo "$label: exit $?"
}

for side in before after; do
    root=$BEFORE; [ "$side" = after ] && root=$AFTER
    # A: the weekly refresh as it runs today (deploy clone caches)
    run "refresh_20261013_$side" "$root" "$DEPLOY/output/backtest_multi_20261013_114528.json" "$DEPLOY/data/cache"
    # B: the D10-of-record bars, with the caches that run used
    run "d10_20260819_$side" "$root" "$MAIN/output/backtest_multi_20260819_183451.json" "$MAIN/data/cache"
    # The edge pin's config, and the Rung-0 pin's
    run "pin_20260915_$side" "$root" "$DEPLOY/output/backtest_multi_20260915_102125.json" "$MAIN/data/cache"
    run "rung0_20260925_$side" "$root" "$DEPLOY/output/baselines/rung0_momentum_20260925.json" "$MAIN/data/cache" \
        --sleeves momentum --capital 3700 --whole-shares
done
echo "D10: python $(dirname "$0")/d10_rehearsal.py <label>=<artifact> ... (one per refresh_/d10_ artifact)"
