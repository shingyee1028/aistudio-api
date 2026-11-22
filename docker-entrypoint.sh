#!/bin/sh
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Render injects PORT (default 10000); locally default to 8080.
PUBLIC_PORT="${PORT:-8080}"
if [ -z "${AISTUDIO_PORT:-}" ]; then
    if [ "$PUBLIC_PORT" = "8080" ]; then
        AISTUDIO_PORT="18080"
    else
        AISTUDIO_PORT="8080"
    fi
fi
export PUBLIC_PORT AISTUDIO_PORT
export OAI_BRIDGE_HOST="${OAI_BRIDGE_HOST:-127.0.0.1}"
export OAI_BRIDGE_PORT="${OAI_BRIDGE_PORT:-8765}"
export OAI_BRIDGE_UPSTREAM="${OAI_BRIDGE_UPSTREAM:-http://127.0.0.1:${AISTUDIO_PORT}}"

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

export PYTHONPATH="${SCRIPT_DIR}/src${PYTHONPATH:+:$PYTHONPATH}"

# Replace the image CMD's hardcoded "python3 main.py server --port 8080" with
# the sidecar launcher (aistudio-api + oai-bridge + public path router).
if [ "$#" -eq 0 ] || { [ "$1" = "python3" ] && [ "$2" = "main.py" ]; }; then
    set -- python3 -m aistudio_api.sidecar.launcher
fi

echo "[entrypoint] Router 0.0.0.0:${PUBLIC_PORT} -> /v1/* to ${OAI_BRIDGE_HOST}:${OAI_BRIDGE_PORT}, else 127.0.0.1:${AISTUDIO_PORT}"
exec "$@"
