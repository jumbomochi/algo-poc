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
#   deploy/launchd/secrets.sh --import             # interactive: every known name
#   deploy/launchd/secrets.sh --import --only NAME # just NAME (repeatable)
#     typed twice, echo off; empty = skip; existing item only on an explicit y;
#     Ctrl-C aborts with nothing written; value never in argv (KAN-115)
#   deploy/launchd/secrets.sh --import-from-env F  # bulk (see caveat below)
#   eval "$(deploy/launchd/secrets.sh --export)"   # for docker compose / shells

ALGO_KEYCHAIN_SERVICE="${ALGO_KEYCHAIN_SERVICE:-algo-poc}"
# Default: the .env of the tree THIS file lives in, so the CLI modes run in the
# deploy clone (KAN-72) check the clone, not whichever checkout was hardcoded.
ALGO_SECRETS_ENV_FILE="${ALGO_SECRETS_ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/.env}"

# Absolute path by default so a hijacked PATH cannot substitute the binary that
# reads our secrets. Overridable only so the test suite can stub it.
ALGO_SECURITY_BIN="${ALGO_SECURITY_BIN:-/usr/bin/security}"

# Same rationale, and overridable for the same single reason: without it the
# test suite raises real desktop notifications on the developer's machine every
# time it exercises a wrapper's failure path.
ALGO_OSASCRIPT_BIN="${ALGO_OSASCRIPT_BIN:-/usr/bin/osascript}"

# Every secret the stack needs. Order is the order --import prompts in.
# Overridable so an import/check can be scoped to a subset, e.g.
#   ALGO_SECRET_NAMES="POSTGRES_PASSWORD REDIS_PASSWORD" ... --import-from-env F
ALGO_SECRET_NAMES="${ALGO_SECRET_NAMES:-POSTGRES_PASSWORD REDIS_PASSWORD TELEGRAM_BOT_TOKEN TELEGRAM_CHAT_ID API_KEYS}"

# KAN-15: secrets the stack runs WITHOUT, but is less observable without.
# Kept out of $ALGO_SECRET_NAMES on purpose — --check/--export must not start
# reporting failure on a host that simply has not configured a dead-man switch
# yet, or the exit status stops meaning "the stack cannot authenticate".
# --import still prompts for these, and --export/--env-file still emit them
# when present (docker compose needs DEADMAN_WATCHDOG_URL for the
# alertmanager container).
#
# One name per scheduled job rather than one shared URL: an external checker
# pages on a *missing* ping, so a shared check would go on looking healthy for
# as long as any single job kept running. The whole point is to identify which
# job stopped.
#
#   DEADMAN_WATCHDOG_URL        — Alertmanager pings this every 5m (the
#                                 Watchdog alert in config/alert_rules.yml)
#   ALGO_DEADMAN_PAPER_URL      — run_paper.sh, on a successful run
#   ALGO_DEADMAN_DIVERGENCE_URL — run_divergence.sh, on a run that reached a
#                                 verdict (KAN-56)
#   ALGO_DEADMAN_REFRESH_URL    — run_backtest_refresh.sh, on success (KAN-56)
#   ALGO_DEADMAN_BACKUP_URL     — run_db_backup.sh, on a verified dump (KAN-56)
#   ALGO_DEADMAN_DIGEST_URL     — scripts/ops/evidence_digest.py, on a
#                                 *delivered* digest (KAN-29)
#   ALGO_DEADMAN_EARNINGS_URL   — run_earnings_refresh.sh, when earnings.json's
#                                 fetched_at was advanced (KAN-110)
ALGO_OPTIONAL_SECRET_NAMES="${ALGO_OPTIONAL_SECRET_NAMES:-DEADMAN_WATCHDOG_URL ALGO_DEADMAN_PAPER_URL ALGO_DEADMAN_DIVERGENCE_URL ALGO_DEADMAN_REFRESH_URL ALGO_DEADMAN_BACKUP_URL ALGO_DEADMAN_DIGEST_URL ALGO_DEADMAN_EARNINGS_URL}"

