#!/bin/sh
set -eu

PORT="${PORT:-8080}"
DATA_DIR="${CLI_PROXY_DATA_DIR:-/data}"
CONFIG_FILE="${DATA_DIR}/config.yaml"

if ! printf '%s' "${PORT}" | grep -Eq '^[0-9]+$'; then
  echo "PORT must be a number" >&2
  exit 64
fi

if [ -z "${CLI_PROXY_API_KEY:-}" ]; then
  echo "CLI_PROXY_API_KEY is required" >&2
  exit 64
fi

if [ -n "${UPSTREAM_BASE_URL:-}" ] || [ -n "${UPSTREAM_API_KEY:-}" ] || [ -n "${UPSTREAM_MODEL:-}" ]; then
  if [ -z "${UPSTREAM_BASE_URL:-}" ] || [ -z "${UPSTREAM_API_KEY:-}" ] || [ -z "${UPSTREAM_MODEL:-}" ]; then
    echo "UPSTREAM_BASE_URL, UPSTREAM_API_KEY and UPSTREAM_MODEL must be set together" >&2
    exit 64
  fi
  case "${UPSTREAM_BASE_URL}" in
    https://*) ;;
    *) echo "UPSTREAM_BASE_URL must use HTTPS" >&2; exit 64 ;;
  esac
fi

mkdir -p "${DATA_DIR}/auth"

yaml_quote() {
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

cat > "${CONFIG_FILE}" <<EOF
host: "0.0.0.0"
port: ${PORT}
tls:
  enable: false
  cert: ""
  key: ""
remote-management:
  allow-remote: false
  secret-key: ""
  disable-control-panel: true
auth-dir: "${DATA_DIR}/auth"
api-keys:
  - "$(yaml_quote "${CLI_PROXY_API_KEY}")"
debug: false
EOF

if [ -n "${UPSTREAM_BASE_URL:-}" ]; then
  alias_name="${UPSTREAM_MODEL_ALIAS:-${UPSTREAM_MODEL}}"
  cat >> "${CONFIG_FILE}" <<EOF
openai-compatibility:
  - name: "authorized-upstream"
    base-url: "$(yaml_quote "${UPSTREAM_BASE_URL}")"
    api-key-entries:
      - api-key: "$(yaml_quote "${UPSTREAM_API_KEY}")"
    models:
      - name: "$(yaml_quote "${UPSTREAM_MODEL}")"
        alias: "$(yaml_quote "${alias_name}")"
        input-modalities: [text, image]
        output-modalities: [text]
EOF
fi

exec /usr/local/bin/cli-proxy-api -config "${CONFIG_FILE}" -local-model
