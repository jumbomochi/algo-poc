# Is the paper DB schema at the code's alembic head? (KAN-103)
#
# run_paper.sh has refused to start on a schema mismatch since the 2026-07-25
# incident, where a migration landed without `alembic upgrade head` and
# surfaced mid-run as a cryptic psycopg2 UndefinedColumn. The other wrappers
# that read the paper DB never had the check. KAN-103 adds
# equity_snapshots.session_date, which every reader of that table now names,
# so a pull without a migrate would kill the 04:45 divergence monitor and the
# Monday digest mid-run with that same raw error. One implementation, sourced
# by path like the rest of lib/, so the three wrappers cannot disagree about
# what "behind" means.
#
# Usage:
#   algo_schema_guard "$ALGO_DIR"      # ALGO_DATABASE_URL must be exported
#
# Returns 0 when the DB revision equals the head revision. Otherwise returns 1
# and sets:
#   ALGO_SCHEMA_ERROR    one line naming the fault and the fix
#   ALGO_SCHEMA_DB_REV   the DB's revision ('' when unreadable)
#   ALGO_SCHEMA_HEAD_REV the code's head revision ('' when unreadable)
#
# It never migrates. `alembic upgrade head` against the paper DB is a human's
# call, and an unattended job that applied one would be the outage, not the
# guard against it.
#
# ALGO_ALEMBIC_BIN overrides the binary (tests only); the default is the tree's
# own venv, matching the python every wrapper runs.

algo_schema_guard() {
    local dir="$1"
    local alembic="${ALGO_ALEMBIC_BIN:-$dir/.venv/bin/alembic}"
    ALGO_SCHEMA_ERROR=""
    ALGO_SCHEMA_DB_REV=""
    ALGO_SCHEMA_HEAD_REV=""

    # alembic.ini resolves migrations/ relative to the working directory, so
    # ask from the tree, in a subshell that leaves the caller's cwd alone.
    ALGO_SCHEMA_HEAD_REV=$(cd "$dir" 2>/dev/null && "$alembic" heads 2>/dev/null \
        | grep -oE '[0-9a-f]{12}' | head -1)
    if [ -z "$ALGO_SCHEMA_HEAD_REV" ]; then
        ALGO_SCHEMA_ERROR="could not determine alembic head revision"
        return 1
    fi
    ALGO_SCHEMA_DB_REV=$(cd "$dir" 2>/dev/null && "$alembic" current 2>/dev/null \
        | grep -oE '[0-9a-f]{12}' | head -1)
    if [ "$ALGO_SCHEMA_DB_REV" != "$ALGO_SCHEMA_HEAD_REV" ]; then
        ALGO_SCHEMA_ERROR="paper DB schema out of date (DB at '${ALGO_SCHEMA_DB_REV:-none}', head '$ALGO_SCHEMA_HEAD_REV'); run '.venv/bin/alembic upgrade head' with ALGO_DATABASE_URL set"
        return 1
    fi
    return 0
}
