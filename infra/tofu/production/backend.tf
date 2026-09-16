terraform {
  backend "s3" {
    bucket       = "findb-production-tofu-state-289112218471"
    key          = "production/control-plane.tfstate"
    region       = "ap-southeast-1"
    encrypt      = true
    use_lockfile = true

  }
}
