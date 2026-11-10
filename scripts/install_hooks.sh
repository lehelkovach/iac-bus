#!/usr/bin/env bash
# Install the iac-bus pre-commit hook into a repository (or the current one).
#   bash scripts/install_hooks.sh [/path/to/repo] [--force]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-.}"
FORCE="${2:-}"
[[ "${TARGET}" == "--force" ]] && { TARGET="."; FORCE="--force"; }
HOOKS_DIR="$(git -C "${TARGET}" rev-parse --git-path hooks)"
case "${HOOKS_DIR}" in /*) ;; *) HOOKS_DIR="$(cd "${TARGET}" && pwd)/${HOOKS_DIR}" ;; esac
mkdir -p "${HOOKS_DIR}"
DEST="${HOOKS_DIR}/pre-commit"
if [[ -e "${DEST}" && "${FORCE}" != "--force" ]] && ! grep -q "iac-bus pre-commit hook" "${DEST}"; then
  echo "A different pre-commit hook exists at ${DEST}; re-run with --force to replace it, or call" >&2
  echo "${HERE}/hooks/pre-commit from it." >&2
  exit 1
fi
install -m 0755 "${HERE}/hooks/pre-commit" "${DEST}"
echo "installed ${DEST}"
echo "Commits in this repo now need a live exclusive iac-bus lock on every staged file"
echo "when an iac-bus MCP session is active (IAC_BUS_HOOK_STRICT=1 requires one always)."
