#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <user@host> [ssh_key]"
  exit 1
fi

TARGET="$1"
SSH_KEY="${2:-}"
REF="${REF:-main}"
REPO_OWNER="${REPO_OWNER:-lehelkovach}"
REPO_NAME="${REPO_NAME:-iac-bus}"

SSH_OPTS=()
if [[ -n "${SSH_KEY}" ]]; then
  SSH_OPTS+=("-i" "${SSH_KEY}")
fi

ssh "${SSH_OPTS[@]}" "${TARGET}" \
  "sudo bash -lc 'REPO_OWNER=${REPO_OWNER} REPO_NAME=${REPO_NAME} REF=${REF} bash -s' " <<'EOF'
set -euo pipefail
TMP_DIR="/tmp/iac-bus-hotfix"
INSTALL_DIR="/opt/iac-bus"

mkdir -p "${TMP_DIR}"
rm -rf "${TMP_DIR:?}/"*

curl -fsSL "https://github.com/${REPO_OWNER}/${REPO_NAME}/archive/${REF}.tar.gz" \
  | tar -xz -C "${TMP_DIR}"

SRC_DIR="${TMP_DIR}/${REPO_NAME}-${REF}"
cd "${SRC_DIR}"
chmod +x ./deploy.sh
./deploy.sh
EOF

echo "Remote hotfix applied on ${TARGET}"
