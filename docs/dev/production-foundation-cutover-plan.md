# Production foundation 與 cutover 計畫

> 未完成開發計畫；完成後應將仍有效的操作契約整併至 `docs/operations/` 並移除此檔。

## 現況

- [x] `findb-production-cd.yml`、`fetcher-production-cd.yml`與target-aware reusable deploy workflows已實作；
  annotated tag、immutable binding、staging v2 accepted bundle、digest copy、production accepted record與
  rollback fail-closed契約已完成dry-run驗證。
- [ ] Production AWS account、IAM／OIDC、ECR、EC2、RDS、SSM／CloudWatch、deploy-bundle
  S3／KMS、runtime secrets與GitHub Environments尚未建立或完成live驗證。
- [ ] FinDB與Fetcher的production promote、bounded SSM activation及production accepted-record-only
  rollback尚未執行live acceptance。

Workflow存在不代表production target已存在或可部署；目前仍由缺少
`PRODUCTION_DEPLOY_ENABLED=true`與production foundation而fail closed。

## 前提

- 建立獨立 production AWS account（`ap-southeast-1`）、每 unit 一台 EC2、獨立 ECR repositories、SSM target tags、CloudWatch log group 與 deploy bundle bucket prefix。
- Instance role 只能讀取 `findb/production/<unit>/`，deploy OIDC role 只能寫入 bundle、呼叫本 unit SSM 與複製 ECR manifest；它不得讀取 Secrets Manager value。promotion role 另需限於自身 `<unit>/production/release-tags/*` metadata prefix 的 `s3:GetObject`／條件式 `s3:PutObject` 與 deploy-bundle KMS encrypt/decrypt，讓 semver tag 可原子綁定 tag object OID、peeled commit與staging accepted key。
- 建立 production runtime secret catalog values（包含 `runtime/configuration`）及 host bootstrap，確認 catalog 以 `DEPLOYMENT_TARGET=production` 推導 prefix，而不是接受外來 prefix。
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
