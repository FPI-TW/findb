#!/usr/bin/env bash
# One-time FinDB EC2 initialization script for Ubuntu 22.04
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
echo "  1. Create/configure the matching staging-findb or production-findb GitHub Environment."
echo "     Keep these settings out of repository scope and out of staging-fetcher/production-fetcher."
echo "     Staging application secrets remain environment-scoped during recycle."
echo "     Production application secrets are loaded by the instance role from"
echo "       findb/production/findb/ and are never stored in GitHub."
echo "     Staging-only secrets:"
echo "       DATABASE_URL"
echo "       ADMIN_BREAK_GLASS_API_KEY"
echo "       CELERY_BROKER_URL"
echo "       RABBITMQ_DEFAULT_USER"
echo "       RABBITMQ_DEFAULT_PASS"
echo "       RABBITMQ_ERLANG_COOKIE"
echo "       FINDB_QUEUE_HEALTH_ADMIN_API_KEY (DB-backed viewer key for deployment checks)"
echo "       FINDB_LOOKUP_SERVE_API_KEY (required when SERVE_REQUIRE_AUTH=true)"
echo "       FINDB_STATIC_CACHE_SERVE_API_KEY (required when SERVE_REQUIRE_AUTH=true)"
echo "     Variables:"
echo "       AWS_REGION, AWS_ACCOUNT_ID, AWS_DEPLOY_ROLE_ARN"
echo "       AWS_INSTANCE_PROFILE_NAME, AWS_SSM_LOG_GROUP"
echo "       ECR_REGISTRY, DEPLOY_BUNDLE_BUCKET, DEPLOY_BUNDLE_KMS_KEY_ARN"
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
echo "  3. Attach the unit-specific EC2 instance profile and apply the reviewed"
echo "     staging OpenTofu stack. Its SSM association installs the bounded"
echo "     dependency-free host/runtime metric publisher; do not persist AWS keys."
echo ""
echo "  4. Push through a PR to main. GitHub Actions will sync compose and deploy."
echo ""
echo "  5. Add EC2 user to docker group (optional, avoids sudo):"
echo "     usermod -aG docker \$USER && newgrp docker"
echo ""
echo "Fetcher is a separate deployment target. Do not configure provider,"
echo "raw-storage, or Fetcher Source client credentials on this host or"
echo "in the matching *-findb Environment; those belong only to the matching"
echo "*-fetcher Environment."
echo ""
echo "Setup complete."
