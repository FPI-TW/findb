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
  --output text > /dev/null 2>"$error_file"; then
  echo "staging_ecr_build=reused repository=$repository_name tag=$image_tag"
  exit 0
fi

# Reject NUL bytes before command substitution: Bash silently removes them
# while reading a file into a shell string. Capture and verify `od` separately
# so a scan failure also fails closed before the textual dump is inspected.
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

# Only one complete, known AWS CLI DescribeImages ImageNotFoundException proves
# that this immutable tag is absent. Accept both CLI prefixes and both observed
# ECR bodies, binding every variable field to this exact lookup. Wrapped,
# mixed, malformed, or forged diagnostics fail closed rather than causing a
# publish after an ambiguous inspection failure.
legacy_body="The image with imageId {imageTag=$image_tag} does not exist within the repository with name $repository_name in the registry with id 439622209937"
service_body="The image with imageId {imageDigest:'null', imageTag:'$image_tag'} does not exist within the repository with name '$repository_name' in the registry with id '439622209937'"
legacy_prefix='An error occurred (ImageNotFoundException) when calling the DescribeImages operation: '
current_prefix='aws: [ERROR]: An error occurred (ImageNotFoundException) when calling the DescribeImages operation: '

if ! awk 'END { exit NR == 1 ? 0 : 1 }' "$error_file" \
  || ! error_text="$(<"$error_file")" \
  || ! { [ "$error_text" = "$legacy_prefix$legacy_body" ] \
    || [ "$error_text" = "$current_prefix$legacy_body" ] \
    || [ "$error_text" = "$legacy_prefix$service_body" ] \
    || [ "$error_text" = "$current_prefix$service_body" ]; }; then
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