# KAN-110: credentials ONE job needs, not the stack. The earnings refresh
# cannot run without ALPHAVANTAGE_API_KEY (source of truth: 1Password
# op://Personal/AlphaVantage/credential), but nothing else reads it, so:
#   * --check reports it under its own heading and does NOT exit non-zero on
#     its absence — that status keeps meaning "the stack cannot authenticate";
#     the job itself aborts loudly (Telegram + ALERTS.log) without it;
#   * --import prompts for it;
#   * --export / --env-file do NOT emit it: docker compose has no use for a
#     network credential, and scripts/fetch_earnings.py reads the keychain
#     itself when run by hand.
ALGO_JOB_SECRET_NAMES="${ALGO_JOB_SECRET_NAMES:-ALPHAVANTAGE_API_KEY}"

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
            ALGO_SECRETS_ERROR="keychain service '$ALGO_KEYCHAIN_SERVICE' has no item for '$name'. Import it with: deploy/launchd/secrets.sh --import --only $name"
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
    "$ALGO_OSASCRIPT_BIN" -e 'on run argv
        display notification (item 1 of argv) with title "algo-poc job failed"
    end run' "$msg" >/dev/null 2>&1 || true
    return 0
}

# ---------------------------------------------------------------------------
# CLI (only when executed directly, never when sourced)
# ---------------------------------------------------------------------------

# --- interactive import (KAN-115) -------------------------------------------
#
# INCIDENT 2026-10-09 ~16:16 SGT. To add ONE dead-man URL the operator ran
# --import. It walked every known name, printed "Enter POSTGRES_PASSWORD (input
# hidden, empty to skip)" and then ran `security add-generic-password ... -U -w`
# with no value, so `security` prompted for the secret itself. There was no
# skip: pressing Enter stored an EMPTY POSTGRES_PASSWORD over the real one (-U
# updates in place), Ctrl-C did nothing because the terminal was in password
# mode, and every job that reads it would have failed at its next run. On top
# of that, security's own interactive -w prompt reads into a 128-byte buffer
# and silently truncates longer values (e.g. ~192-char API tokens).
#
# So the value is now read by bash, and `security` is only ever handed a
# complete, confirmed value:
#   * read twice from /dev/tty with echo off, and refused unless both match;
#   * an empty entry is a real skip — nothing is called for that name;
#   * an existing item is never replaced without an explicit "y";
#   * every answer is collected FIRST and written only after the last prompt,
#     so Ctrl-C (or Ctrl-D) at any prompt aborts with nothing written;
#   * each write is read back and its length checked.
#
# WRITE PATH: `security -i` reads `add-generic-password ... -X <hex>` from a
# pipe fed by the `printf` builtin. Nothing secret is ever in a process's argv
# (unlike `-w VALUE`, see _algo_keychain_put_value), and hex sidesteps the
# quoting rules of security's interactive tokenizer, so any byte sequence
# round-trips. Verified against the real binary with a 209-byte value of
# quotes, $, backticks, backslashes and spaces: stored byte-for-byte.
#
# Two properties of `security -i` that this code depends on:
#   * it splits an input line somewhere past ~4 KB and runs the tail as a
#     separate command, whose "unknown command" error ECHOES the tail. Values
#     are therefore capped at $ALGO_IMPORT_MAX_BYTES (hex doubles it, still far
#     below the split) and any captured stderr is redacted before printing;
#   * its exit status is that of the last command it ran, so a failed add is
#     visible as a non-zero exit.

# NOTHING in this block runs at source time. Every launchd job sources this
# file, so the import path only DEFINES functions here; its state
# (_ALGO_TTY_FD, _ALGO_TTY_SAVED, the byte cap, ...) is set inside
# _algo_cli_import. A test pins that sourcing adds no new variables.
#
# XTRACE: `bash -x secrets.sh --import`, or an exported SHELLOPTS=xtrace /
# BASH_XTRACEFD, would trace every assignment and command line holding the
# value or its hex to stderr. The import path switches tracing off on entry
# (and the helpers that touch a value do so again, in case they are ever
# called from elsewhere). It is never switched back on: the CLI exits after.

