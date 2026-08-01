#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/deploy-oci-bus.sh user@host [remote_dir]

Deploy IAC Bus to an OCI host and run deploy.sh. If remote_dir already contains
a Git checkout, the script fetches and fast-forwards the selected branch. If not,
it rsyncs the current local checkout.

Environment:
  BUS_PORT       Port written to /etc/iac-bus/iac-bus.env (default: 8101)
  BRANCH         Remote Git branch to deploy (default: current branch)
  SSH_OPTS       Extra options passed to ssh, e.g. '-i ~/.ssh/key'
  RSYNC_OPTS     Extra options passed to rsync
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

REMOTE="${1:-}"
REMOTE_DIR="${2:-iac-bus}"
if [[ -z "${REMOTE}" ]]; then
  usage >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
BUS_PORT="${BUS_PORT:-8101}"
BRANCH="${BRANCH:-$(git -C "${ROOT_DIR}" rev-parse --abbrev-ref HEAD)}"
SSH_OPTS="${SSH_OPTS:-}"
RSYNC_OPTS="${RSYNC_OPTS:-}"

remote_dir_q="$(printf "%q" "${REMOTE_DIR}")"
branch_q="$(printf "%q" "${BRANCH}")"
bus_port_q="$(printf "%q" "${BUS_PORT}")"

ssh_cmd() {
  # shellcheck disable=SC2086
  ssh ${SSH_OPTS} "${REMOTE}" "$@"
}

echo "Preparing ${REMOTE}:${REMOTE_DIR}"
ssh_cmd "mkdir -p ${remote_dir_q}"

if ssh_cmd "test -d ${remote_dir_q}/.git"; then
  echo "Remote Git checkout found; pulling ${BRANCH}"
  ssh_cmd "cd ${remote_dir_q} && git fetch origin && git checkout ${branch_q} && git pull --ff-only origin ${branch_q}"
else
  if command -v rsync >/dev/null 2>&1; then
    echo "No remote Git checkout found; rsyncing local checkout"
    # shellcheck disable=SC2086
    rsync -az --delete ${RSYNC_OPTS} \
      --exclude ".git" \
      --exclude ".pytest_cache" \
      --exclude "__pycache__" \
      --exclude "venv" \
      --exclude ".venv" \
      -e "ssh ${SSH_OPTS}" \
      "${ROOT_DIR}/" "${REMOTE}:${REMOTE_DIR}/"
  else
    echo "rsync unavailable; cloning public GitHub branch ${BRANCH} on remote"
    ssh_cmd "git clone --branch ${branch_q} https://github.com/lehelkovach/iac-bus.git ${remote_dir_q}"
  fi
fi

echo "Running deploy.sh with BUS_PORT=${BUS_PORT}"
ssh_cmd "cd ${remote_dir_q} && sudo env BUS_PORT=${bus_port_q} ./deploy.sh"

echo "Ensuring /etc/iac-bus/iac-bus.env uses BUS_PORT=${BUS_PORT}"
ssh_cmd "if sudo grep -q '^BUS_PORT=' /etc/iac-bus/iac-bus.env; then sudo sed -i 's/^BUS_PORT=.*/BUS_PORT=${BUS_PORT}/' /etc/iac-bus/iac-bus.env; else echo 'BUS_PORT=${BUS_PORT}' | sudo tee -a /etc/iac-bus/iac-bus.env >/dev/null; fi && sudo systemctl restart iac-bus.service"

echo "Deployment complete. Smoke with:"
echo "  IAC_BUS_URL=http://<host>:${BUS_PORT} IAC_BUS_TOKEN=\$BUS_API_TOKEN ./scripts/bus_smoke.sh"
