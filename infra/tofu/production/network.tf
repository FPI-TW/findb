resource "aws_vpc" "production" {
  cidr_block           = "10.20.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = merge(local.common_tags, {
    Name = "findb-production", DeploymentUnit = "network"
  })
}
resource "aws_internet_gateway" "production" {
  vpc_id = aws_vpc.production.id
  tags = merge(local.common_tags, {
    DeploymentUnit = "network"
  })
}

locals {
  subnet_config = {
    public_a = {
      cidr = "10.20.0.0/24", az = "${var.aws_region}a", public = true
    }
    public_c = {
      cidr = "10.20.1.0/24", az = "${var.aws_region}c", public = true
    }
    private_a = {
      cidr = "10.20.100.0/24", az = "${var.aws_region}a", public = false
    }
    private_c = {
      cidr = "10.20.101.0/24", az = "${var.aws_region}c", public = false
    }

  }
}
resource "aws_subnet" "production" {
  for_each                = local.subnet_config
  vpc_id                  = aws_vpc.production.id
  cidr_block              = each.value.cidr
  availability_zone       = each.value.az
  map_public_ip_on_launch = false
  tags = merge(local.common_tags, {
    Name = "findb-production-${each.key}", DeploymentUnit = "network", Tier = each.value.public ? "public" : "private"
  })
}
resource "aws_route_table" "public" {
  vpc_id = aws_vpc.production.id
  tags = merge(local.common_tags, {
    DeploymentUnit = "network"
  })
}
resource "aws_route" "internet" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.production.id
}
resource "aws_route_table_association" "public" {
  for_each       = toset(["public_a", "public_c"])
  subnet_id      = aws_subnet.production[each.key].id
  route_table_id = aws_route_table.public.id
}
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.production.id
  tags = merge(local.common_tags, {
    DeploymentUnit = "database"
  })
}
resource "aws_route_table_association" "private" {
  for_each       = toset(["private_a", "private_c"])
  subnet_id      = aws_subnet.production[each.key].id
  route_table_id = aws_route_table.private.id
}

resource "aws_security_group" "findb" {
  name        = "findb-production-findb"
  description = "Cloudflare HTTPS only; no SSH"
  vpc_id      = aws_vpc.production.id
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
  tags = merge(local.common_tags, {
    DeploymentUnit = "findb"
  })
}
resource "aws_security_group" "fetcher" {
  name        = "findb-production-fetcher"
  description = "Fetcher has no inbound rules"
  vpc_id      = aws_vpc.production.id
  egress {
    protocol    = "-1"
    from_port   = 0
    to_port     = 0
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(local.common_tags, {
    DeploymentUnit = "fetcher"
  })
}
resource "aws_security_group" "database" {
  name        = "findb-production-rds"
  description = "PostgreSQL from FinDB only"
  vpc_id      = aws_vpc.production.id
  ingress {
    protocol        = "tcp"
    from_port       = 5432
    to_port         = 5432
    security_groups = [aws_security_group.findb.id]
  }
  tags = merge(local.common_tags, {
    DeploymentUnit = "database"
  })
}
