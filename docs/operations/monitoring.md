# Staging Native Monitoring Runbook

## Scope and current evidence boundary

`infra/tofu/staging/monitoring.tf` declares the Phase 6 control-plane baseline:
one private KMS-encrypted SNS topic, one required email subscription, six
native EC2/RDS alarms, and the applied custom-metric expansion.
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

## Live acceptance record (2026-09-01 to 2026-09-15)

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
- On 2026-09-09, a zero-destroy OpenTofu apply added
  `findb-staging-dlm-policy-unhealthy`. The FinDB collector has an exact
  `dlm:GetLifecyclePolicy` grant for `policy-0d0a29c9e19f6323e`; it published
  `DLMPolicyHealthy=1` after verifying both `State` and `StatusMessage` are
  `ENABLED`. The new alarm and all other 42 staging alarms were then `OK`.
  This detects a disabled/error policy or missing collector data; it does not
  prove that DLM created a usable snapshot.
- Later on 2026-09-09, a second zero-destroy apply added three active-feed
  anomaly alarms and updated the existing FinDB publisher in place (`3 added,
  4 changed, 0 destroyed`). A controlled calibration cloned the latest
  retained real Twelve Data delivery: removing required `close` persisted a
  `422/INGRESS_SCHEMA_INVALID` attempt, while an empty snapshot was accepted as
  a governed `202` warning run. The collector observed rejected and empty
  metrics changing from `0` to `1`; both alarms changed from `OK` to `ALARM`.
  `ActiveFeedDQErrors` stayed `0/OK`, proving that the signals remained
  separated. These are staging calibration records, not production incidents.
- On 2026-09-16, protected-`main` merge SHA
  `a72fe2e3ebcd12392ad9c806d207249984f73413` added four governance/security
  alarms. The fresh saved plan was `4 create, 5 in-place update, 0 destroy`
  with no replacements; apply was `4 added, 4 changed, 0 destroyed`, and the
  plan-time IAM policy update required no write after apply-time recomputation.
  The post-apply plan had no changes. FinDB and Fetcher publisher associations
  both completed successfully, and the new lineage orphan, EOD default
  partition, credential usage aggregate mismatch, and invalid-credential
  series each published `0` in the 09:50 and 09:55 five-minute buckets.
- A controlled request used an explicitly invalid non-production key and
  returned `403`; neither the collector nor CloudWatch retained the supplied
  value. `InvalidCredentialEvents` changed from `0` to `1`, and alarm
  `findb-staging-invalid-credential-events` changed `OK -> ALARM` at 09:57.
  CloudWatch recorded a successful SNS action; SNS recorded one delivery and
  zero failures for the transition. After the overlapping ten-minute log
  window expired, the metric naturally returned to `0` and the alarm changed
  `ALARM -> OK` at 10:07 without `SetAlarmState`. Final inventory was 46
  bounded metric series and 54/54 alarms `OK`, with actions enabled on all.

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
| DLM control plane | Exact root-volume policy health `< 1` for 2 × 5 minutes; query failure or missing data also breaches |
| Public TLS certificate | Verified `notAfter` days remaining `<= 30` for 2 × 5 minutes; TLS query/verification failure suppresses the healthy datum and missing data breaches |
| Fetcher runtime security | Each of the three active schedulers must preserve non-root `10001:10001`, read-only root, `Privileged=false`, `CapDrop=ALL`, no added capabilities, `no-new-privileges`, exact `/tmp` tmpfs protections, and the exact writable bind-mount allowlist; violation or missing data breaches |
| Active-feed contract | Rejected ingress-schema/required-field/completeness attempt count `>= 1` in the overlapping ten-minute window |
| Active-feed completeness | Non-rerun ingestion run with zero records `>= 1` in the overlapping ten-minute window |
| Active-feed DQ | Blocking DQ error count `>= 1` in the overlapping ten-minute window |
| Lineage integrity | Aggregate orphan count across PostgreSQL raw and every canonical table with `run_id`; any row or missing metric breaches |
| EOD partition routing | `market_data_eod_default` row count; any row or missing metric breaches |
| Credential aggregate integrity | Durable rollup/request aggregate mismatch count; any row or missing metric breaches |
| Invalid credentials | Rejected Source, Serve, or Admin credential log events `>= 1` in the overlapping ten-minute window |
| Staging CD | A failed or cancelled protected-`main` build/deploy publishes one sparse `DeploymentFailure` datum |

The collector reads scheduler heartbeat ages through the local authenticated
FinDB Admin endpoint from inside `findb-ingest`; the credential never crosses
the container boundary or enters metric dimensions/output. It reads only
Docker state, root filesystem counters, RabbitMQ local alarm flags, scheduler
keys/ages, bounded active-feed aggregate counts, aggregate DB integrity counts,
the stable invalid-credential log message, and RDS `LatestRestorableTime`. It
does not publish supplied keys, client IPs, endpoints, row IDs, request IDs,
symbols, or provider error text. Instance and deployment roles may
publish only the exact `FinDB/Staging` namespace; only the FinDB instance role
receives `rds:DescribeDBInstances` for the backup-lag observation.

