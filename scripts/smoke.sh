#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
: "${CEZAR_VERSION:?Set the exact Cezar version}"
: "${CODEX_VERSION:?Set the exact Codex version}"
export CEZAR_IMAGE="${CEZAR_IMAGE:-cezar:smoke}"
export CEZ_DRY_RUN=1 CEZAR_PORT=0 CEZAR_BIND_ADDRESS=127.0.0.1
project="cezar-smoke-${GITHUB_RUN_ID:-local}-$$"
replacement="cezar:replacement-$$"
dc() { docker compose -p "$project" -f compose.yaml "$@"; }
cleanup() {
  status=$?
  if [[ $status -ne 0 ]]; then dc logs --no-color --tail 60 || true; fi
  # Only this script's disposable project and its test volumes are removed.
  dc down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker image rm "$replacement" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT

test "$(docker image inspect "$CEZAR_IMAGE" --format '{{.Architecture}}')" = amd64
docker run --rm --entrypoint bash "$CEZAR_IMAGE" -ceu '! command -v codex'
dc config --quiet
# The cockpit must start before a repository or agent CLI exists.
dc up -d --no-build --pull never --wait --wait-timeout 120
port=$(dc port cezar 4321)
curl --fail --silent "http://$port/api/v1/health" | python3 -c '
import json, os, sys
h = json.load(sys.stdin)
assert h["version"] == os.environ["CEZAR_VERSION"], h
assert h["repo"] is None, h
'
dc stop --timeout 60
dc run --rm --no-deps --entrypoint bash cezar -ceu '
  git init -b main .
  git -c user.name="Cezar test" -c user.email="ci@example.invalid" commit --allow-empty -m fixture
'
dc run --rm --no-deps --entrypoint codex-tool cezar install "$CODEX_VERSION"
# Expansions below run inside the container.
# shellcheck disable=SC2016
dc run --rm --no-deps --entrypoint bash cezar -ceu '
  test "$(command -v codex)" = "$HOME/.local/bin/codex"
  printf "configuration-survives\n" > "$HOME/.persistence-test"
  # An explicit non-credential fixture verifies storage, never real login.
  printf "{\"test_fixture\":\"not-a-credential\"}\n" > "$CODEX_HOME/auth.json"
  chmod 600 "$CODEX_HOME/auth.json"
'
dc run --rm --no-deps --entrypoint cezar cezar run mock:done

check_cockpit() {
  dc up -d --no-build --pull never --wait --wait-timeout 120
  port=$(dc port cezar 4321)
  response=$(curl --fail --silent "http://$port/api/v1/health")
  printf '%s' "$response" | python3 -c '
import json, os, sys
h = json.load(sys.stdin)
assert h["version"] == os.environ["CEZAR_VERSION"], h
assert any(c["name"] == "codex" and c["available"] for c in h["checks"]), h
'
  curl --fail --silent "http://$port/" >/dev/null
  dc exec -T cezar codex-tool check | grep -Fx "codex-cli $CODEX_VERSION"
}
check_cockpit
before=$(dc ps -q cezar)

# A different image configuration, same app: test replacement, not restart.
printf 'FROM %s\nLABEL io.cezar.test-replacement="true"\n' "$CEZAR_IMAGE" |
  docker build --quiet -t "$replacement" - >/dev/null
dc stop --timeout 60
dc rm -f cezar
export CEZAR_IMAGE="$replacement"
check_cockpit
test "$before" != "$(dc ps -q cezar)"
# Expansions below run inside the container.
# shellcheck disable=SC2016
dc exec -T cezar bash -ceu '
  test "$(cat "$HOME/.persistence-test")" = configuration-survives
  node -e '\''const fs=require("node:fs"); const p=process.env.CODEX_HOME+"/auth.json"; if(JSON.parse(fs.readFileSync(p)).test_fixture!=="not-a-credential")process.exit(1)'\''
  node -e '\''const fs=require("node:fs"); const runs=JSON.parse(fs.readFileSync("/workspace/project/.ai/cezar/runs.json")); if(!runs.some(r=>["done","review"].includes(r.status)))process.exit(1)'\''
'
# The persisted CLI also works without network or reinstallation.
docker run --rm --network none --mount "source=${project}_home,target=/home/cezar" \
  --entrypoint codex-tool "$replacement" check | grep -Fx "codex-cli $CODEX_VERSION"
printf 'PASS: Cezar %s; Codex %s; container replacement; persisted settings/auth fixture.\n' "$CEZAR_VERSION" "$CODEX_VERSION"
