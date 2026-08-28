#!/usr/bin/env bash
# Build a staging ECR image only when its immutable source SHA tag is absent.
# This makes a retry after a partial publisher-job failure safe: an already
# pushed exact tag is reused and never overwritten.

set -euo pipefail
set +x

image="${1:?image repository required}"
context="${2:?build context required}"
dockerfile="${3:?Dockerfile required}"

: "${AWS_REGION:?AWS_REGION is required}"
: "${GITHUB_REF:?GITHUB_REF is required}"
: "${GITHUB_SHA:?GITHUB_SHA is required}"

image_tag="${ECR_IMAGE_TAG:-$GITHUB_SHA}"
reuse_only="${ECR_REUSE_ONLY:-false}"

readonly expected_registry="439622209937.dkr.ecr.ap-southeast-1.amazonaws.com"
case "$image" in
  "$expected_registry/findb/staging/backend"|\
  "$expected_registry/findb/staging/dashboard"|\
  "$expected_registry/findb/staging/fetcher/twelve-data"|\
  "$expected_registry/findb/staging/fetcher/finlab"|\
  "$expected_registry/findb/staging/fetcher/shioaji")
    ;;
  *)
    echo "staging_ecr_build=failed reason=image_repository_contract" >&2
    exit 1
    ;;
esac

if [ "$AWS_REGION" != "ap-southeast-1" ] \
  || [ "$GITHUB_REF" != "refs/heads/main" ] \
  || ! printf '%s' "$GITHUB_SHA" | grep -Eq '^[0-9a-f]{40}$' \
  || ! printf '%s' "$image_tag" | grep -Eq '^[0-9a-f]{40}$' \
  || { [ "$reuse_only" = "false" ] && [ "$image_tag" != "$GITHUB_SHA" ]; } \
  || { [ "$reuse_only" != "true" ] && [ "$reuse_only" != "false" ]; }; then
  echo "staging_ecr_build=failed reason=publisher_identity_or_sha_contract" >&2
  exit 1
fi

repository_name="${image#"$expected_registry/"}"
error_file="$(mktemp)"
hex_file="$(mktemp)"
trap 'rm -f -- "$error_file" "$hex_file"' EXIT

if aws ecr describe-images \
  --region "$AWS_REGION" \
  --repository-name "$repository_name" \
  --image-ids "imageTag=$image_tag" \
  --query 'imageDetails[0].imageDigest' \
  --output text \
  --cli-error-format json > /dev/null 2>"$error_file"; then
  echo "staging_ecr_build=reused repository=$repository_name tag=$image_tag"
  exit 0
fi

# Some jq versions accept a raw NUL byte even though it is invalid JSON, so
# reject it before parsing. Other raw control bytes cause jq parsing to fail.
if ! LC_ALL=C od -An -v -tx1 "$error_file" > "$hex_file"; then
  echo "staging_ecr_build=failed reason=ecr_tag_inspection_failed repository=$repository_name" >&2
  exit 1
fi

nul_scan_status=0
LC_ALL=C grep -Eq '(^|[[:space:]])00([[:space:]]|$)' "$hex_file" \
  || nul_scan_status=$?
if [ "$nul_scan_status" -ne 1 ]; then
  echo "staging_ecr_build=failed reason=ecr_tag_inspection_failed repository=$repository_name" >&2
  exit 1
fi

# Only a single structured ImageNotFoundException with a non-empty message
# proves that this immutable tag is absent. `jq --slurp` rejects streams with
# additional JSON values; malformed JSON and raw control bytes also fail closed.
if ! jq -e --slurp '
  length == 1
  and (
    .[0]
    | type == "object"
      and (.Code | type == "string" and . == "ImageNotFoundException")
      and (.Message | type == "string" and length > 0)
  )
' "$error_file" > /dev/null; then
  echo "staging_ecr_build=failed reason=ecr_tag_inspection_failed repository=$repository_name" >&2
  exit 1
fi

if [ "$reuse_only" = "true" ]; then
  echo "staging_ecr_build=failed reason=rollback_tag_not_found repository=$repository_name tag=$image_tag" >&2
  exit 1
fi

docker buildx build \
  --file "$dockerfile" \
  --tag "$image:$image_tag" \
  --push \
  "$context"
echo "staging_ecr_build=pushed repository=$repository_name tag=$image_tag"
