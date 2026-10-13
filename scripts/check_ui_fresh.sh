#!/usr/bin/env bash
# Fail when the shipped UI in easyagent/ui is not the build of the current web/src.
set -euo pipefail
cd "$(dirname "$0")/.."
(cd web && npm ci --no-audit --no-fund && npm run build)
if ! git diff --quiet --exit-code -- easyagent/ui || [ -n "$(git status --porcelain --untracked-files=all -- easyagent/ui)" ]; then
  echo "easyagent/ui is stale: it does not match a fresh build of web/src."
  echo "Run: cd web && npm ci && npm run build, then commit easyagent/ui."
  git status --short -- easyagent/ui
  exit 1
fi
echo "easyagent/ui matches web/src."
