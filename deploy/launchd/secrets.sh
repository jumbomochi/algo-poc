#!/bin/bash
# Shared secret loader for the algo-poc launchd wrappers.
#
# WHY THIS EXISTS
# ---------------
# On 2026-08-12 10:51 the repo's `.env` stopped being a file: 1Password
# Environments replaced it with a named pipe (FIFO) it serves from the desktop
# app. Every wrapper read credentials with `grep '^POSTGRES_PASSWORD=' .env`,
# and against an app-backed FIFO that nothing is serving, that open BLOCKS for
# ~60s and then returns nothing. Arithmetic from the logs:
#
#   run_paper.sh      04:15:00 start, 2 reads -> error at 04:17:01  (~121s)
#   run_divergence.sh 04:45:00 start, 1 read  -> error at 04:46:02  (~62s)
#
# Both jobs aborted before doing any work on 2026-08-13 and 2026-08-14, and
# nobody was told, because gateway_watchdog.sh gated its own Telegram alerting
# on `[ -f "$ENV_FILE" ]` — which is FALSE for a FIFO. The alert path
# short-circuited to *success* and stayed quiet. The FIFO cost two sessions;
# the silent-skip cost the two days it took to notice.
#
# It also only fails at 04:15. With the operator at the keyboard and 1Password
# unlocked, the pipe serves instantly, so every hand-test passes.
#
# STORE OF RECORD: the macOS login keychain
#   service = $ALGO_KEYCHAIN_SERVICE  (default "algo-poc")
#   account = the variable name, e.g. POSTGRES_PASSWORD
#
# Why the keychain and not a 1Password service-account token: the token would
# itself be plaintext on disk (launchd cannot do interactive auth), and unlike
# these loopback-only Postgres/Redis passwords it is a *network* credential
# usable from any machine — a strictly wider blast radius for no gain on the
# "no auth at 4am" requirement. A keychain item is encrypted at rest and grants
# nothing off this box.
#
# Verified on this host: `security find-generic-password` returns the value with
# no controlling TTY, closed stdin and a stripped environment (i.e. launchd's
# conditions), exit 0, no prompt. The login keychain is `no-timeout` here, so
# screen lock and sleep do NOT relock it. Only logout, or a reboot with nobody
# logging in, leaves it locked — and that already breaks Docker Desktop and IB
# Gateway, which these jobs need anyway. That case is reported as LOCKED rather
# than as a missing secret, because the operator action is different.
#
# This file is SOURCED BY PATH from the repo (`$ALGO_DIR/deploy/launchd/`), not
# from ~/ibc, so there is exactly one copy of the lookup logic and it cannot
# drift the way the hand-copied wrappers did on 2026-08-11. deploy.sh
# deliberately does not copy it.
#
# Usage (sourced):
#   . "$ALGO_DIR/deploy/launchd/secrets.sh"
#   if ! algo_load_secrets POSTGRES_PASSWORD REDIS_PASSWORD; then
#       echo "$(date): ERROR - $ALGO_SECRETS_ERROR" >> "$LOG_FILE"; exit 1
#   fi
#
#   # one secret, keeping the failure reason:
#   if algo_secret_into TELEGRAM_BOT_TOKEN; then token="$_ALGO_SECRET_VALUE"; fi
#
# Do NOT write `v=$(algo_secret X)` when you intend to log why it failed: a
# command substitution is a subshell and $ALGO_SECRETS_ERROR will come back
# empty.
#
# Usage (CLI):
#   deploy/launchd/secrets.sh --check              # presence only, no values
#   deploy/launchd/secrets.sh --import             # interactive, argv-free
#   deploy/launchd/secrets.sh --import-from-env F  # bulk (see caveat below)
#   eval "$(deploy/launchd/secrets.sh --export)"   # for docker compose / shells

ALGO_KEYCHAIN_SERVICE="${ALGO_KEYCHAIN_SERVICE:-algo-poc}"
ALGO_SECRETS_ENV_FILE="${ALGO_SECRETS_ENV_FILE:-/Users/huiliang/GitHub/algo-poc/.env}"

# Absolute path by default so a hijacked PATH cannot substitute the binary that
# reads our secrets. Overridable only so the test suite can stub it.
ALGO_SECURITY_BIN="${ALGO_SECURITY_BIN:-/usr/bin/security}"

