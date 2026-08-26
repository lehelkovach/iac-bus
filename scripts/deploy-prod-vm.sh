#!/usr/bin/env bash
set -euo pipefail

# Deploy current workspace to the OCI prod/edge VM (systemd iac-bus.service).
# Public hostname (DNS may lag): iac-bus.knowshowgo.com

HOST="${IAC_BUS_PROD_HOST:-}"
USER_NAME="${IAC_BUS_PROD_USER:-}"
PORT="${IAC_BUS_PROD_PORT:-22}"
APP_DIR="${IAC_BUS_PROD_APP_DIR:-/opt/iac-bus}"
RAW_KEY="${IAC_BUS_PROD_KEY:-}"
BUS_PORT="${PROD_BUS_PORT:-8101}"
BUS_LOG_LEVEL="${PROD_BUS_LOG_LEVEL:-INFO}"
BUS_API_TOKEN_VALUE="${BUS_API_TOKEN:-}"
PUBLIC_HOST="${IAC_BUS_PUBLIC_HOST:-iac-bus.knowshowgo.com}"

if [[ -z "${HOST}" || -z "${USER_NAME}" || -z "${RAW_KEY}" ]]; then
  echo "Missing IAC_BUS_PROD_HOST / IAC_BUS_PROD_USER / IAC_BUS_PROD_KEY"
  exit 2
fi

cleanup() {
  if [[ -n "${TMP_KEY_FILE:-}" && -f "${TMP_KEY_FILE}" ]]; then
    rm -f "${TMP_KEY_FILE}"
  fi
}
trap cleanup EXIT

TMP_KEY_FILE="$(mktemp)"
chmod 600 "${TMP_KEY_FILE}"
if [[ -f "${RAW_KEY}" ]]; then
  cp "${RAW_KEY}" "${TMP_KEY_FILE}"
  chmod 600 "${TMP_KEY_FILE}"
else
  printf '%b\n' "${RAW_KEY}" > "${TMP_KEY_FILE}"
  if ! ssh-keygen -y -f "${TMP_KEY_FILE}" >/dev/null 2>&1; then
    if echo "${RAW_KEY}" | base64 -d > "${TMP_KEY_FILE}" 2>/dev/null; then
      chmod 600 "${TMP_KEY_FILE}"
    else
      printf '%s\n' "${RAW_KEY}" > "${TMP_KEY_FILE}"
    fi
  fi
fi
if ! ssh-keygen -y -f "${TMP_KEY_FILE}" >/dev/null 2>&1; then
  echo "IAC_BUS_PROD_KEY could not be parsed as a valid SSH private key"
  exit 2
fi

SSH_OPTS=(-i "${TMP_KEY_FILE}" -o StrictHostKeyChecking=accept-new -p "${PORT}")
TARGET="${USER_NAME}@${HOST}"

echo "Syncing to ${TARGET}:${APP_DIR} (public host hint: ${PUBLIC_HOST})"
ssh "${SSH_OPTS[@]}" "${TARGET}" "mkdir -p '${APP_DIR}'"
tar --exclude ".git" --exclude "venv" --exclude ".venv" --exclude "__pycache__" -cf - . \
  | ssh "${SSH_OPTS[@]}" "${TARGET}" "tar -xf - -C '${APP_DIR}'"

ssh "${SSH_OPTS[@]}" "${TARGET}" \
  "sudo APP_DIR='${APP_DIR}' BUS_PORT='${BUS_PORT}' BUS_LOG_LEVEL='${BUS_LOG_LEVEL}' BUS_API_TOKEN_VALUE='${BUS_API_TOKEN_VALUE}' bash -s" <<'EOF'
set -euo pipefail
if [[ ! -d "${APP_DIR}/venv" ]]; then
  python3 -m venv "${APP_DIR}/venv"
fi
"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt"

mkdir -p /etc/iac-bus
cat > /etc/iac-bus/iac-bus.env <<ENVFILE
BUS_HOST=0.0.0.0
BUS_PORT=${BUS_PORT}
BUS_LOG_LEVEL=${BUS_LOG_LEVEL}
BUS_MAX_MESSAGES=2000
BUS_RETENTION_SECONDS=7200
BUS_QUEUE_LEASE_SECONDS=60
ENVFILE
if [[ -n "${BUS_API_TOKEN_VALUE}" ]]; then
  echo "BUS_API_TOKEN=${BUS_API_TOKEN_VALUE}" >> /etc/iac-bus/iac-bus.env
fi

install -m 0644 "${APP_DIR}/systemd/iac-bus.service" /etc/systemd/system/iac-bus.service
systemctl daemon-reload
systemctl enable iac-bus.service
systemctl restart iac-bus.service
systemctl --no-pager --full status iac-bus.service | head -20
curl -fsS "http://127.0.0.1:${BUS_PORT}/health" || true
EOF

echo "Deployed. Verify: curl http://${HOST}:${BUS_PORT}/health"
echo "When DNS is live: curl https://${PUBLIC_HOST}/health"
