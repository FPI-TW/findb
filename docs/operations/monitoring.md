# Staging Native Monitoring Runbook

## Scope and current evidence boundary

`infra/tofu/staging/monitoring.tf` declares the Phase 6 control-plane baseline:
one private KMS-encrypted SNS topic, one required email subscription, the six
already accepted native EC2/RDS alarms, and a pending custom-metric expansion.
The expansion uses two 30-minute SSM reconciliation associations to install and
maintain a five-minute local systemd timer plus the dependency-free
`infra/monitoring/publish_staging_metrics.py` collector. State Manager does not
support association intervals shorter than 30 minutes; the host timer preserves
the five-minute metric and two-period alarm contract. This does not install an
external agent, persist credentials, or add inbound network access. The existing
EC2 and RDS resources remain references only: this stack must not import, create,
replace, or otherwise manage EC2, RDS, VPC, security groups, or volumes.

The tracked configuration remains an **IaC declaration, not evidence by
itself**. A merged configuration, a successful `tofu validate`, or a
pull-request refresh plan does not establish that AWS resources exist, an email
subscription is confirmed, or an alert can be delivered. The live record below
is the separate evidence for the initial apply and notification exercise. Keep
the original safety distinction explicit: **IaC declarations, not live-apply or delivery evidence**,
are all that repository source alone can prove.

## Live acceptance record (2026-09-01 to 2026-09-03)

- CloudTrail records Tyler creating the private SNS topic at
  `2026-09-01 10:58:38 +08:00`, then creating the email subscription and all
  six declared alarms at `12:40:11`–`12:40:12 +08:00`.
- The topic uses the customer-managed KMS key behind
  `alias/findb-staging-operational-alerts`. Inventory on 2026-09-03 showed one
  confirmed email subscription, zero pending subscriptions, and all six alarms
  with actions enabled against the same topic.
- At `2026-09-03 09:53:47 +08:00`, Tyler set
  `findb-staging-findb-status-check-failed` from `OK` to `ALARM` with the
  explicit Phase 6 synthetic-test reason. The alarm was reset to `OK` at
  `09:54:04 +08:00`.
- The SNS metric for that interval recorded one delivered notification and
  zero failed notifications. The named inbox owner separately confirmed actual
  email receipt. This proves the CloudWatch alarm-to-SNS-to-inbox path for the
  controlled test; it does not prove a real EC2 or RDS failure mode.
- `/findb/staging/findb/ssm` and `/findb/staging/fetcher/ssm` remain
  KMS-encrypted with 30-day retention.

This record closes the owner/channel, subscription-confirmation, SSM log
retention, and synthetic-notification portion of Phase 6. It does not close the
coverage gaps below.

### Declaration, applied state, and live synthetic confirmation

Keep these three evidence levels separate. The tracked declaration contains
only `on-call@example.invalid`, which is the placeholder deliberately used by
PR refresh plans. An **applied state** can contain the real recipient because
the approved operator supplies it only through an ignored local tfvars file;
the existing remote state encryption protects that state, but it must still be
handled as restricted operational contact data and never copied into Git,
workflow logs, plans attached to tickets, or this runbook. Once the first
subscription has been applied, OpenTofu ignores `endpoint` drift, so a PR
refresh using the placeholder neither exposes nor replaces the state-held
recipient.

A **live synthetic confirmation** is separate again: the inbox owner accepts
the SNS confirmation email, then an authorized operator records a synthetic
`SetAlarmState`, inbox receipt, and reset to `OK`. Neither a declaration nor
an applied state proves those delivery results.

## Declared alarm contract

The six native alarms use an explicit 300-second period, two evaluation periods, two
datapoints to alarm, `treat_missing_data = "missing"`, enabled actions, the
same SNS alarm action, and empty OK/INSUFFICIENT_DATA action lists. A missing
native metric therefore remains visible as `INSUFFICIENT_DATA`; it is not
silently treated as healthy or breaching.

| Target | Native metric / statistic | Alarm condition |
| --- | --- | --- |
| Existing FinDB EC2 | `AWS/EC2` `StatusCheckFailed` / `Maximum` | `>= 1` for 2 × 5 minutes |
| Existing Fetcher EC2 | `AWS/EC2` `StatusCheckFailed` / `Maximum` | `>= 1` for 2 × 5 minutes |
| Existing `fin-db` RDS | `AWS/RDS` `FreeStorageSpace` / `Average` | `<= 5 GiB` for 2 × 5 minutes |
| Existing `fin-db` RDS | `AWS/RDS` `DatabaseConnections` / `Average` | `>= 80` for 2 × 5 minutes |
| Existing `fin-db` RDS | `AWS/RDS` `ReadLatency` / `Average` | `>= 0.1` seconds for 2 × 5 minutes |
| Existing `fin-db` RDS | `AWS/RDS` `WriteLatency` / `Average` | `>= 0.1` seconds for 2 × 5 minutes |

