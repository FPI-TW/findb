#!/usr/bin/env bash
# One-time EC2 initialization script for Ubuntu 22.04
# Run as root or with sudo: sudo bash setup_ec2.sh

set -euo pipefail

echo "==> Installing Docker CE..."
apt-get update -y
apt-get install -y ca-certificates curl gnupg lsb-release

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable docker
systemctl start docker

echo "==> Creating /opt/findb..."
mkdir -p /opt/findb

echo ""
echo "==> Docker installed: $(docker --version)"
echo "==> docker compose installed: $(docker compose version)"
echo ""
echo "Next steps (manual):"
echo "  1. Copy docker-compose.prod.yml to /opt/findb/"
echo "     scp docker-compose.prod.yml ec2-user@<EC2_HOST>:/opt/findb/"
echo ""
echo "  2. Create /opt/findb/.env with production values:"
echo "     DATABASE_URL=postgresql+asyncpg://findb:PASSWORD@your-rds.rds.amazonaws.com:5432/findb"
echo "     DEBUG=false"
echo "     SOURCE_ALLOWLIST_CIDRS=10.0.0.0/8,<your-office-IP>/32"
echo "     SOURCE_API_KEYS=<production-source-key>"
echo "     ADMIN_API_KEYS=<production-admin-key>"
echo "     SERVE_REQUIRE_AUTH=false"
echo "     RATE_LIMIT_REQUESTS=100"
echo "     RATE_LIMIT_WINDOW=60"
echo "     RAW_RETENTION_ENABLED=false"
echo "     RAW_RETENTION_DAYS=14"
echo ""
echo "  3. Add EC2 user to docker group (optional, avoids sudo):"
echo "     usermod -aG docker \$USER && newgrp docker"
echo ""
echo "  4. Set the following GitHub Secrets:"
echo "     EC2_HOST  — EC2 public IP or domain"
echo "     EC2_USER  — ubuntu (Ubuntu AMI) or ec2-user"
echo "     EC2_SSH_KEY — full PEM private key text"
echo ""
echo "Setup complete."
