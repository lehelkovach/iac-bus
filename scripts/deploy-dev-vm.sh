#!/usr/bin/env bash
set -euo pipefail

# Deploy current workspace contents to an Oracle Cloud dev VM and run the
# hot-reload development service.

HOST="${KSG_DEV_VM_HOST:-}"
USER_NAME="${KSG_DEV_VM_USER:-}"
PORT="${KSG_DEV_VM_PORT:-22}"
APP_DIR="${KSG_DEV_VM_APP_DIR:-/opt/iac-bus-dev}"
RAW_KEY="${KSG_DEV_VM_KEY:-}"
BUS_PORT="${DEV_BUS_PORT:-8091}"
BUS_LOG_LEVEL="${DEV_BUS_LOG_LEVEL:-DEBUG}"
BUS_API_TOKEN_VALUE="${BUS_API_TOKEN:-}"

if [[ -z "${HOST}" || -z "${USER_NAME}" || -z "${RAW_KEY}" ]]; then
  echo "Missing required KSG dev VM secrets: KSG_DEV_VM_HOST/KSG_DEV_VM_USER/KSG_DEV_VM_KEY"
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
  # Support secrets injected as:
  # - raw PEM/OpenSSH key content
  # - escaped newlines (\n)
  # - base64-encoded private key content
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
  echo "KSG_DEV_VM_KEY could not be parsed as a valid SSH private key"
  exit 2
fi

SSH_OPTS=(-i "${TMP_KEY_FILE}" -o StrictHostKeyChecking=accept-new -p "${PORT}")
TARGET="${USER_NAME}@${HOST}"

echo "Syncing repository to ${TARGET}:${APP_DIR}"
ssh "${SSH_OPTS[@]}" "${TARGET}" "mkdir -p '${APP_DIR}'"
tar \
  --exclude ".git" \
  --exclude "venv" \
  --exclude ".venv" \
  --exclude "__pycache__" \
  -cf - . | ssh "${SSH_OPTS[@]}" "${TARGET}" "tar -xf - -C '${APP_DIR}'"

echo "Configuring and restarting iac-bus-dev service on ${TARGET}"
ssh "${SSH_OPTS[@]}" "${TARGET}" \
  "sudo APP_DIR='${APP_DIR}' BUS_PORT='${BUS_PORT}' BUS_LOG_LEVEL='${BUS_LOG_LEVEL}' BUS_API_TOKEN_VALUE='${BUS_API_TOKEN_VALUE}' bash -s" <<'EOF'
set -euo pipefail

if ! command -v python3 >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 unavailable after install attempt"
  exit 1
fi

if [[ ! -d "${APP_DIR}" ]]; then
  echo "Expected app directory missing: ${APP_DIR}"
  exit 1
fi

chmod +x "${APP_DIR}/scripts/run-dev-hot-reload.sh"
chmod +x "${APP_DIR}/scripts/deploy-dev-vm.sh"

if [[ ! -d "${APP_DIR}/venv" ]]; then
  python3 -m venv "${APP_DIR}/venv"
fi

"${APP_DIR}/venv/bin/pip" install --upgrade pip
"${APP_DIR}/venv/bin/pip" install -r "${APP_DIR}/requirements.txt" -r "${APP_DIR}/requirements-dev.txt"

mkdir -p /etc/iac-bus-dev
cat > /etc/iac-bus-dev/iac-bus-dev.env <<ENVFILE
APP_DIR=${APP_DIR}
BUS_HOST=0.0.0.0
BUS_PORT=${BUS_PORT}
BUS_MAX_MESSAGES=2000
BUS_RETENTION_SECONDS=21600
BUS_QUEUE_LEASE_SECONDS=60
BUS_LOG_LEVEL=${BUS_LOG_LEVEL}
FLASK_DEBUG=1
PYTHONUNBUFFERED=1
WATCH_DIRECTORY=${APP_DIR}
WATCH_PATTERNS=*.py;*.json;*.md;*.yaml;*.yml
WATCH_IGNORE_PATTERNS=*/.git/*;*/venv/*;*/.venv/*;*/__pycache__/*
ENVFILE

if [[ -n "${BUS_API_TOKEN_VALUE}" ]]; then
  echo "BUS_API_TOKEN=${BUS_API_TOKEN_VALUE}" >> /etc/iac-bus-dev/iac-bus-dev.env
fi

install -m 0644 "${APP_DIR}/systemd/iac-bus-dev.service" /etc/systemd/system/iac-bus-dev.service
systemctl daemon-reload
systemctl enable iac-bus-dev.service
systemctl restart iac-bus-dev.service
systemctl --no-pager --full status iac-bus-dev.service | sed -n '1,20p'
echo
echo "Recent logs:"
journalctl -u iac-bus-dev.service -n 30 --no-pager || true
EOF

echo
echo "Deployment complete."
echo "Use this for live debug logs:"
echo "  ssh ${SSH_OPTS[*]} ${TARGET} 'sudo journalctl -u iac-bus-dev.service -f'"
