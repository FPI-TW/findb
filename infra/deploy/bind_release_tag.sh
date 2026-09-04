#!/usr/bin/env bash
# Bind a production release tag to exactly one reviewed staging release.
#
# The object is deliberately separate from accepted deployment records: it is
# the immutable tag identity used to decide whether a later dispatch is still
# allowed to reach CI or deployment.
set -euo pipefail

: "${UNIT:?UNIT is required}"
: "${MODE:?MODE is required}"
: "${RELEASE_TAG:?RELEASE_TAG is required}"
: "${TAG_REF_OID:?TAG_REF_OID is required}"
: "${COMMIT_SHA:?COMMIT_SHA is required}"
: "${AWS_REGION:?AWS_REGION is required}"
: "${ACCOUNT_ID:?ACCOUNT_ID is required}"
: "${BUNDLE_BUCKET:?BUNDLE_BUCKET is required}"
: "${BUNDLE_KMS_KEY:?BUNDLE_KMS_KEY is required}"

fail() {
  echo "release_tag_binding=failed reason=$1" >&2
  exit 1
}

[[ "$UNIT" =~ ^(findb|fetcher)$ ]] || fail unit_invalid
[[ "$MODE" =~ ^(promote|rollback)$ ]] || fail mode_invalid
[[ "$TAG_REF_OID" =~ ^[0-9a-f]{40}$ ]] || fail tag_ref_oid_invalid
[[ "$COMMIT_SHA" =~ ^[0-9a-f]{40}$ ]] || fail commit_sha_invalid
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || fail account_invalid
[[ "$AWS_REGION" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]] || fail region_invalid
[[ "$BUNDLE_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail bucket_invalid
[[ "$BUNDLE_BUCKET" != *..* && "$BUNDLE_BUCKET" != *.-* && "$BUNDLE_BUCKET" != *-.* ]] || fail bucket_invalid
[[ "$BUNDLE_KMS_KEY" =~ ^arn:aws:kms:${AWS_REGION}:${ACCOUNT_ID}:key/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]] || fail kms_key_invalid

case "$UNIT" in
  findb)
    [[ "$RELEASE_TAG" =~ ^findb-v[0-9]+[.][0-9]+[.][0-9]+$ ]] || fail release_tag_invalid
    ;;
  fetcher)
    [[ "$RELEASE_TAG" =~ ^fetcher-v[0-9]+[.][0-9]+[.][0-9]+$ ]] || fail release_tag_invalid
    ;;
esac

if [ "$MODE" = promote ]; then
  : "${SOURCE_BUNDLE_KEY:?SOURCE_BUNDLE_KEY is required for promote}"
  [[ "$SOURCE_BUNDLE_KEY" =~ ^${UNIT}/accepted/${COMMIT_SHA}/[0-9a-f]{64}[.]tar$ ]] || fail source_bundle_key_invalid
else
  [ -z "${SOURCE_BUNDLE_KEY:-}" ] || fail rollback_source_bundle_key_forbidden
fi

[ "$(aws sts get-caller-identity --query Account --output text)" = "$ACCOUNT_ID" ] || fail account_mismatch

binding_key="${UNIT}/production/release-tags/${RELEASE_TAG}.binding.json"
binding_file="$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/release-tag-binding.XXXXXX.json")"
put_error_file="$(mktemp "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/release-tag-binding-put.XXXXXX.err")"
trap 'rm -f "$binding_file" "$put_error_file"' EXIT

validate_binding() {
  local source_key_pattern="^${UNIT}/accepted/${COMMIT_SHA}/[0-9a-f]{64}[.]tar$"
  jq -e \
    --arg unit "$UNIT" \
    --arg release_tag "$RELEASE_TAG" \
    --arg tag_ref_oid "$TAG_REF_OID" \
    --arg commit_sha "$COMMIT_SHA" \
    --arg source_bundle_key "${SOURCE_BUNDLE_KEY:-}" \
    --arg source_key_pattern "$source_key_pattern" '
      type == "object"
      and (keys | sort == ["commit_sha", "release_tag", "schema_version", "staging_accepted_bundle_key", "tag_ref_oid", "unit"])
      and .schema_version == 1
      and .unit == $unit
      and .release_tag == $release_tag
      and .tag_ref_oid == $tag_ref_oid
      and .commit_sha == $commit_sha
      and (.staging_accepted_bundle_key | type == "string" and test($source_key_pattern))
      and (if $source_bundle_key == "" then
             (.staging_accepted_bundle_key | type == "string")
           else
             .staging_accepted_bundle_key == $source_bundle_key
           end)
    ' "$binding_file" >/dev/null
}

if [ "$MODE" = promote ]; then
  jq -cn \
    --arg unit "$UNIT" \
    --arg release_tag "$RELEASE_TAG" \
    --arg tag_ref_oid "$TAG_REF_OID" \
    --arg commit_sha "$COMMIT_SHA" \
    --arg source_bundle_key "$SOURCE_BUNDLE_KEY" \
    '{schema_version: 1, unit: $unit, release_tag: $release_tag, tag_ref_oid: $tag_ref_oid, commit_sha: $commit_sha, staging_accepted_bundle_key: $source_bundle_key}' \
    > "$binding_file"

  if aws s3api put-object \
    --bucket "$BUNDLE_BUCKET" \
    --key "$binding_key" \
    --body "$binding_file" \
    --if-none-match '*' \
    --expected-bucket-owner "$ACCOUNT_ID" \
    --server-side-encryption aws:kms \
    --ssekms-key-id "$BUNDLE_KMS_KEY" \
    >/dev/null 2>"$put_error_file"; then
    created=true
  else
    created=false
  fi
fi

# Always read the durable object after a conditional put.  A failed conditional
# put can be a concurrent writer that won the race, and must be validated rather
# than retried or overwritten.
if ! aws s3api get-object \
  --bucket "$BUNDLE_BUCKET" \
  --key "$binding_key" \
  --expected-bucket-owner "$ACCOUNT_ID" \
  "$binding_file" >/dev/null; then
  fail binding_missing_or_unreadable
fi

if ! validate_binding; then
  fail binding_mismatch
fi

if [ "$MODE" = promote ] && [ "$created" = true ]; then
  echo "release_tag_binding=created key=$binding_key"
else
  echo "release_tag_binding=validated key=$binding_key"
fi