The lineage check deliberately uses a periodic aggregate rather than new
canonical/raw foreign keys. Raw retention, staging reset, and long-lived
canonical rows have different deletion semantics, while adding constraints to
partitioned or growing tables would introduce migration locks and cascade risk.
The aggregate fails visibly without changing deletion behavior. The credential
check compares only durable counters; invalid-key events are counted from the
two API containers over an overlapping ten-minute window without parsing or
retaining the rejected key.

The four governance/security metrics and alarms completed protected-`main`
apply, two healthy periods, and controlled invalid-key notification acceptance
on 2026-09-16. They extend the prior 42-series/50-alarm baseline to 46 series
and 54 alarms.

The thresholds and evaluation settings are fixed-by-validation variables in
`infra/tofu/staging/variables.tf`; changing them requires a reviewed IaC
change. Container and scheduler resource sets are explicit in source; there
are no per-queue, per-symbol, per-request, or other unbounded dimensions.

PR [#237](https://github.com/FPI-TW/findb/pull/237) merged the custom metrics and alarms;
PR [#238](https://github.com/FPI-TW/findb/pull/238) corrected the State Manager cadence so its
supported 30-minute association installs and maintains the local five-minute timer. Live apply
created 36 custom alarms in addition to the six native alarms. Associations
`974a1570-3ace-4cc8-be97-09f4c5ba9eae`（FinDB）與
`9de9a3a4-ddc2-414c-8410-0dc1af9536e6`（Fetcher）均成功，`FinDB/Staging`
有34組bounded metric series，兩台collector連續成功，42個alarms後驗皆為`OK`。
2026-09-09追加DLM policy health後為35組bounded metric series與43個alarms；active-feed
anomaly expansion再增加3組series與3個alarms。2026-09-15的TLS與Fetcher runtime-security
expansion再增加4組series與4個alarms，因此穩態為42組series與50個alarms。受控校準期間
ingress-rejected與empty-snapshot為`ALARM`；17:20再次發布0後，兩者分別於17:24:42與17:24:56
自然回到`OK`，DQ全程為`OK`。不得在事件仍位於觀察窗時手動把alarm設回`OK`來掩蓋訊號。

PR [#257](https://github.com/FPI-TW/findb/pull/257) 合併後，2026-09-15由乾淨且與
`origin/main`一致的merge SHA `5a7f6c74ab8e972b9a798026a9f37a3c7523b18b`執行fresh saved plan。
Plan為4 create、5 in-place update、0 destroy且無replacement；apply結果為4 added、4 changed、
0 destroyed，另一個plan-time IAM policy update在apply重算後無需寫入。FinDB與Fetcher association
均更新至version 5並成功；後續SSM commands
`23d84369-59d4-44fe-8070-3ac0c12f8584`（FinDB）與
`c8d5562d-ba11-4d83-bb87-35002ed287f0`（Fetcher）再次執行publisher，均為`Success`／exit 0／
stderr空白，輸出分別保存於既有KMS-encrypted log groups。相鄰兩個五分鐘bucket的
`TLSCertificateDaysRemaining`為`71.33820040579862`與`71.33503951233797`天，三個
`DockerRuntimeSecurityHealthy` series兩輪均為`1`。四個新alarm使用2/2個五分鐘週期並以missing data
為breaching，健康評估均為`OK`；全體inventory後驗為50/50 `OK`、0 `ALARM`、0
`INSUFFICIENT_DATA`。這是正常路徑與fail-closed設定的live驗收，不宣稱曾將真實憑證推近到期或
弱化運行中container來做故障注入；既有SNS synthetic notification evidence仍獨立有效。

## Active-feed evidence and anomaly calibration

`infra/acceptance/collect_staging_feed_evidence.py` is a local operator
coordinator. It sends the repository's two read-only probes to the existing
Fetcher and FinDB execution units through SSM, merges only bounded non-secret
JSON, and can upload the result with explicit SSE-KMS headers and
`If-None-Match: *` to the existing versioned bundle bucket. It adds no host and
creates no runtime dependency between the two units. Use `--phase pre` first;
after a genuinely new eligible trade date, use `--phase post --pre-manifest
<local-pre-file>`. The post manifest stores the exact pre-file SHA-256. Never
create pre and post at the same observation point merely to close the gate.

The Backend probe treats `ingestion_attempt.http_status` as the durable Source
HTTP audit truth, so FinLab and Twelve Data acceptance no longer depends on
ephemeral nginx access logs. EOD feeds require the Serve read path. The minute
feeds have no public Serve read model and therefore declare Serve
`not_applicable` with reason `market_minute_read_model_not_exposed`; their
required operational read boundary is Admin raw, Admin market freshness, and
Dashboard representation.

2026-09-15 completed the natural `post` acceptance. The immutable manifest
SHA-256 is `36489b45ad94e5c10f4037f931898ca1b0ae8474f657395722124adaa5e50a24`;
it links the exact 2026-09-09 `pre` SHA-256
`7c234156bdf18964c7f1dc5d164a208a00c42db7458ee35fb30fffc0ec0e8e68` and is stored at
`evidence/staging/active-feeds/2026-09-15/post-36489b45ad94e5c1.json`, version
`.quZPhy3PoNJRFp3l5qzp14y7OCpeV7Z`, with the deployment-bundle KMS key. FinLab,
both Shioaji feeds, and Twelve Data advanced naturally to trade dates
2026-09-11 and 2026-09-14 while their reviewed config/universe identities
remained stable. All four feeds passed Source, Raw, Normalize, outbox, DQ, and
canonical checks; EOD Source attempts persisted HTTP `202`, and both minute
feeds retained the explicit non-Serve operational boundary. This closes the
multi-trade-date pre/post P0 gate without a same-observation replay.

`infra/acceptance/calibrate_staging_provider_alerts.py` is staging-only. It
uses a retained real Twelve Data request, generates fresh bounded request and
idempotency keys, and exercises missing-required-field and empty-snapshot
outcomes without calling the provider or changing scheduler state. The empty
warning run is excluded from pass-only delivery-policy baselines. Preserve its
attempt/run IDs in the live acceptance record; do not reuse this tool in
production.

Controlled tests使`findb-staging-findb-disk-used`與
`findb-staging-fetcher-deployment-failed`分別完成`OK -> ALARM -> OK`；SNS delivery metrics
記錄delivery且failed為0。這證明periodic與sparse custom alarm可抵達既有SNS topic；因本次未取得兩封
custom synthetic email的獨立收件確認，端到端inbox證據仍沿用前述native alarm測試，不把SNS delivery
metrics單獨描述為custom email收件證據。

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

## Applying monitoring changes

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
   backend and create a fresh saved plan after the exact commit reaches
   protected `main`. Review every action against the approved change. Any
   unexpected replacement or destroy, or any EC2, RDS, VPC, security group,
   volume, runtime-secret, or unrelated resource mutation is a no-go.
   The read-only plan role scopes CloudWatch alarm refresh to the deterministic
   `findb-staging-*` alarm ARN prefix. This bounded prefix is intentional: listing
   every custom alarm ARN for both metadata and tag refresh exceeds IAM's
   aggregate inline role-policy size limit.
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

The applied custom coverage includes disk/inode use, required container
health/restart count, Fetcher runtime-security contracts, RabbitMQ local
disk/memory alarms, persisted scheduler heartbeat age, deployment failure,
RDS backup lag, active-feed ingestion/DQ anomalies, and public TLS certificate
expiry. The certificate probe uses the default trust store and SNI/hostname
verification, reads `notAfter`, and emits days remaining; query or verification
failure emits no healthy datum, so the alarm's breaching missing-data policy
fails closed. Queue-depth trends, broader market-data freshness policy, and
other log-derived monitoring remain out of scope. These remaining domains must
not be inferred from the healthy alarm set.

The two current root volumes have been replaced with encrypted volumes, the
RDS restore rehearsal and SSH-ingress removal have also been completed, and
their live records are maintained in
[`deployment.md`](deployment.md#phase-6-live-recovery-record-2026-09-01-to-2026-09-03).
A one-time encrypted migration and a recurring backup chain are separate
controls; migration or backup chain evidence must remain distinct. Daily DLM policy
`policy-0d0a29c9e19f6323e` was created for the two exact current root volumes and immediate encrypted
snapshots completed. The 2026-09-08 live check found the policy in `ERROR` because copied source-volume
tags and schedule `TagsToAdd` both contained `Purpose`. The schedule-only key was changed to
`BackupPurpose`; a guarded zero-delete OpenTofu plan applied one in-place update, and AWS now reports the
policy as `ENABLED`. The DLM policy-health metric/alarm was then added with disabled, error, query-failure,
and missing-data behavior failing closed. The 2026-09-14 readback confirmed at least five distinct
dual-volume scheduled cycles through 2026-09-13, with five completed, encrypted, correctly tagged
recovery points per volume; the first two distinct cycles and recurring backup-chain acceptance are
therefore complete. Fetcher SQLite recovery, RabbitMQ rebuild from PostgreSQL,
different-digest rollback, and schema-rejection rehearsal completed on 2026-09-03; exact evidence is
in the linked deployment record. SSH ingress/recovery-key retirement was also completed: the matching host keys and both EC2
key-pair resources are absent, while unit-specific Session Manager break-glass
sessions and CloudTrail audit events were verified. HA and production recovery
remain separate scopes and must not be inferred from the staging controls.

AWS charges can arise from CloudWatch alarm evaluation, SNS publishes/email
notifications, and customer-managed KMS key/API use. CloudWatch native metric
retention is controlled by AWS service behavior rather than this stack; this
slice creates no custom metric retention policy. SNS does not provide a
durable incident archive in this configuration. Keep the existing SSM
CloudWatch Logs retention policy separate from these alarms. Custom metric
retention follows the CloudWatch service lifecycle; the stack does not create
a separate retention resource. Retain
operator test evidence according to the project's change/audit practice.