The custom expansion declares the following low-cardinality
`FinDB/Staging` metrics. Every datum has only `DeploymentUnit` and a bounded
`Resource` dimension. Missing periodic health metrics are breaching; the
sparse `DeploymentFailure` metric treats missing data as healthy. Alarms wait
for the first SSM association runs so normal metrics can be seeded before the
breaching-on-missing alarms are created.

| Coverage | Metric and condition |
| --- | --- |
| Both hosts | Root `DiskUsedPercent >= 85%`, `InodeUsedPercent >= 90%`, collector success `< 1` for 2 × 5 minutes |
| Required containers | Health `< 1` for 2 × 5 minutes; current-container restart count `>= 3` |
| RabbitMQ | Local disk or memory alarm flag `>= 1`; collected from `rabbitmq-diagnostics`, without credentials |
| Active Fetcher schedulers | Persisted heartbeat age `>= 180` seconds for FinLab, Shioaji, or Twelve Data |
| RDS recovery | `LatestRestorableTime` lag `>= 1800` seconds, providing a continuous PITR/backup-lag signal |
| Staging CD | A failed or cancelled protected-`main` build/deploy publishes one sparse `DeploymentFailure` datum |

The collector reads scheduler heartbeat ages through the local authenticated
FinDB Admin endpoint from inside `findb-ingest`; the credential never crosses
the container boundary or enters metric dimensions/output. It reads only
Docker state, root filesystem counters, RabbitMQ local alarm flags, scheduler
keys/ages, and RDS `LatestRestorableTime`. Instance and deployment roles may
publish only the exact `FinDB/Staging` namespace; only the FinDB instance role
receives `rds:DescribeDBInstances` for the backup-lag observation.

The thresholds and evaluation settings are fixed-by-validation variables in
`infra/tofu/staging/variables.tf`; changing them requires a reviewed IaC
change. Container and scheduler resource sets are explicit in source; there
are no per-queue, per-symbol, per-request, or other unbounded dimensions.

This expansion remains **declaration and saved-plan evidence only** until it is
merged through the protected-main IaC workflow, applied from a fresh reviewed
plan, produces two consecutive collector runs, and completes controlled
synthetic tests for representative periodic and sparse alarms. Until then the
initial six-alarm live record above remains the applied coverage boundary.

The SNS topic is KMS encrypted at rest and has no public allow statement. Its
topic policy gives the account root only a small enumerated set of topic and
subscription lifecycle actions—never `sns:Publish`—and permits the CloudWatch
service to publish only from the exact declared alarm ARNs in the exact source
account. Explicit deny statements reject direct/non-CloudWatch publishing,
missing or incorrect source accounts, and missing or unexpected source alarm
ARNs, so an account identity policy cannot bypass that provenance model. A
separate deny rejects insecure SNS API transport. Its KMS key permits the SNS
service only for this topic's encryption context and permits CloudWatch alarm
publishers only from the same exact ARNs and account. The ARNs are
deterministically derived in local IaC values, so these restrictive policies
can exist before the subscription and alarms are created without a dependency
cycle. Email is the only declared subscription protocol; SNS manages encrypted
HTTPS transport to its email delivery infrastructure.

## Planned live apply

Do not run this from a pull-request plan identity. That role is intentionally
read-only except for the native remote-state lockfile; it has no permission to
create or alter monitoring resources.

1. Review the configuration in a clean checkout at the approved commit. Copy
   `infra/tofu/staging/terraform.tfvars.example` to an ignored local
   `terraform.tfvars`, replace `operational_alert_email` with the on-call
   inbox, and do not commit the contact value. The tracked placeholder remains
   for PR refresh plans.
2. Confirm the account, region, existing EC2 IDs, `fin-db` identifier, current
   instance profiles, SSM Online state, and healthy baseline metrics from a
   read-only inventory.
3. From a separately authorized apply identity, initialize the reviewed remote
   backend and create a fresh plan after the exact commit reaches protected
   `main`. For this expansion, the expected shape is 38 creates (36 alarms and
   two associations), seven in-place policy updates, zero replacements, and
   zero destroys. The policy updates are limited to the exact alarm ARN list,
   namespace-scoped metric publishing, FinDB-only RDS metadata read, and
   read-only plan refresh for the associations. Any EC2, RDS, VPC, security
   group, volume, runtime-secret, or unrelated resource mutation is a no-go.
4. Apply only that reviewed fresh plan. Confirm both associations succeed,
   wait for two five-minute datapoints, verify every non-sparse alarm settles
   to `OK`, and retain the apply and association records. A breaching or
   missing baseline is a no-go for synthetic notification testing.

## Controlled recipient replacement

`operational_alert_email` deliberately has `ignore_changes = [endpoint]` so
the real state-held recipient is not replaced by the PR placeholder. Do not
remove that lifecycle rule or edit a tracked tfvars file to rotate a recipient.
Replacing a recipient is a deliberate subscription replacement and can cause a
brief notification gap:

