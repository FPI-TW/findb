# 環境與抓取流程統一（進行中）

目標是保留 GitHub Actions、移除 GitHub Environment 依賴，以 AWS 提供兩環境相同 schema 的設定。
Staging 自動部署 main，production 人工 promotion 同一組已驗收 image digests。
Pilot 與 full market 共用七條 feed 的完整流程，pilot 每日從 published universe 穩定排序取前兩個商品，
各自保存 plans、first dates、checkpoints 與缺漏；同環境共用帳號 governor。
所有抓取啟停由 Owner 人工操作；部署不得啟動抓取。

## 交付條件

- AWS 設定／OIDC／部署收斂，兩環境實際切換並驗證。
- 共用 runtime、profile 隔離、canonical 寫入證據與 Dashboard 分開展示。
- 全專案 check、test，IaC 檢查及兩環境部署健康驗收。
- 首個統一版本為新的 rollback 下限；舊資料與 quota 不得重置。
- Provider／R2 native 問題另列 backlog，部署通過不表示 native 資料驗收通過。

## 工作狀態

本機程式與部署流程已完成驗證：已加入 profile schema／migration、持久化 canonical 寫入證據、
共用七條 feed runtime、Dashboard 分頁，以及 SSM 設定與 main OIDC 部署流程。
現行契約已整併至部署、Full market、Dashboard 與測試文件；此處只保留未完成交付項。

本機 `pnpm check` 與完整 `pnpm test` 已通過（3,572 tests、268 subtests）；
首輪獨立 High implementation review 為 CLEAR（437 tests、10 subtests）。
2026-10-15 已更新兩個 AWS profile，核對帳號、四台主機與 main-ref OIDC subject。
Staging refresh plan 為新增 3／修改 11／刪除 0；production 原先因上游 current AMI
漂移而被 `prevent_destroy` 阻擋，已將 AMI 固定為兩台既有主機的版本，重新 plan 為
新增 0／修改 8／刪除 0（兩個 EC2 change 僅解除 AMI 的 sensitive 標記，值不變）。
此 IaC 補正已通過 `pnpm check`、兩環境 format／validate 與新一輪獨立 High review
（CLEAR，446 項針對性測試）。兩份 saved plan 已套用，隨後兩環境 refresh plan 均為 No changes。

四個 SSM deployment parameters 已建立並讀回驗證；staging 的兩個 runtime/configuration secrets
已依原有環境值建立。Backend 兩環境皆為 20 個設定欄位，Fetcher 皆為 15 個。
Staging TLS 已在原主機直接遷入 Secrets Manager，驗證 certificate／private key 配對，
遷移用 15 分鐘暫時 write policy 已撤除。四台主機以各自 instance role 驗證新版 catalog 通過
（每環境 Backend 11 個 secrets、Fetcher 10 個 secrets），沒有將應用秘密交給 runner。
設定與授權基礎已完成；staging 已進入停止／備份階段，尚未部署統一版本。

- 將統一版本經 PR／Required CI 整合至 protected main。
- 依正式文件備份並停止／drain 舊 runtime，先 staging、後 production promotion 同一組 digests。
- 保存兩環境部署健康驗收、profile 缺漏隔離證據；確認脫離依賴後刪除四個 GitHub Environments。
- Native provider／R2 問題與 native acceptance 繼續獨立列管，不能以 fixtures 或部署通過關閉。

尚未完成 AWS 應用實際切換，不能宣稱完整計畫完成。完成後移除此文件與對應 backlog。

2026-10-15 PR #294 Required CI 通過後已開始 staging 切換：Owner 停止八個 controls，
取消排隊歷史回補並確認 queue／outbox drain；八個 Fetcher 正常 exit 0，15 個 SQLite
與完整安裝備份、checksum 已核對。Backend 舊 shell PID 1 未轉送 TERM，ingest 與
raw cleanup 在 120 秒後 exit 137；切換已停在備份階段，尚未部署或 promotion。
正在修正新版 API 的 exec、dispatcher init 與 cleanup 可中斷的日循環，保留異常退出紀錄，
以停止後 DB 交易／drain 核對與成套快照完成復原基準；不得將 exit 137 記為優雅停止。