# The value cap, in bytes. Ample for every secret here (passwords, ~192-char
# tokens, ping URLs), and about what a terminal accepts anyway: macOS's
# canonical-mode line limit (MAX_CANON) is 1024 bytes. $ALGO_IMPORT_MAX_BYTES
# may LOWER it (the test suite does); anything non-numeric or larger is 1024.
_algo_import_max_bytes() {
    case "${ALGO_IMPORT_MAX_BYTES:-}" in
        ''|*[!0-9]*) printf '1024' ;;
        *) if [ "$ALGO_IMPORT_MAX_BYTES" -gt 1024 ]; then printf '1024'; else printf '%s' "$ALGO_IMPORT_MAX_BYTES"; fi ;;
    esac
}

# Length of a value in BYTES (the unit of the cap), whatever the locale.
_algo_byte_len() {
    { set +x; } 2>/dev/null
    printf '%s' "$1" | wc -c | tr -d ' '
}

# Every name --import knows about, in prompt order.
_algo_import_known_names() {
    printf '%s\n' $ALGO_SECRET_NAMES $ALGO_OPTIONAL_SECRET_NAMES $ALGO_JOB_SECRET_NAMES
}

_algo_is_known_secret_name() {
    local want="$1" n
    for n in $(_algo_import_known_names); do
        [ "$n" = "$want" ] && return 0
    done
    return 1
}

# Strip anything that could be (part of) the value from security's stderr
# before it is shown: the exact hex, and any long hex run in case a split line
# echoed a fragment of it.
_algo_redact_security_err() {
    local err="$1" hex="$2"
    [ -n "$hex" ] && err="${err//$hex/<redacted>}"
    printf '%s' "$err" | sed -E 's/[0-9A-Fa-f]{16,}/<redacted>/g' | tr '\n' ' ' | sed 's/ *$//'
}

# _algo_keychain_put_stdin NAME VALUE -> 0 on success; on failure 1 with a
# redacted reason in $_ALGO_PUT_ERROR. The value never reaches argv.
_algo_keychain_put_stdin() {
    { set +x; } 2>/dev/null
    local name="$1" value="$2" hex err rc
    _ALGO_PUT_ERROR=""
    hex=$(printf '%s' "$value" | od -An -v -tx1 | tr -d ' \n')
    err=$(printf 'add-generic-password -s %s -a %s -T %s -U -X %s\n' \
              "$ALGO_KEYCHAIN_SERVICE" "$name" "$ALGO_SECURITY_BIN" "$hex" \
          | "$ALGO_SECURITY_BIN" -i 2>&1 >/dev/null)
    rc=$?
    if [ "$rc" -ne 0 ]; then
        _ALGO_PUT_ERROR="security exited $rc: $(_algo_redact_security_err "$err" "$hex")"
        return 1
    fi
    return 0
}

# _algo_keychain_item_info NAME -> sets _ALGO_ITEM_STATE to present|absent|error,
# and for a present item _ALGO_ITEM_LEN (bytes, or "?") and _ALGO_ITEM_MDAT.
# The value is read only to measure it; it is never printed. A locked keychain
# is classified exactly as _algo_secret_from_keychain does, so --import refuses
# it before the first prompt instead of discovering it at the write.
_algo_keychain_item_info() {
    { set +x; } 2>/dev/null
    local name="$1" out rc v stamp
    _ALGO_ITEM_STATE="" _ALGO_ITEM_LEN="?" _ALGO_ITEM_MDAT="unknown date" _ALGO_ITEM_ERROR=""
    # Without -w/-g, find-generic-password prints attributes only.
    out=$("$ALGO_SECURITY_BIN" find-generic-password -s "$ALGO_KEYCHAIN_SERVICE" -a "$name" 2>&1)
    rc=$?
    if [ "$rc" -ne 0 ]; then
        case "$out" in
            *"could not be found"*|*"-25300"*) _ALGO_ITEM_STATE="absent"; return 0 ;;
        esac
        _ALGO_ITEM_STATE="error"
        case "$out" in
            *"interaction is not allowed"*|*"-25308"*)
                _ALGO_ITEM_ERROR="login keychain is LOCKED. Unlock it (e.g. security unlock-keychain ~/Library/Keychains/login.keychain-db) and re-run."
                ;;
            *)
                _ALGO_ITEM_ERROR="$(printf '%s' "$out" | tr '\n' ' ' | sed 's/ *$//')"
                ;;
        esac
        return 1
    fi
    _ALGO_ITEM_STATE="present"
    # "mdat"<timedate>=0x3230...  "20261009081616Z\000"
    stamp=$(printf '%s\n' "$out" | sed -n 's/.*"mdat"<timedate>=[^"]*"\([0-9]\{14\}\)Z.*/\1/p' | head -1)
    if [ -n "$stamp" ]; then
        _ALGO_ITEM_MDAT="${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:8:2}:${stamp:10:2}:${stamp:12:2} UTC"
    fi
    if v=$("$ALGO_SECURITY_BIN" find-generic-password -w -s "$ALGO_KEYCHAIN_SERVICE" -a "$name" 2>/dev/null); then
        _ALGO_ITEM_LEN=$(_algo_byte_len "$v")
    fi
    v=""
    return 0
}