# Every secret the stack needs. Order is the order --import prompts in.
# Overridable so an import/check can be scoped to a subset, e.g.
#   ALGO_SECRET_NAMES="POSTGRES_PASSWORD REDIS_PASSWORD" ... --import-from-env F
ALGO_SECRET_NAMES="${ALGO_SECRET_NAMES:-POSTGRES_PASSWORD REDIS_PASSWORD TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID API_KEYS}"

# Human-readable reason the last lookup failed. Callers log this verbatim; it
# names the operator action, which is the whole point of separating the failure
# modes.
ALGO_SECRETS_ERROR=""

# ---------------------------------------------------------------------------
# .env file state
# ---------------------------------------------------------------------------

# Classify $ALGO_SECRETS_ENV_FILE as absent / regular / irregular.
#
# `[ -e ]` and `[ -f ]` both stat(2) and never block, so this is safe to call
# even when the path is a FIFO with no writer. The distinction is the fix for
# the 2026-08-12 incident: "irregular" must be a LOUD failure, never the
# silent "no .env, carry on" that `[ -f ] || return 0` produced.
_algo_env_file_state() {
    if [ ! -e "$ALGO_SECRETS_ENV_FILE" ]; then
        printf 'absent'
    elif [ -f "$ALGO_SECRETS_ENV_FILE" ]; then
        printf 'regular'
    else
        printf 'irregular'
    fi
}

# Name the file type for the error message ("fifo", "socket", ...), so the log
# line says what is actually there instead of just "not a file".
_algo_env_file_kind() {
    local kind
    kind=$(stat -f '%HT' "$ALGO_SECRETS_ENV_FILE" 2>/dev/null) || kind=""
    [ -n "$kind" ] || kind="unknown type"
    printf '%s' "$kind"
}

# ---------------------------------------------------------------------------
# Keychain access
# ---------------------------------------------------------------------------

# Lookups hand their result back through this global rather than through
# stdout. A command substitution runs in a SUBSHELL, so `val=$(lookup ...)`
# would discard every assignment to $ALGO_SECRETS_ERROR — losing exactly the
# diagnostic that distinguishes "keychain LOCKED" from "secret not imported".
_ALGO_SECRET_VALUE=""

_algo_secret_from_keychain() {
    local name="$1" out rc
    # stderr is merged so the failure reason can be classified; `out` is only
    # inspected when rc != 0, so a secret is never pattern-matched.
    out=$("$ALGO_SECURITY_BIN" find-generic-password -w \
              -s "$ALGO_KEYCHAIN_SERVICE" -a "$name" 2>&1)
    rc=$?
    if [ "$rc" -eq 0 ]; then
        _ALGO_SECRET_VALUE="$out"
        return 0
    fi
    case "$out" in
        *"interaction is not allowed"*|*"-25308"*)
            ALGO_SECRETS_ERROR="login keychain is LOCKED, so '$name' cannot be read. A launchd user agent needs a logged-in GUI session; after a reboot with no login the keychain stays locked (Docker Desktop and IB Gateway would be down too). Log in, then re-run."
            ;;
        *"could not be found"*|*"-25300"*)
            ALGO_SECRETS_ERROR="keychain service '$ALGO_KEYCHAIN_SERVICE' has no item for '$name'. Import it with: deploy/launchd/secrets.sh --import"
            ;;
        *)
            ALGO_SECRETS_ERROR="keychain lookup for '$name' failed: $(printf '%s' "$out" | tr '\n' ' ')"
            ;;
    esac
    return 1
}

_algo_secret_from_env_file() {
    local name="$1" val
    [ "$(_algo_env_file_state)" = "regular" ] || return 1
    val=$(grep "^${name}=" "$ALGO_SECRETS_ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
    [ -n "$val" ] || return 1
    _ALGO_SECRET_VALUE="$val"
    return 0
}

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# algo_secret_into NAME -> on success sets $_ALGO_SECRET_VALUE and returns 0;
# on failure returns 1 with $ALGO_SECRETS_ERROR set. This is the form to use
# whenever you need the error message, because it runs in the CALLER's shell.
#
# Keychain first; a *regular-file* .env is accepted as a fallback so the stack
# keeps working mid-migration and if the operator deliberately reverts to
# plaintext.
algo_secret_into() {
    local name="$1" kc_err
    ALGO_SECRETS_ERROR=""
    _ALGO_SECRET_VALUE=""

    if _algo_secret_from_keychain "$name"; then
        return 0
    fi
    kc_err="$ALGO_SECRETS_ERROR"

    case "$(_algo_env_file_state)" in
        irregular)
            ALGO_SECRETS_ERROR="$ALGO_SECRETS_ENV_FILE exists but is NOT a regular file (it is a $(_algo_env_file_kind)) — e.g. a 1Password Environments pipe. Reading it blocks ~60s and yields nothing, so it is being refused rather than read. Import the secrets into the keychain: deploy/launchd/secrets.sh --import. [keychain: $kc_err]"
            return 1
            ;;
        regular)
            if _algo_secret_from_env_file "$name"; then
                return 0
            fi
            ALGO_SECRETS_ERROR="'$name' is in neither the keychain nor $ALGO_SECRETS_ENV_FILE. [keychain: $kc_err]"
            return 1
            ;;
        *)
            ALGO_SECRETS_ERROR="$kc_err [no $ALGO_SECRETS_ENV_FILE to fall back to]"
            return 1
            ;;
    esac
}

