variable "aws_region" {
  type    = string
  default = "ap-southeast-1"
  validation {
    condition     = var.aws_region == "ap-southeast-1"
    error_message = "Production region mismatch."
  }
}
variable "aws_account_id" {
  type    = string
  default = "289112218471"
  validation {
    condition     = var.aws_account_id == "289112218471"
    error_message = "Production account mismatch."
  }
}
variable "owner" {
  type    = string
  default = "tylercore"
}
variable "backup_owner" {
  type    = string
  default = "tylercore"
}
variable "github_repository" {
  type    = string
  default = "FPI-TW/findb"
  validation {
    condition     = var.github_repository == "FPI-TW/findb"
    error_message = "Repository trust is fixed."
  }
}
variable "operational_alert_email" {
  type    = string
  default = "tyler.ho@fpitw.com"
}
variable "db_name" {
  type    = string
  default = "findb"
}
variable "db_master_username" {
  type    = string
  default = "findb_root"
}
variable "cloudflare_ipv4_cidrs" {
  type    = list(string)
  default = ["173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22"]
}
variable "cloudflare_ipv6_cidrs" {
  type    = list(string)
  default = ["2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32"]
}
