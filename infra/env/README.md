# AWS 環境設定

GitHub Actions 保留編排責任；部署不使用 GitHub Environment、Environment secrets、審批或 OIDC environment subject。
Repo 的 `bootstrap.json` 只保存 AWS account、region、unit bootstrap role 與 SSM 路徑。
`GITHUB_ENV` 是 runner 的暫存輸出檔，與 GitHub Environment 設定無關。

## 設定來源

| 設定 | 權威來源 | 讀取者 |
| --- | --- | --- |
| 部署控制 | `/findb/<environment>/<unit>/deployment` SSM String | unit deploy role |
| 應用設定 | `findb/<environment>/<unit>/runtime/configuration` Secrets Manager | instance role |
| DB、provider、Source、R2、TLS credentials | runtime catalog 指定的獨立 Secrets Manager secrets | instance 上各 consumer 的最小 scope |

`deployment.schema.json` 定義兩環境相同的必填欄位、選填欄位與預設值。
`infra/deploy/runtime-secrets/{findb,fetcher}.json` 定義每個 consumer 的相同設定／credentials schema。
不得為 staging 放寬必填值，或將應用秘密送進 runner。所有命令只回報欄位名稱與驗證狀態。

## 驗證與同步

各目錄的 `deployment.json.example` 是模板，KMS UUID 等示範值必須替換為實際資源。
Production promotion 設定需提供 production promotion role，以及 staging account、registry、bundle bucket、unit promotion reader role。
先驗證本機 JSON，再使用已登入且帳號吻合的 AWS profile 同步：

```bash
uv --directory backend run python ../infra/env/aws_configuration.py validate \
  --target staging --unit findb --input /private/tmp/findb-staging-deployment.json
AWS_PROFILE=staging uv --directory backend run python ../infra/env/aws_configuration.py sync \
  --target staging --unit findb --input /private/tmp/findb-staging-deployment.json
```

相同工具支援 `--target production` 與 `--unit fetcher`。`sync` 會覆寫該 unit 的單一 SSM parameter；
先保留原 parameter version，部署前對四組設定執行相同驗證。不得在此 JSON 加入 application secrets。
`FULL_MARKET_ENABLED` 預設 `false`，只接受 `true`／`false`；它不選擇安裝程式，也不啟動抓取。
`PROVIDER_WORK_REQUIRED` 在 deployment SSM 與 runtime configuration 缺省為 `true`；
明確 `false` 會在設定驗證／部署前拒絕。升級既有 false 環境前，須完成完整 provider-work
drain、備份與 cutover attestation，兩端同步 true；本機範本更新不會修改遠端設定。Session tokens 與 TAIFEX endpoint／timeout 等選填欄位缺省亦不代表部署設定失效。

應用設定與 credentials 由 host loader 在 tmpfs 建立短期檔案，結束即清除；TLS／nginx 敏感檔案留在
`/run/findb-runtime-secrets/nginx`，開機由 systemd 重新載入。詳細部署契約見
[deployment.md](../../docs/operations/deployment.md)。
