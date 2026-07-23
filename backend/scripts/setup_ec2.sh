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
echo "  1. Configure all production environment variables in GitHub Actions."
echo "     Secrets:"
echo "       EC2_HOST"
echo "       EC2_USER"
echo "       EC2_SSH_KEY"
echo "       DATABASE_URL"
echo "       SOURCE_API_KEY"
echo "       ADMIN_API_KEY"
echo "       CELERY_BROKER_URL"
echo "       RABBITMQ_DEFAULT_USER"
echo "       RABBITMQ_DEFAULT_PASS"
echo "       RABBITMQ_ERLANG_COOKIE"
echo "       SERVE_API_KEYS (optional Serve fallback; DB-backed keys preferred)"
echo "       FINDB_STATIC_CACHE_SERVE_API_KEY (required when SERVE_REQUIRE_AUTH=true)"
echo "     Variables:"
echo "       APP_NAME, APP_VERSION, DEBUG, PORT"
echo "       DATABASE_POOL_SIZE, DATABASE_MAX_OVERFLOW"
echo "       API_V1_PREFIX, API_KEY_HEADER"
echo "       SOURCE_ALLOWLIST_CIDRS (nginx Source API allowlist)"
echo "       SOURCE_TRUST_PROXY_HEADERS"
echo "       SERVE_REQUIRE_AUTH"
echo "       RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW"
echo "       RAW_RETENTION_ENABLED, RAW_RETENTION_DAYS"
echo "       FINDB_STATIC_CACHE_BASE_URL, FINDB_LATEST_PRICE_WORKERS"
echo ""
echo "  2. Attach and verify RabbitMQ storage before deploying:"
echo "       - encrypted 20 GiB gp3 EBS"
echo "       - DeleteOnTermination=false"
echo "       - filesystem UUID entry in /etc/fstab"
echo "       - mounted at /var/lib/findb/rabbitmq"
echo "       - owner UID/GID 999:999"
echo "       - no Security Group ingress for 5672 or 15672"
echo ""
echo "  3. Install/configure CloudWatch Agent and attach an EC2 IAM role that can"
echo "     publish host, disk, container, and FinDB queue-health metrics."
echo ""
echo "  4. Push through a PR to main. GitHub Actions will sync compose and deploy."
echo ""
echo "  5. Add EC2 user to docker group (optional, avoids sudo):"
echo "     usermod -aG docker \$USER && newgrp docker"
echo ""
echo "Setup complete."