# algo_secret NAME -> prints the value on stdout. Convenience for the CLI and
# for one-off use. NOTE: called as `v=$(algo_secret X)` it runs in a subshell,
# so $ALGO_SECRETS_ERROR will NOT propagate to you — use algo_secret_into when
# you intend to log the reason.
algo_secret() {
    algo_secret_into "$1" || return 1
    printf '%s' "$_ALGO_SECRET_VALUE"
}

# algo_load_secrets NAME... -> exports each name, or returns 1 on the first
# failure with $ALGO_SECRETS_ERROR set for the caller to log.
algo_load_secrets() {
    local name
    for name in "$@"; do
        algo_secret_into "$name" || return 1
        export "$name=$_ALGO_SECRET_VALUE"
    done
    return 0
}

# algo_alert_local MESSAGE — secret-free alerting of last resort.
#
# When the keychain is locked there is no Telegram token, so the Telegram path
# cannot run: that is exactly the state that went unnoticed for two days. This
# needs no credential at all. It appends to a single persistent file (not a
# per-day log that a failed run never creates) and raises a desktop
# notification in the Aqua session. Best-effort; never fails a caller.
algo_alert_local() {
    local msg="$1" alert_log="${HOME}/ibc/logs/ALERTS.log"
    mkdir -p "$(dirname "$alert_log")" 2>/dev/null || true
    printf '%s: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$msg" >> "$alert_log" 2>/dev/null || true
    /usr/bin/osascript -e 'on run argv
        display notification (item 1 of argv) with title "algo-poc job failed"
    end run' "$msg" >/dev/null 2>&1 || true
    return 0
}

# ---------------------------------------------------------------------------
# CLI (only when executed directly, never when sourced)
# ---------------------------------------------------------------------------

_algo_keychain_put_interactive() {
    # No -w VALUE, so `security` prompts and reads the secret itself: the value
    # never appears in argv (visible to `ps`) or in shell history.
    local name="$1"
    printf 'Enter %s (input hidden, empty to skip): ' "$name" >&2
    "$ALGO_SECURITY_BIN" add-generic-password \
        -s "$ALGO_KEYCHAIN_SERVICE" -a "$name" \
        -T "$ALGO_SECURITY_BIN" -U -w
}

_algo_keychain_put_value() {
    # Bulk path. CAVEAT: the value passes through argv, so it is briefly
    # visible to `ps` for other processes running as this user. Fine for a
    # one-time migration on a single-user Mac; use --import for anything you
    # would rather not expose even briefly.
    local name="$1" value="$2"
    "$ALGO_SECURITY_BIN" add-generic-password \
        -s "$ALGO_KEYCHAIN_SERVICE" -a "$name" -w "$value" \
        -T "$ALGO_SECURITY_BIN" -U >/dev/null 2>&1
}

_algo_shell_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

_algo_cli_check() {
    local name state rc=0
    state=$(_algo_env_file_state)
    echo "keychain service : $ALGO_KEYCHAIN_SERVICE"
    echo "security binary  : $ALGO_SECURITY_BIN"
    echo "fallback .env    : $ALGO_SECRETS_ENV_FILE [$state$([ "$state" = irregular ] && printf ': %s' "$(_algo_env_file_kind)")]"
    if [ "$state" = "irregular" ]; then
        # Not an error on its own: the keychain is consulted first, so a pipe
        # here is simply never read. It only bites as a *fallback*, and then the
        # per-secret line below says so. Exit status tracks whether the secrets
        # are obtainable, nothing else.
        echo "  note: not a regular file — ignored while the keychain has the secrets"
    fi
    echo "secrets:"
    for name in $ALGO_SECRET_NAMES; do
        if algo_secret "$name" >/dev/null 2>&1; then
            echo "  OK      $name"
        else
            echo "  MISSING $name — $ALGO_SECRETS_ERROR"
            rc=1
        fi
    done
    return $rc
}