# Prompt on the terminal and read one hidden line into $_ALGO_TTY_INPUT.
# Echo is switched off BEFORE the prompt is shown so type-ahead is not echoed
# either. Returns non-zero on EOF (Ctrl-D) or a read error.
_algo_tty_read_secret() {
    { set +x; } 2>/dev/null
    local prompt="$1" rc
    _ALGO_TTY_INPUT=""
    stty -echo <&"$_ALGO_TTY_FD" 2>/dev/null
    printf '%s' "$prompt" >&"$_ALGO_TTY_FD"
    IFS= read -rs -u "$_ALGO_TTY_FD" _ALGO_TTY_INPUT
    rc=$?
    [ -n "$_ALGO_TTY_SAVED" ] && stty "$_ALGO_TTY_SAVED" <&"$_ALGO_TTY_FD" 2>/dev/null
    printf '\n' >&"$_ALGO_TTY_FD"
    return $rc
}

# Visible y/N answer into $_ALGO_TTY_INPUT; non-zero on EOF.
_algo_tty_read_line() {
    _ALGO_TTY_INPUT=""
    printf '%s' "$1" >&"$_ALGO_TTY_FD"
    IFS= read -r -u "$_ALGO_TTY_FD" _ALGO_TTY_INPUT
}

# The EXIT trap is what puts the terminal back. It must not be the INT trap:
# when `exit` runs from inside an interrupted `read -s`, bash's own unwind
# restores the settings `read -s` saved — which are our echo-off ones — AFTER
# an INT handler has run, leaving the operator's terminal silent. The EXIT trap
# runs after that unwind.
_algo_import_restore_tty() {
    if [ -n "$_ALGO_TTY_SAVED" ]; then
        stty "$_ALGO_TTY_SAVED" <&"$_ALGO_TTY_FD" 2>/dev/null
    fi
}

_algo_import_interrupted() {
    printf '\n' >&"$_ALGO_TTY_FD" 2>/dev/null
    if [ "${_ALGO_IMPORT_PHASE:-}" = "write" ]; then
        echo "Interrupted while writing — the lines above say which secrets were stored; one more may have been stored just before the interrupt — run: $0 --check" >&2
    else
        echo "Interrupted — import aborted, nothing was written to the keychain." >&2
    fi
    exit 130
}

_algo_keychain_put_value() {
    # Bulk path. CAVEAT: the value passes through argv, so it is briefly
    # visible to `ps` for other processes running as this user. Fine for a
    # one-time migration on a single-user Mac; use --import (which feeds
    # `security -i` through a pipe, see _algo_keychain_put_stdin) for anything
    # you would rather not expose even briefly.
    local name="$1" value="$2"
    "$ALGO_SECURITY_BIN" add-generic-password \
        -s "$ALGO_KEYCHAIN_SERVICE" -a "$name" -w "$value" \
        -T "$ALGO_SECURITY_BIN" -U >/dev/null 2>&1
}

