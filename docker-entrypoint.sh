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

if [ -n "${CLI_PROXY_OUTBOUND_PROXY:-}" ]; then
  case "${CLI_PROXY_OUTBOUND_PROXY}" in
    http://*|https://*|socks5://*|socks5h://*) ;;
    *) echo "CLI_PROXY_OUTBOUND_PROXY must use http, https, socks5 or socks5h" >&2; exit 64 ;;
  esac
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

if [ -n "${CLI_PROXY_AUTH_JSON_B64:-}" ] && \
   { [ -n "${UPSTREAM_BASE_URL:-}" ] || [ -n "${UPSTREAM_API_KEY:-}" ] || [ -n "${UPSTREAM_MODEL:-}" ]; }; then
  echo "Choose either CLI_PROXY_AUTH_JSON_B64 or UPSTREAM_*; do not configure both" >&2
  exit 64
fi

mkdir -p "${DATA_DIR}/auth"

# OAuth credentials are injected as a base64-encoded JSON secret at deploy time.
# Never print the value: container logs must not contain access or refresh tokens.
if [ -n "${CLI_PROXY_AUTH_JSON_B64:-}" ]; then
  auth_file="${DATA_DIR}/auth/codex-oauth.json"
  auth_version="${CLI_PROXY_AUTH_VERSION:-initial}"
  auth_version_file="${DATA_DIR}/auth/.codex-oauth-version"
  install_auth=false
  if [ ! -s "${auth_file}" ]; then
    install_auth=true
  elif [ "$(cat "${auth_version_file}" 2>/dev/null || true)" != "${auth_version}" ]; then
    install_auth=true
  fi
  if [ "${install_auth}" = true ]; then
    auth_tmp="${auth_file}.tmp"
    if ! printf '%s' "${CLI_PROXY_AUTH_JSON_B64}" | base64 -d > "${auth_tmp}"; then
      echo "CLI_PROXY_AUTH_JSON_B64 is not valid base64" >&2
      rm -f "${auth_tmp}"
      exit 64
    fi
    if ! grep -q '"type"[[:space:]]*:[[:space:]]*"codex"' "${auth_tmp}" || \
       ! grep -q '"refresh_token"[[:space:]]*:[[:space:]]*"' "${auth_tmp}"; then
      echo "CLI_PROXY_AUTH_JSON_B64 must contain a Codex OAuth JSON file" >&2
      rm -f "${auth_tmp}"
      exit 64
    fi
    chmod 0600 "${auth_tmp}"
    mv -f "${auth_tmp}" "${auth_file}"
    printf '%s' "${auth_version}" > "${auth_version_file}"
    chmod 0600 "${auth_version_file}"
  fi
fi

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

if [ -n "${CLI_PROXY_OUTBOUND_PROXY:-}" ]; then
  cat >> "${CONFIG_FILE}" <<EOF
proxy-url: "$(yaml_quote "${CLI_PROXY_OUTBOUND_PROXY}")"
EOF
fi

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