1. Obtain change approval and prepare an ignored local tfvars file containing
   the new real recipient; do not put it in command arguments, Git, shared
   shell history, or the change record.
2. With the separately authorized apply identity, create and review a fresh
   plan that explicitly requests replacement:

   ```bash
   tofu -chdir=infra/tofu/staging plan \
     -var-file=terraform.tfvars \
     -replace='aws_sns_topic_subscription.operational_alert_email' \
     -out=recipient-replacement.plan
   ```

   Confirm the only intended replacement is the email subscription; the KMS
   key, topic, topic policy, and all declared alarms must remain unchanged.
3. Apply that reviewed plan from the same approved environment, accept the new
   SNS confirmation email, and repeat the live synthetic confirmation below.
   Record only non-contact evidence (UTC time, subscription confirmation,
   message IDs, and operator) in the change record.

## Confirm and exercise notification delivery

After an SNS email subscription is created, its status is `PendingConfirmation`
until the inbox owner selects Amazon SNS's confirmation link. Do not treat the
topic as an active paging path before checking the subscription is confirmed.

Use an operator identity with separately approved `cloudwatch:SetAlarmState`
permission. Substitute one actual declared alarm name; do not place it in
shell history shared with unrelated operators. Direct SNS `Publish` is not a
supported synthetic test: the topic policy deliberately allows only
CloudWatch to publish from the exact declared alarm ARNs.

```bash
aws cloudwatch set-alarm-state \
  --region ap-southeast-1 \
  --alarm-name 'findb-staging-findb-status-check-failed' \
  --state-value ALARM \
  --state-reason 'Synthetic operator notification test; no service fault.'
```

Verify receipt of the alarm notification at the confirmed inbox and record UTC
time, message ID, topic ARN, alarm name, and operator. `set-alarm-state`
validates that the selected alarm action can publish to the encrypted topic and
that SNS can deliver to the confirmed inbox. This supported end-to-end
synthetic test does not validate a real EC2 or RDS failure mode.

Immediately reset the synthetic alarm so it cannot obscure later incidents:

```bash
aws cloudwatch set-alarm-state \
  --region ap-southeast-1 \
  --alarm-name 'findb-staging-findb-status-check-failed' \
  --state-value OK \
  --state-reason 'Synthetic operator notification test complete; reset to OK.'
```

Then inspect the alarm history and inbox for the reset transition. Do not
delete the topic, KMS key, subscription, or alarms as test cleanup; they are
the persistent operating control. If an incorrect subscription was applied,
use the controlled recipient replacement procedure above and reconfirm the
replacement. If the runbook test must be repeated, use a clearly labelled
synthetic reason and reset the selected alarm to `OK` again.

## Coverage gaps and cost / retention caveats

The custom declaration does **not** claim completion until its protected-main
apply and live acceptance are recorded. It covers disk/inode use, required
container health/restart count, RabbitMQ local disk/memory alarms, persisted
scheduler heartbeat age, deployment failure, and RDS backup lag. Queue-depth
trends, market-data freshness policy, ingestion/delivery/DQ, TLS certificate,
and other log-derived monitoring remain out of scope. Until the expansion is
live, a healthy native alarm set can still miss a full disk, stalled scheduler,
repeated container crash, broker resource alarm, failed deployment, or failed
backup.

The two current root volumes have been replaced with encrypted volumes, the
RDS restore rehearsal and SSH-ingress removal have also been completed, and
their live records are maintained in
[`deployment.md`](deployment.md#phase-6-live-recovery-record-2026-09-01-to-2026-09-03).
A one-time encrypted migration and a recurring backup chain are separate
controls; migration or backup chain evidence must remain distinct. The current
volumes still have no recurring AWS Backup or DLM policy. Fetcher SQLite
recovery, RabbitMQ rebuild from PostgreSQL, different-digest rollback, and
schema-rejection rehearsal also remain open. SSH ingress/recovery-key retirement
was completed on 2026-09-03: the matching host keys and both EC2
key-pair resources are absent, while unit-specific Session Manager break-glass
sessions and CloudTrail audit events were verified. The remaining recovery risks
must not be inferred as covered by native alarms, encryption, or a one-time
migration snapshot.

AWS charges can arise from CloudWatch alarm evaluation, SNS publishes/email
notifications, and customer-managed KMS key/API use. CloudWatch native metric
retention is controlled by AWS service behavior rather than this stack; this
slice creates no custom metric retention policy. SNS does not provide a
durable incident archive in this configuration. Keep the existing SSM
CloudWatch Logs retention policy separate from these alarms. Custom metric
retention follows the CloudWatch service lifecycle; the stack does not create
a separate retention resource. Retain
operator test evidence according to the project's change/audit practice.