_algo_shell_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# ---------------------------------------------------------------------------
# Shape validation (KAN-84)
# ---------------------------------------------------------------------------
# `--check` used to answer "is this retrievable", which is not the question it
# gets asked. On 2026-09-12 a 192-character JIRA API token was pasted into
# API_KEYS and --check reported "OK". services/api/auth.py parses that value at
# import time and raises on a malformed one, so the api service — which carries
# the kill switch behind require_role("admin") — would have refused to start,
# crash-looping under `restart: unless-stopped`, first noticed at the next
# `docker compose up`.
#
# A check consulted precisely when someone is worried must not answer a
# different question confidently. So --check now reports three states: OK,
# MISSING, and MALFORMED.
#
# NO VALIDATOR PRINTS A VALUE, in any branch. --check is run in terminals and
# pasted into tickets; "got: <secret>" would turn a shape report into a leak.
# Names of roles and expected formats are not secret; the values are.
#
# A name with no validator keeps reporting OK on presence. The point is to add
# signal, not to make --check fail on anything it does not recognise.

# Echo a reason if $2 is not a usable value for secret $1, nothing if it is.
_algo_secret_shape_error() {
    local name="$1" value="$2"
    case "$name" in
        API_KEYS)
            # key:role[,key:role], role in admin|operator|viewer — the format
            # services/api/auth.py:_load_api_keys accepts.
            local entry key role seen=0
            local IFS=','
            for entry in $value; do
                entry="${entry#"${entry%%[![:space:]]*}"}"   # ltrim
                entry="${entry%"${entry##*[![:space:]]}"}"   # rtrim
                [ -z "$entry" ] && continue
                seen=$((seen + 1))
                key="${entry%%:*}"
                role="${entry#*:}"
                case "$entry" in
                    *:*) ;;
                    *) printf 'expected key:role[,key:role]'; return 0 ;;
                esac
                [ -z "$key" ] && { printf 'an entry has an empty key'; return 0; }
                # Checked BEFORE the role whitelist: a missing comma merges two
                # entries, leaving a second colon inside the role, and the
                # whitelist would then report "role must be admin|operator|viewer"
                # for a value whose real problem is the missing separator.
                case "$role" in
                    *:*) printf 'an entry contains two colons (missing comma?)'; return 0 ;;
                esac
                case "$role" in
                    admin|operator|viewer) ;;
                    *) printf 'role must be admin|operator|viewer'; return 0 ;;
                esac
            done
            [ "$seen" -eq 0 ] && { printf 'no entries'; return 0; }
            ;;
        TELEGRAM_CHAT_ID)
            # Integer, negative for a group chat. A bad id makes every alert
            # fail to deliver, silently.
            case "$value" in
                ''|*[!0-9-]*|*-*-*) printf 'expected an integer chat id'; return 0 ;;
                -) printf 'expected an integer chat id'; return 0 ;;
                -*) case "${value#-}" in ''|*[!0-9]*) printf 'expected an integer chat id'; return 0 ;; esac ;;
            esac
            ;;
    esac
    return 0
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
    local value why
    for name in $ALGO_SECRET_NAMES; do
        if value=$(algo_secret "$name" 2>/dev/null); then
            why=$(_algo_secret_shape_error "$name" "$value")
            if [ -n "$why" ]; then
                # Retrievable but unusable. Non-zero like MISSING: --check is a
                # gate, and this is not a passing state.
                echo "  MALFORMED $name — $why"
                rc=1
            else
                echo "  OK      $name"
            fi
        else
            echo "  MISSING $name — $ALGO_SECRETS_ERROR"
            rc=1
        fi
    done
    unset value why
    # Optional: reported, never fatal. See $ALGO_OPTIONAL_SECRET_NAMES.
    if [ -n "$ALGO_OPTIONAL_SECRET_NAMES" ]; then
        echo "dead-man switches (optional, but nothing external watches this host without them):"
        for name in $ALGO_OPTIONAL_SECRET_NAMES; do
            if algo_secret "$name" >/dev/null 2>&1; then
                echo "  OK      $name"
            else
                echo "  ABSENT  $name — not configured; import it with: $0 --import --only $name"
            fi
        done
    fi
    # Per-job credentials: reported, never fatal. See $ALGO_JOB_SECRET_NAMES.
    if [ -n "$ALGO_JOB_SECRET_NAMES" ]; then
        echo "job credentials (a missing one stops only its own job, which alerts):"
        for name in $ALGO_JOB_SECRET_NAMES; do
            if algo_secret "$name" >/dev/null 2>&1; then
                echo "  OK      $name"
            else
                echo "  ABSENT  $name — its job aborts until imported: $0 --import --only $name"
            fi
        done
    fi
    return $rc
}

