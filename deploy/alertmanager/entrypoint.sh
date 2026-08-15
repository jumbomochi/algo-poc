#!/bin/sh
# KAN-14 (P1-11) — render Alertmanager's secrets at container start.
#
# config/alertmanager.yml is committed and secret-free; it points
# `bot_token_file` at a tmpfs path and carries a placeholder chat_id. This
# wrapper turns that template into the running config, entirely in memory:
#
#   $TELEGRAM_BOT_TOKEN -> /etc/alertmanager/secrets/telegram_token (0600)
#   $TELEGRAM_CHAT_ID   -> substituted into the rendered config
#
# Every failure below aborts with a named error rather than starting. That is
# deliberate: the 2026-08-13 outage was not caused by a missing secret, it was
# caused by an alert path that failed *quietly* (`[ -f "$ENV_FILE" ]` is false
# for a FIFO, so the guard returned success and nobody was told). An
# Alertmanager that comes up unable to deliver is exactly that failure again.
set -eu

TEMPLATE="${ALERTMANAGER_CONFIG_TEMPLATE:-/etc/alertmanager/alertmanager.template.yml}"
SECRET_DIR="${ALERTMANAGER_SECRET_DIR:-/etc/alertmanager/secrets}"
TOKEN_FILE="$SECRET_DIR/telegram_token"
RENDERED_CONFIG="$SECRET_DIR/alertmanager.yml"

fail() {
    echo "alertmanager-entrypoint: FATAL: $*" >&2
    exit 1
}

[ -f "$TEMPLATE" ] || fail "config template $TEMPLATE is missing or is not a regular file — is ./config/alertmanager.yml still mounted there?"

[ -n "${TELEGRAM_BOT_TOKEN:-}" ] || fail "TELEGRAM_BOT_TOKEN is unset or empty. Set it in .env (see .env.example). Alertmanager is the only alert path that survives a wedged notifications service; refusing to start beats coming up as a silent monitor."
[ -n "${TELEGRAM_CHAT_ID:-}" ] || fail "TELEGRAM_CHAT_ID is unset or empty. Set it in .env (see .env.example)."

# Telegram chat ids are integers (negative for groups/channels). Catch a
# pasted @handle here rather than as a 400 on the first real alert.
case "$TELEGRAM_CHAT_ID" in
    ''|*[!0-9-]*) fail "TELEGRAM_CHAT_ID must be an integer, got '$TELEGRAM_CHAT_ID'" ;;
esac

mkdir -p "$SECRET_DIR"

# umask, not chmod: the file is never world-readable even momentarily.
(umask 077; printf '%s' "$TELEGRAM_BOT_TOKEN" > "$TOKEN_FILE")
(umask 077; sed "s#^\([[:space:]]*chat_id:\)[[:space:]].*#\1 ${TELEGRAM_CHAT_ID}#" "$TEMPLATE" > "$RENDERED_CONFIG")

# Verify the substitution instead of trusting it: a template edit that renamed
# or reindented the key would otherwise leave the placeholder in place and
# page a stranger's chat forever.
placeholders=$(grep -c '^[[:space:]]*chat_id:' "$TEMPLATE" || true)
rendered=$(grep -c "^[[:space:]]*chat_id: ${TELEGRAM_CHAT_ID}\$" "$RENDERED_CONFIG" || true)
[ "$placeholders" -gt 0 ] || fail "$TEMPLATE has no chat_id: line to render"
[ "$placeholders" = "$rendered" ] || fail "chat_id substitution failed ($rendered of $placeholders lines rendered) — check the chat_id formatting in $TEMPLATE"

# KAN-15 — the dead-man's switch destination.
#
# config/alert_rules.yml's Watchdog alert fires permanently and is routed to
# the `deadman` receiver, whose committed URL is an RFC 2606 `.invalid`
# placeholder. Substitute the real check URL here so it is never in git.
#
# Unlike the Telegram credentials this is NOT fatal when unset. An
# unconfigured dead-man check is a monitoring gap; refusing to start over it
# would also take down Telegram delivery for every other alert, which is
# strictly worse. Instead the Watchdog route is repointed at the `null`
# receiver — Alertmanager will not start with a webhook URL it cannot parse,
# and a permanently-firing alert falling through to Telegram would train the
# operator to mute the bot, which is the failure this whole story exists to
# prevent.
DEADMAN_PLACEHOLDER='https://deadman.invalid/replaced-at-container-start'
if [ -n "${DEADMAN_WATCHDOG_URL:-}" ]; then
    case "$DEADMAN_WATCHDOG_URL" in
        http://*|https://*) ;;
        *) fail "DEADMAN_WATCHDOG_URL must be an http(s) URL, got '$DEADMAN_WATCHDOG_URL'" ;;
    esac
    # '#' as the sed delimiter: the URL contains '/', never '#'.
    (umask 077; sed "s#${DEADMAN_PLACEHOLDER}#${DEADMAN_WATCHDOG_URL}#" "$RENDERED_CONFIG" > "$RENDERED_CONFIG.tmp")
    mv "$RENDERED_CONFIG.tmp" "$RENDERED_CONFIG"
    if grep -q "$DEADMAN_PLACEHOLDER" "$RENDERED_CONFIG"; then
        fail "dead-man URL substitution failed — the placeholder $DEADMAN_PLACEHOLDER survived into the rendered config"
    fi
    echo "alertmanager-entrypoint: Watchdog will be pinged to the configured dead-man URL." >&2
else
    (umask 077; sed 's#^\([[:space:]]*\)receiver: deadman$#\1receiver: "null"#' "$RENDERED_CONFIG" > "$RENDERED_CONFIG.tmp")
    mv "$RENDERED_CONFIG.tmp" "$RENDERED_CONFIG"
    if grep -q '^[[:space:]]*receiver: deadman$' "$RENDERED_CONFIG"; then
        fail "could not repoint the Watchdog route away from the unconfigured deadman receiver — check the route formatting in $TEMPLATE"
    fi
    echo "alertmanager-entrypoint: WARNING: DEADMAN_WATCHDOG_URL is unset, so the Watchdog alert is being dropped. Nothing outside this host can tell you that this host stopped monitoring itself. Set it in .env (see .env.example)." >&2
fi

# ALERTMANAGER_BIN is overridable only so tests/deploy/
# test_observability_healthchecks.py can drive this script end-to-end with a
# stub; the container never sets it.
exec "${ALERTMANAGER_BIN:-/bin/alertmanager}" --config.file="$RENDERED_CONFIG" "$@"
