#!/usr/bin/env bash
# Credential glue for the release workflow's explicit Git commands.
# The token stays in the job environment; this file never stores its value.
# ci-inputs:
set -euo pipefail
: "${GH_TOKEN:?release Git authentication needs the job token}"
release_auth_tmp=$(mktemp -d "${RUNNER_TEMP:?}/release-git.XXXXXX")
trap 'rm -f "$release_auth_tmp/askpass"; rmdir "$release_auth_tmp"' EXIT
cat > "$release_auth_tmp/askpass" <<'ASKPASS'
#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s\n' x-access-token ;;
  *Password*) printf '%s\n' "$GH_TOKEN" ;;
  *) exit 1 ;;
esac
ASKPASS
chmod 700 "$release_auth_tmp/askpass"
GIT_ASKPASS="$release_auth_tmp/askpass" GIT_TERMINAL_PROMPT=0 git "$@"
