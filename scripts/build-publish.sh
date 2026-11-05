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
  if [[ ! $version =~ $version_pattern || ${#version} -gt 122 ]]; then
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

export CEZAR_VERSION
image="$IMAGE_REPOSITORY:$CEZAR_VERSION"
for arch in amd64 arm64; do
  export CEZAR_PLATFORM="linux/$arch" CEZAR_IMAGE="cezar:release-test-$arch"
  docker build --platform "$CEZAR_PLATFORM" --pull --build-arg "CEZAR_VERSION=$CEZAR_VERSION" -t "$CEZAR_IMAGE" .
  bash scripts/smoke.sh
done

if [[ $PUBLISH == true ]]; then
  # Publish only after BOTH smoke tests. The shared tag selects the host platform.
  sources=()
  for arch in amd64 arm64; do
    ref="$IMAGE_REPOSITORY:$arch-$CEZAR_VERSION"
    docker tag "cezar:release-test-$arch" "$ref"
    output=$(docker push "$ref")
    printf '%s\n' "$output"
    digest=$(printf '%s\n' "$output" | sed -nE 's/.*digest: (sha256:[0-9a-f]{64}).*/\1/p' | tail -n 1)
    [[ $digest =~ ^sha256:[0-9a-f]{64}$ ]] || { echo 'Registry did not report a platform digest' >&2; exit 1; }
    sources+=("$IMAGE_REPOSITORY@$digest")
  done
  # regctl preserves the tested manifests and creates an annotated OCI index,
  # even when Docker pushed the individual images as schema 2.
  digest=$(regctl index create "$image" --ref "${sources[0]}" --ref "${sources[1]}" \
    --media-type application/vnd.oci.image.index.v1+json \
    --annotation 'org.opencontainers.image.description=Cezar for Linux AMD64 (x86_64) and ARM64 (aarch64); install Codex separately.' \
    --format '{{.Manifest.GetDescriptor.Digest}}')
  if [[ ! $digest =~ ^sha256:[0-9a-f]{64}$ ]]; then
    echo 'Registry did not report a digest' >&2
    exit 1
  fi
  regctl manifest get "$IMAGE_REPOSITORY@$digest" --format raw-body | python3 -c '
import json, sys
m = json.load(sys.stdin)
assert m["mediaType"] == "application/vnd.oci.image.index.v1+json", m
assert len(m["manifests"]) == 2, m
assert {(x["platform"]["os"], x["platform"]["architecture"]) for x in m["manifests"]} == {("linux", "amd64"), ("linux", "arm64")}, m
assert m["annotations"]["org.opencontainers.image.description"] == "Cezar for Linux AMD64 (x86_64) and ARM64 (aarch64); install Codex separately.", m
'
  if [[ $RELEASE_CHANNEL != version ]]; then
    regctl image copy "$IMAGE_REPOSITORY@$digest" "$IMAGE_REPOSITORY:$RELEASE_CHANNEL"
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
