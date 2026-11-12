# The FinDB origin accepts HTTPS from Cloudflare only, matching production. The
# instance's other groups are not managed here: the RDS client group stays as is,
# and the legacy launch-wizard group is detached once by the operator after the
# first apply (it remains on the RDS interface, where it opens no listening port).
resource "aws_security_group" "findb_origin" {
  name        = "findb-staging-findb"
  description = "Cloudflare HTTPS only; no SSH"
  vpc_id      = var.findb_vpc_id
  ingress {
    description = "Cloudflare IPv4 HTTPS"
    protocol    = "tcp"
    from_port   = 443
    to_port     = 443
    cidr_blocks = var.cloudflare_ipv4_cidrs
  }
  ingress {
    description      = "Cloudflare IPv6 HTTPS"
    protocol         = "tcp"
    from_port        = 443
    to_port          = 443
    ipv6_cidr_blocks = var.cloudflare_ipv6_cidrs
  }
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.common_tags, { DeploymentUnit = "findb" })
}

resource "aws_network_interface_sg_attachment" "findb_origin" {
  security_group_id    = aws_security_group.findb_origin.id
  network_interface_id = data.aws_instance.findb.network_interface_id
}

# Warns on plan when any other group (such as the world-open launch-wizard group)
# is attached to the FinDB instance again.
check "findb_origin_groups" {
  assert {
    condition = toset(data.aws_instance.findb.vpc_security_group_ids) == toset([
      var.findb_rds_client_security_group_id,
      aws_security_group.findb_origin.id,
    ])
    error_message = "The FinDB staging instance must carry only the RDS client group and the Cloudflare-only origin group."
  }
}