_algo_cli_import() {
    local name
    echo "Importing into keychain service '$ALGO_KEYCHAIN_SERVICE' (login keychain)."
    echo "Values are read by \`security\` itself — not via argv, not into history."
    echo ""
    for name in $ALGO_SECRET_NAMES; do
        _algo_keychain_put_interactive "$name" || echo "  (skipped $name)" >&2
    done
    echo ""
    echo "Done. Verify with: $0 --check"
}

_algo_cli_import_from_env() {
    local file="$1" name val imported=0
    if [ ! -f "$file" ]; then
        echo "ERROR: '$file' is not a regular file (a FIFO/pipe cannot be imported)." >&2
        return 1
    fi
    for name in $ALGO_SECRET_NAMES; do
        val=$(grep "^${name}=" "$file" 2>/dev/null | head -1 | cut -d= -f2-)
        if [ -z "$val" ]; then
            echo "  skip    $name (not in $file)"
            continue
        fi
        if _algo_keychain_put_value "$name" "$val"; then
            echo "  stored  $name"
            imported=$((imported + 1))
        else
            echo "  FAILED  $name" >&2
        fi
    done
    echo ""
    echo "$imported secret(s) stored. Verify with: $0 --check"
}

_algo_cli_export() {
    local name val rc=0
    for name in $ALGO_SECRET_NAMES; do
        if val=$(algo_secret "$name"); then
            printf 'export %s=%s\n' "$name" "$(_algo_shell_quote "$val")"
        else
            echo "# $name unavailable: $ALGO_SECRETS_ERROR" >&2
            rc=1
        fi
    done
    return $rc
}

_algo_cli_env_file() {
    # KEY=VALUE lines for `docker compose --env-file`. Unquoted: compose does
    # not do shell dequoting, so quotes would end up inside the value.
    local name val rc=0
    for name in $ALGO_SECRET_NAMES; do
        if val=$(algo_secret "$name"); then
            printf '%s=%s\n' "$name" "$val"
        else
            echo "# $name unavailable: $ALGO_SECRETS_ERROR" >&2
            rc=1
        fi
    done
    return $rc
}

# Am I being sourced, and by which shell?
#
# `${BASH_SOURCE[0]:-$0}` alone is NOT enough: zsh does not define BASH_SOURCE,
# so the test collapsed to `$0 = $0` and sourcing this from an interactive zsh
# ran the CLI instead of defining the functions. zsh also does not word-split
# unquoted parameter expansions, so `for n in $ALGO_SECRET_NAMES` there yields
# ONE bogus name ("POSTGRES_PASSWORD REDIS_PASSWORD ..."). Rather than carry two
# dialects, the sourced form is bash-only and says so.
_algo_sourced=0
if [ -n "${BASH_VERSION:-}" ]; then
    [ "${BASH_SOURCE[0]}" != "$0" ] && _algo_sourced=1
elif [ -n "${ZSH_VERSION:-}" ]; then
    # Value is colon-joined tokens like "cmdarg:file" — no trailing colon, so
    # pad both ends before matching or the final token never matches.
    case ":${ZSH_EVAL_CONTEXT:-}:" in *:file:*) _algo_sourced=1 ;; esac
fi

if [ "$_algo_sourced" = "1" ] && [ -z "${BASH_VERSION:-}" ]; then
    echo "secrets.sh: sourcing is supported from bash only (this is $(ps -o comm= -p $$ 2>/dev/null || echo 'another shell'))." >&2
    echo "  From zsh/sh use the executed form instead:  eval \"\$(deploy/launchd/secrets.sh --export)\"" >&2
    return 1 2>/dev/null || exit 1
fi

if [ "$_algo_sourced" = "0" ]; then
    case "${1:---check}" in
        --check)           _algo_cli_check ;;
        --import)          _algo_cli_import ;;
        --import-from-env) _algo_cli_import_from_env "${2:?usage: $0 --import-from-env FILE}" ;;
        --export)          _algo_cli_export ;;
        --env-file)        _algo_cli_env_file ;;
        -h|--help)
            sed -n '/^# Usage (sourced)/,/--export.*docker compose/p' "$0" | sed 's/^# \{0,1\}//'
            ;;
        *)
            echo "unknown option '$1' (try --help)" >&2
            exit 64
            ;;
    esac
    exit $?
fi
