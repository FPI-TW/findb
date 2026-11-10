terraform {
  # Production is a single fixed state root, so the whole backend is pinned
  # here. The state bucket policy denies every write whose SSE-KMS key header
  # is not this exact key (alias/findb-production-tofu-state), including the
  # native lockfile, so kms_key_id must travel with bucket and key.
  backend "s3" {
    bucket       = "findb-production-tofu-state-289112218471"
    key          = "production/control-plane.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    kms_key_id   = "arn:aws:kms:ap-southeast-1:289112218471:key/be629e7b-7540-4f6f-9818-a0b8f2d7bddc"
    use_lockfile = true
  }
}
