#!/usr/bin/env bash
# Fails if any term listed in the local, untracked .private-terms file appears in a tracked file.
# Keeps personal details (names, addresses, device ids, room names) out of this public repo
# without publishing the list itself. One term per line; blank lines and # comments ignored.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .private-terms ]; then
  echo "privacy-scan: SKIPPED (no .private-terms here)"
  exit 0
fi

fail=0
while IFS= read -r term; do
  case "$term" in ''|\#*) continue ;; esac
  # Capture instead of piping into `grep -q`: under pipefail its early exit SIGPIPEs xargs and hides a match.
  matches="$(git ls-files -z --cached --others --exclude-standard | xargs -0 grep -IliF -- "$term" 2>/dev/null || true)"
  if [ -n "$matches" ]; then
    echo "PRIVACY-SCAN FAIL: a private term appears in a tracked file"
    fail=1
  fi
done < .private-terms

if command -v gitleaks >/dev/null 2>&1; then
  for d in custom_components tests scripts; do
    gitleaks dir --no-banner --redact "$d" >/dev/null || { echo "PRIVACY-SCAN FAIL: gitleaks findings in $d"; fail=1; }
  done
else
  echo "privacy-scan: SKIPPED gitleaks (not installed)"
fi

[ "$fail" -eq 0 ] && echo "privacy-scan: clean"
exit "$fail"
