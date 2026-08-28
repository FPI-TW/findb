# Preserve the addresses of every active runtime secret through the resource
# name split. The two legacy GHCR metadata addresses are intentionally absent:
# their exact deletion is controlled by the trusted retirement plan gate.
moved {
  from = aws_secretsmanager_secret.runtime["findb/database/application"]
  to   = aws_secretsmanager_secret.active_runtime["findb/database/application"]
}

moved {
  from = aws_secretsmanager_secret.runtime["findb/database/migration"]
  to   = aws_secretsmanager_secret.active_runtime["findb/database/migration"]
}

moved {
  from = aws_secretsmanager_secret.runtime["findb/api/admin-break-glass"]
  to   = aws_secretsmanager_secret.active_runtime["findb/api/admin-break-glass"]
}

moved {
  from = aws_secretsmanager_secret.runtime["findb/api/queue-health-admin"]
  to   = aws_secretsmanager_secret.active_runtime["findb/api/queue-health-admin"]
}

moved {
  from = aws_secretsmanager_secret.runtime["findb/api/lookup-serve"]
  to   = aws_secretsmanager_secret.active_runtime["findb/api/lookup-serve"]
}

moved {
  from = aws_secretsmanager_secret.runtime["findb/api/static-cache-serve"]
  to   = aws_secretsmanager_secret.active_runtime["findb/api/static-cache-serve"]
}

moved {
  from = aws_secretsmanager_secret.runtime["findb/rabbitmq/runtime"]
  to   = aws_secretsmanager_secret.active_runtime["findb/rabbitmq/runtime"]
}

moved {
  from = aws_secretsmanager_secret.runtime["findb/r2/canonical-publisher"]
  to   = aws_secretsmanager_secret.active_runtime["findb/r2/canonical-publisher"]
}

moved {
  from = aws_secretsmanager_secret.runtime["findb/r2/canonical-reader"]
  to   = aws_secretsmanager_secret.active_runtime["findb/r2/canonical-reader"]
}

moved {
  from = aws_secretsmanager_secret.runtime["fetcher/api/calendar-serve"]
  to   = aws_secretsmanager_secret.active_runtime["fetcher/api/calendar-serve"]
}

moved {
  from = aws_secretsmanager_secret.runtime["fetcher/api/source/twelve-data"]
  to   = aws_secretsmanager_secret.active_runtime["fetcher/api/source/twelve-data"]
}

moved {
  from = aws_secretsmanager_secret.runtime["fetcher/api/source/finlab"]
  to   = aws_secretsmanager_secret.active_runtime["fetcher/api/source/finlab"]
}

moved {
  from = aws_secretsmanager_secret.runtime["fetcher/api/source/shioaji"]
  to   = aws_secretsmanager_secret.active_runtime["fetcher/api/source/shioaji"]
}

moved {
  from = aws_secretsmanager_secret.runtime["fetcher/provider/twelve-data"]
  to   = aws_secretsmanager_secret.active_runtime["fetcher/provider/twelve-data"]
}

moved {
  from = aws_secretsmanager_secret.runtime["fetcher/provider/finlab"]
  to   = aws_secretsmanager_secret.active_runtime["fetcher/provider/finlab"]
}

moved {
  from = aws_secretsmanager_secret.runtime["fetcher/provider/shioaji"]
  to   = aws_secretsmanager_secret.active_runtime["fetcher/provider/shioaji"]
}

moved {
  from = aws_secretsmanager_secret.runtime["fetcher/r2/raw"]
  to   = aws_secretsmanager_secret.active_runtime["fetcher/r2/raw"]
}
