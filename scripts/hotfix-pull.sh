#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="${REPO_OWNER:-lehelkovach}"
REPO_NAME="${REPO_NAME:-iac-bus}"
REF="${REF:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/iac-bus}"
TMP_DIR="${TMP_DIR:-/tmp/iac-bus-hotfix}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run as root (sudo ./scripts/hotfix-pull.sh)"
  exit 1
fi

mkdir -p "${TMP_DIR}"
rm -rf "${TMP_DIR:?}/"*

curl -fsSL "https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/${REF}.tar.gz" \
  | tar -xz -C "${TMP_DIR}"

SRC_DIR="${TMP_DIR}/${REPO_NAME}-${REF}"
if [[ ! -d "${SRC_DIR}" ]]; then
  echo "Expected source dir not found: ${SRC_DIR}"
  exit 1
fi

if [[ ! -x "${SRC_DIR}/deploy.sh" ]]; then
  chmod +x "${SRC_DIR}/deploy.sh"
fi

cd "${SRC_DIR}"
./deploy.sh

echo "Hotfix applied from ${REPO_OWNER}/${REPO_NAME}@${REF}"
