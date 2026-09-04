#!/usr/bin/env bash
# Copy one immutable staging digest to production and verify its destination.
set -euo pipefail
set +x

: "${SOURCE_IMAGE_REF:?SOURCE_IMAGE_REF is required}"
: "${DESTINATION_REPOSITORY:?DESTINATION_REPOSITORY is required}"
: "${RELEASE_TAG:?RELEASE_TAG is required}"

[[ "$SOURCE_IMAGE_REF" =~ ^[^@]+@sha256:([0-9a-f]{64})$ ]] || { echo "promotion=failed reason=source_invalid" >&2; exit 1; }
source_digest="${BASH_REMATCH[1]}"
[[ "$DESTINATION_REPOSITORY" =~ ^[0-9]{12}[.]dkr[.]ecr[.]ap-southeast-1[.]amazonaws[.]com/.+$ ]] || { echo "promotion=failed reason=destination_invalid" >&2; exit 1; }
[[ "$RELEASE_TAG" =~ ^v[0-9]+[.][0-9]+[.][0-9]+$ ]] || { echo "promotion=failed reason=release_tag_invalid" >&2; exit 1; }

destination_tag="$DESTINATION_REPOSITORY:$RELEASE_TAG"
inspect_destination() {
  local stderr_file output
  stderr_file="$(mktemp)"
  trap 'rm -f -- "$stderr_file"' RETURN
  if output="$(docker buildx imagetools inspect --format '{{json .Manifest.Digest}}' "$destination_tag" 2>"$stderr_file")"; then
    printf '%s\n' "$output" | tr -d '"\n'
    return 0
  fi
  # Only a registry's explicit absence result permits creating an immutable
  # tag. Authentication, connectivity, throttling, or malformed responses
  # are fail-closed: treating those as absent could overwrite a collision.
  if grep -Eqi 'manifest unknown|name unknown|not found' "$stderr_file"; then
    return 3
  fi
  cat "$stderr_file" >&2
  return 1
}
if current="$(inspect_destination)"; then
  [[ "$current" = "sha256:$source_digest" ]] || { echo "promotion=failed reason=tag_collision" >&2; exit 1; }
  echo "promotion=ready state=idempotent image=$destination_tag"
  exit 0
else
  status=$?
  [ "$status" -eq 3 ] || { echo "promotion=failed reason=destination_inspect_failed" >&2; exit 1; }
fi
docker buildx imagetools create --tag "$destination_tag" "$SOURCE_IMAGE_REF"
actual="$(inspect_destination)" || { echo "promotion=failed reason=destination_digest_missing" >&2; exit 1; }
[[ "$actual" = "sha256:$source_digest" ]] || { echo "promotion=failed reason=destination_digest_mismatch" >&2; exit 1; }
echo "promotion=ready state=copied image=$destination_tag"