# Read NAME back and compare with VALUE without either reaching a trace or
# stdout. Sets _ALGO_READBACK_BYTES; returns 0 only on an exact match.
_algo_keychain_read_back() {
    { set +x; } 2>/dev/null
    local name="$1" value="$2" got
    _ALGO_READBACK_BYTES=0
    got=$("$ALGO_SECURITY_BIN" find-generic-password -w -s "$ALGO_KEYCHAIN_SERVICE" -a "$name" 2>/dev/null) || got=""
    _ALGO_READBACK_BYTES=$(_algo_byte_len "$got")
    if [ "$got" = "$value" ]; then got=""; return 0; fi
    got=""
    return 1
}

# --import [--only NAME]...  — see the KAN-115 block above _algo_keychain_put_stdin.
_algo_cli_import() {
    # First, before any value exists: no xtrace (see the XTRACE note above).
    { set +x +v; } 2>/dev/null
    unset BASH_XTRACEFD
    local name only="" names n dup rc=0 bytes first i max_bytes
    local pending_names=() pending_values=()
    local states=() lens=() mdats=()
    # CLI-only state; deliberately not set at source time.
    # Fixed fd for the controlling terminal: /bin/bash on macOS is 3.2, which
    # has no `exec {fd}<>` allocation.
    _ALGO_TTY_FD=9
    _ALGO_TTY_SAVED=""
    _ALGO_IMPORT_PHASE="setup"
    max_bytes=$(_algo_import_max_bytes)

    while [ $# -gt 0 ]; do
        case "$1" in
            --only)
                if [ -z "${2:-}" ]; then
                    echo "usage: $0 --import [--only NAME]..." >&2
                    return 64
                fi
                only="$only $2"
                shift 2
                ;;
            *)
                echo "--import: unknown argument '$1' (usage: $0 --import [--only NAME]...)" >&2
                return 64
                ;;
        esac
    done

    if [ -n "$only" ]; then
        names=""
        for n in $only; do
            if ! _algo_is_known_secret_name "$n"; then
                echo "--import --only: '$n' is not a secret this stack knows about; refusing." >&2
                echo "  known: $(_algo_import_known_names | tr '\n' ' ')" >&2
                return 64
            fi
            dup=0
            for name in $names; do [ "$name" = "$n" ] && dup=1; done
            [ "$dup" = 0 ] && names="$names $n"
        done
    else
        names="$(_algo_import_known_names | tr '\n' ' ')"
    fi

    # security -i tokenizes on whitespace; these two go into its command line
    # unquoted. Neither has a reason to contain any of these characters.
    case "$ALGO_KEYCHAIN_SERVICE$ALGO_SECURITY_BIN" in
        *[[:space:]\"\'\\]*)
            echo "--import: keychain service or security binary path contains whitespace or quotes; refusing." >&2
            return 1
            ;;
    esac

    if ! { exec 9<>/dev/tty; } 2>/dev/null; then
        echo "--import reads secrets from a terminal and there is none. For a file use --import-from-env FILE." >&2
        return 1
    fi
    _ALGO_TTY_SAVED=$(stty -g <&"$_ALGO_TTY_FD" 2>/dev/null)
    _ALGO_IMPORT_PHASE="prompt"
    trap _algo_import_restore_tty EXIT
    trap _algo_import_interrupted INT TERM

    echo "Importing into keychain service '$ALGO_KEYCHAIN_SERVICE' (login keychain)."
    echo "Each value is typed twice with echo off. Empty skips, Ctrl-C aborts — nothing"
    echo "is written until every prompt has been answered."
    echo "Note: a macOS keychain dialog may appear — approve it, or Ctrl-C to abort."
    echo ""

    # Phase 0: look at every item BEFORE the first prompt, so a locked keychain
    # (or any other lookup failure) is refused up front, not found at the write.
    i=0
    for name in $names; do
        if ! _algo_keychain_item_info "$name"; then
            echo "  cannot tell whether $name is already set: $_ALGO_ITEM_ERROR" >&2
            echo "Import aborted, nothing was written." >&2
            return 1
        fi
        states[$i]="$_ALGO_ITEM_STATE"
        lens[$i]="$_ALGO_ITEM_LEN"
        mdats[$i]="$_ALGO_ITEM_MDAT"
        i=$((i + 1))
    done

    # Phase 1: collect. Nothing below writes.
    i=-1
    for name in $names; do
        i=$((i + 1))
        if [ "${states[$i]}" = "present" ]; then
            if ! _algo_tty_read_line "$name already set (${lens[$i]} bytes, modified ${mdats[$i]}); overwrite? [y/N] "; then
                printf '\n' >&"$_ALGO_TTY_FD"
                echo "Input closed — import aborted, nothing was written." >&2
                return 1
            fi
            case "$_ALGO_TTY_INPUT" in
                y|Y|yes|YES|Yes) ;;
                *) echo "  kept    $name (unchanged)"; continue ;;
            esac
        fi
        if ! _algo_tty_read_secret "Enter $name (input hidden, empty to skip): "; then
            echo "Input closed — import aborted, nothing was written." >&2
            return 1
        fi
        if [ -z "$_ALGO_TTY_INPUT" ]; then
            echo "  skipped $name"
            continue
        fi
        first="$_ALGO_TTY_INPUT"
        bytes=$(_algo_byte_len "$first")
        if [ "$bytes" -gt "$max_bytes" ]; then
            echo "  REFUSED $name — $bytes bytes is over the $max_bytes-byte limit; not written"
            first=""
            rc=1
            continue
        fi
        if ! _algo_tty_read_secret "Re-enter $name to confirm: "; then
            first=""
            echo "Input closed — import aborted, nothing was written." >&2
            return 1
        fi
        if [ "$_ALGO_TTY_INPUT" != "$first" ]; then
            echo "  REFUSED $name — the two entries did not match; not written"
            first=""
            _ALGO_TTY_INPUT=""
            rc=1
            continue
        fi
        pending_names[${#pending_names[@]}]="$name"
        pending_values[${#pending_values[@]}]="$first"
        first=""
        _ALGO_TTY_INPUT=""
    done

    # Phase 2: write what was confirmed, then read each one back.
    _ALGO_IMPORT_PHASE="write"
    echo ""
    if [ "${#pending_names[@]}" -eq 0 ]; then
        echo "Nothing to write."
    fi
    i=0
    while [ "$i" -lt "${#pending_names[@]}" ]; do
        name="${pending_names[$i]}"
        bytes=$(_algo_byte_len "${pending_values[$i]}")
        if _algo_keychain_put_stdin "$name" "${pending_values[$i]}"; then
            if _algo_keychain_read_back "$name" "${pending_values[$i]}"; then
                echo "  stored  $name ($bytes bytes, read back intact)"
            else
                echo "  WARNING $name was written but does not read back as entered (wrote $bytes bytes, read $_ALGO_READBACK_BYTES). Re-run: $0 --import --only $name"
                rc=1
            fi
        else
            echo "  FAILED  $name — $_ALGO_PUT_ERROR"
            rc=1
        fi
        pending_values[$i]=""
        i=$((i + 1))
    done
    unset pending_values

    trap - INT TERM
    _algo_import_restore_tty
    trap - EXIT
    exec 9<&-
    echo ""
    echo "Done. Verify with: $0 --check"
    return $rc
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
    # Optional names are emitted when present and skipped silently otherwise —
    # an unconfigured dead-man switch must not make `eval "$(... --export)"`
    # return non-zero under `set -e`.
    for name in $ALGO_OPTIONAL_SECRET_NAMES; do
        if val=$(algo_secret "$name"); then
            printf 'export %s=%s\n' "$name" "$(_algo_shell_quote "$val")"
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
    # See _algo_cli_export: present-only, never fatal. docker compose reads
    # DEADMAN_WATCHDOG_URL from here for the alertmanager container.
    for name in $ALGO_OPTIONAL_SECRET_NAMES; do
        if val=$(algo_secret "$name"); then
            printf '%s=%s\n' "$name" "$val"
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
        --import)          shift; _algo_cli_import "$@" ;;
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
