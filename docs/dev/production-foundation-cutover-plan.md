# Production foundation 與 cutover 計畫

> 未完成開發計畫；完成後應將仍有效的操作契約整併至 `docs/operations/` 並移除此檔。

## 現況

- [x] `findb-production-cd.yml`、`fetcher-production-cd.yml`與target-aware reusable deploy workflows已實作；
  annotated tag、immutable binding、staging v2 accepted bundle、digest copy、production accepted record與
  rollback fail-closed契約已完成dry-run驗證。
- [x] Production AWS account、IAM／OIDC、ECR、EC2、RDS、SSM／CloudWatch、deploy-bundle
  S3／KMS、runtime secrets與GitHub Environments已建立；production bounded runtime 已完成 live 驗證。
- [x] FinDB 與 Fetcher production v0.1.6 promote、bounded SSM activation、accepted record 回讀與
  原 Pilot state 恢復已完成；證據見 [deployment runbook](../operations/deployment.md#production-v016-rollout2026-10-13)。
- [x] Full-market Source credentials 與 published calendars 已完成唯讀 live preparation 驗證；
  此為明確授權且完成 review 的 credential preparation，可先於 Full runtime 安裝執行；不代表
  runtime acceptance。詳見 [production operations](../operations/production.md#production-full-market-credential-preparation2026-10-13)。
- [ ] Full-market pacing 修復、Fetcher runtime 部署與實際 provider acceptance 尚未完成。Production
  仍使用 bounded profile，`FULL_MARKET_ENABLED=false`；四個 Full controls 均 stopped（revision 3），
  四個 Full workers 尚未安裝。
- [ ] Production accepted-record-only rollback 的獨立 live acceptance 仍須補齊可核對紀錄。

Production 已有 live target；`PRODUCTION_DEPLOY_ENABLED` 平時為 false，在 operator 授權的 rollout
期間暫開並於終態恢復。v0.1.6 完成的是 bounded deployment，不代表 Full-market activation；
尚未完成的 pacing、Full runtime、provider acceptance 與 rollback 驗證繼續留在本計畫。

2026-10-13 Full credential preparation 的 Fetcher-side Source API 與 calendar 檢查均成功，且
Fetcher role 對 FinDB secrets 維持 `AccessDenied`；可核對的 SSM command IDs、scope 與限制見
[production operations](../operations/production.md#production-full-market-credential-preparation2026-10-13)。
這項明確授權的 preparation 以 `full-market` profile 校準 credentials，即使當時 accepted bundle
與 production runtime 仍為 bounded；profile 與 Source keys 不作為 Full runtime 已安裝或已驗收的證據。
Full runtime 的 OpenTofu plan 必須保留 `enable_full_market_runtime=true` 才會包含 TAIFEX secret
metadata，並保留該 secret 的 `prevent_destroy`。先前唯讀 plan 發現與此任務無關的兩台 EC2 AMI
replacement，均受 `prevent_destroy` 阻擋；該 plan 未 apply。S3 state backend 必須明確指定既有
state KMS key ARN，避免 plan 使用錯誤 state encryption 設定。

## 前提

- 建立獨立 production AWS account（`ap-southeast-1`）、每 unit 一台 EC2、獨立 ECR repositories、SSM target tags、CloudWatch log group 與 deploy bundle bucket prefix。
- Instance role 只能讀取 `findb/production/<unit>/`，deploy OIDC role 只能寫入 bundle、呼叫本 unit SSM 與複製 ECR manifest；它不得讀取 Secrets Manager value。promotion role 另需限於自身 `<unit>/production/release-tags/*` metadata prefix 的 `s3:GetObject`／條件式 `s3:PutObject` 與 deploy-bundle KMS encrypt/decrypt，讓 semver tag 可原子綁定 tag object OID、peeled commit與staging accepted key。
- 建立 production runtime secret catalog values（包含 `runtime/configuration`）及 host bootstrap，確認 catalog 以 `APP_ENVIRONMENT=production` 推導 prefix，而不是接受外來 prefix。
- foundation 與 live acceptance 完成前不得建立 `PRODUCTION_DEPLOY_ENABLED`；完成授權後才由 operator 設為精確的 `true`。

`runtime/configuration` 的 allowlist 以
`infra/deploy/runtime-secrets/findb.json`、`fetcher.json` 為準；同一relative name會由unit prefix隔離。
它保存application所需的非敏感runtime設定，讓GitHub Environment只保留AWS／SSM control plane。
不得在payload加入catalog未宣告的key，loader會拒絕cross-target prefix、unknown key及unsafe value。

## Cutover

1. 只接受 annotated immutable unit semver tag，解析其 tag object OID與peeled commit，先以條件式 KMS binding 固定 exact staging accepted bundle，再 rerun 完整 reusable CI；lightweight tag 必須 fail closed，重跑完整比對 binding，rollback 僅驗證既有 binding。
2. 在 staging accepted record 選擇 exact v2 bundle；驗證其 target、account、registry 與 image digest。
3. 手動 dispatch `promote`，輸入固定 confirmation；只 registry-copy digest，然後確認 destination digest 並生成 production v2 accepted record。
4. 以 bounded SSM candidate/activation rollout，驗證 health、marker、runtime secret isolation與 rollback record。
5. 首次成功後演練只重播 production accepted record 的 rollback；不得由 staging key、SHA tag 或缺失 manifest fallback。

## 未授權事項

本計畫不建立 AWS/IaC 資源、不 apply、不建立 production host，也不變更現有 staging recycle、DLM 或 active-feed 驗收工作。
