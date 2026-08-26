terraform {
  # The bootstrap stack creates the bucket first. Keep this backend partial so
  # the globally unique bucket and KMS alias are supplied explicitly by the
  # operator after inventory. S3 native lockfiles are intentional; no
  # DynamoDB lock table is used.
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}
