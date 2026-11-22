#!/bin/sh
set -e

# Render injects PORT (default 10000); the upstream image CMD hardcodes 8080.
export AISTUDIO_PORT="${AISTUDIO_PORT:-${PORT:-8080}}"
# Public host: refuse to start without AISTUDIO_WEB_PASSWORD + AISTUDIO_API_KEY.
export AISTUDIO_REQUIRE_AUTH="${AISTUDIO_REQUIRE_AUTH:-1}"

CONFIG_FILE="${AISTUDIO_CONFIG_FILE:-/app/data/config.yaml}"
TEMPLATE_FILE="/app/defaults/config.yaml"
FALLBACK_TEMPLATE="/app/config.yaml"

if [ ! -f "$CONFIG_FILE" ]; then
    if [ -f "$TEMPLATE_FILE" ]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cp "$TEMPLATE_FILE" "$CONFIG_FILE"
        echo "[entrypoint] Initialized default config.yaml at $CONFIG_FILE from $TEMPLATE_FILE"
    elif [ -f "$FALLBACK_TEMPLATE" ]; then
        mkdir -p "$(dirname "$CONFIG_FILE")"
        cp "$FALLBACK_TEMPLATE" "$CONFIG_FILE"
        echo "[entrypoint] Initialized default config.yaml at $CONFIG_FILE from $FALLBACK_TEMPLATE"
    fi
fi

# Replace the image CMD's hardcoded "--port 8080" with $AISTUDIO_PORT.
if [ "$#" -eq 0 ] || { [ "$1" = "python3" ] && [ "$2" = "main.py" ]; }; then
    set -- python3 main.py server --port "$AISTUDIO_PORT"
fi

echo "[entrypoint] Listening on 0.0.0.0:${AISTUDIO_PORT} (local waa-signer only)"
exec "$@"
