#!/usr/bin/env bash
set -euo pipefail

INSTALL_DIR="/opt/iac-bus"
ENV_DIR="/etc/iac-bus"
ENV_FILE="${ENV_DIR}/iac-bus.env"
SERVICE_SRC="./systemd/iac-bus.service"
SERVICE_DST="/etc/systemd/system/iac-bus.service"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root (sudo ./deploy.sh)"
  exit 1
fi

mkdir -p "${INSTALL_DIR}" "${ENV_DIR}"
if [[ "${INSTALL_DIR}" == "/" ]]; then
  echo "Refusing to deploy to /"
  exit 1
fi
rm -rf "${INSTALL_DIR:?}/"*
tar --exclude ".git" --exclude "venv" --exclude ".venv" -cf - . | tar -xf - -C "${INSTALL_DIR}"

if [[ ! -d "${INSTALL_DIR}/venv" ]]; then
  python3 -m venv "${INSTALL_DIR}/venv"
fi

"${INSTALL_DIR}/venv/bin/pip" install --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

if [[ ! -f "${ENV_FILE}" ]]; then
  cat > "${ENV_FILE}" <<'EOF'
# Inter-Agent Communication Bus environment
BUS_HOST=0.0.0.0
BUS_PORT=8091
# BUS_API_TOKEN=replace-me
BUS_MAX_MESSAGES=500
BUS_RETENTION_SECONDS=3600
BUS_LOG_LEVEL=INFO
EOF
fi

install -m 0644 "${SERVICE_SRC}" "${SERVICE_DST}"
systemctl daemon-reload
systemctl enable iac-bus.service
systemctl restart iac-bus.service

echo "IAC Bus deployed. Edit ${ENV_FILE} to configure auth."
