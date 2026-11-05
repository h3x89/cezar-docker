#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

: "${CEZAR_VERSION:?Set the exact Cezar version}"
export CODEX_VERSION="${CODEX_VERSION:-0.166.0}"
RELEASE_CHANNEL="${RELEASE_CHANNEL:-version}"
IMAGE_REPOSITORY="${IMAGE_REPOSITORY:-ghcr.io/h3x89/cezar-docker}"
PUBLISH="${PUBLISH:-false}"

version_pattern='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$'
for version in "$CEZAR_VERSION" "$CODEX_VERSION"; do
  if [[ ! $version =~ $version_pattern || ${#version} -gt 128 ]]; then
    printf 'Invalid exact version: %s\n' "$version" >&2
    exit 1
  fi
done
case "$RELEASE_CHANNEL" in latest|nightly|version) ;; *) echo 'Invalid release channel' >&2; exit 1 ;; esac
case "$PUBLISH" in true|false) ;; *) echo 'PUBLISH must be true or false' >&2; exit 1 ;; esac
if [[ ! $IMAGE_REPOSITORY =~ ^ghcr\.io/[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)+$ ]]; then
  echo 'IMAGE_REPOSITORY must be a GHCR repository' >&2
  exit 1
fi

export CEZAR_VERSION CEZAR_IMAGE=cezar:release-test
image="$IMAGE_REPOSITORY:$CEZAR_VERSION"
docker build --platform linux/amd64 --pull --build-arg "CEZAR_VERSION=$CEZAR_VERSION" -t "$CEZAR_IMAGE" .
bash scripts/smoke.sh
docker tag "$CEZAR_IMAGE" "$image"
if [[ $RELEASE_CHANNEL != version ]]; then
  docker tag "$CEZAR_IMAGE" "$IMAGE_REPOSITORY:$RELEASE_CHANNEL"
fi

if [[ $PUBLISH == true ]]; then
  # The registry-reported digest from the exact-version push identifies the tested image.
  output=$(docker push "$image")
  printf '%s\n' "$output"
  digest=$(printf '%s\n' "$output" | sed -nE 's/.*digest: (sha256:[0-9a-f]{64}).*/\1/p' | tail -n 1)
  if [[ ! $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo 'Registry did not report a digest' >&2
    exit 1
  fi
  if [[ $RELEASE_CHANNEL != version ]]; then
    docker push "$IMAGE_REPOSITORY:$RELEASE_CHANNEL"
  fi
  if [[ -n ${RELEASE_RESULT:-} ]]; then
    python3 - "$RELEASE_RESULT" "$image" "$digest" <<'PY'
import json, os, sys, tempfile
path, image, digest = sys.argv[1:]
directory = os.path.dirname(os.path.abspath(path))
fd, tmp = tempfile.mkstemp(prefix='.release-result-', dir=directory, text=True)
try:
    with os.fdopen(fd, 'w') as f:
        json.dump({'image': image, 'digest': digest}, f)
        f.write('\n')
    os.replace(tmp, path)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
PY
  fi
fi
