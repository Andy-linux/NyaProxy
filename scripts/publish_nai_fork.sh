#!/usr/bin/env bash
# Run from a restored Git checkout with gh already authenticated.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
command -v gh >/dev/null
fork_owner="$(gh api user --jq .login)"
gh repo fork Nya-Foundation/NyaProxy --clone=false --remote=false
fork_url="https://github.com/${fork_owner}/NyaProxy.git"
if git remote get-url fork >/dev/null 2>&1; then
  test "$(git remote get-url fork)" = "$fork_url"
else
  git remote add fork "$fork_url"
fi
# Do not force-update an existing fork's main branch.
git push -u fork feat/nai-utility-compat
printf 'Fork branch: https://github.com/%s/NyaProxy/tree/feat/nai-utility-compat\n' "$fork_owner"
