# Legacy／Floating Compatibility 移除計畫

## 目的與範圍

本計畫分波移除六項仍存在的legacy、floating或deprecated compatibility，優先級為P1技術債，不阻塞
active feed acceptance或staging AWS P0工作：

1. `nginx:alpine` floating image。
2. Fetcher v1 schedule、舊slot identity及舊SQLite state migration。
3. Backend scheduler key／slot alias及`dataset_keys` JSONB projection。
4. Fetcher Raw bucket舊binding marker。
5. `/instrument-lookup`與`/skill-install`舊public static routes。
6. Deprecated `fix_misrouted_tw_futures.py`一次性資料修復工具。

本計畫只定義移除順序、gate與驗收。每一波使用獨立PR；state／marker收斂必須先於相容
程式刪除，DB欄位移除必須晚於association-only application release。不得為了完成移除而
重置SQLite、刪除資料、猜測碰撞結果或在staging執行Alembic downgrade。

## 現況與完成條件

| 項目 | 現況 | 完成條件 |
| --- | --- | --- |
| Nginx image | Production Compose使用`nginx:alpine` | 使用staging已驗收的明確版本與repository digest |
| Fetcher schedule/state | Twelve Data／FinLab的通用`SchedulerState`可讀v1／v2並接受舊slot；Shioaji使用獨立`meta.schema_version=9` state | Twelve Data／FinLab的通用state全為SQLite `PRAGMA user_version=3`；Shioaji全為`meta.schema_version=9`，不套用generic legacy-slot migration；runtime只接受current config、provider-specific current state及canonical slot |
| Backend scheduler | 接受舊key／slot，ORM仍保留`dataset_keys` projection | 舊輸入fail closed；association是唯一dataset mapping；projection已移除 |
| Raw bucket marker | Twelve Data可accept-once舊account＋bucket marker | 所有provider只接受provider-scoped marker |
| Static routes | Backend仍提供兩條舊HTML route | 舊route與HTML移除且回`404`；Dashboard替代功能不變 |
| TW futures修復工具 | Deprecated工具只處理歷史TW futures／option routing殘留；目前沒有active TW futures contract feed | 歷史適用資料面及promotion來源以exact-image dry-run驗證為零候選；現行Shioaji／market-minute路徑的0050回歸維持`TW/etf`；工具與現行文件引用已移除 |

整體完成時，將仍有效的部署、ingestion及維護規則併入現行文件，從backlog移除此項，並在
同一PR刪除本文件。Git history是唯一歷史保存方式，不建立archive或redirect stub。

## Wave 1：固定Nginx image

1. 在staging讀取目前已驗收`findb-nginx` container的image ID與RepoDigest，確認digest能對應
   registry中的明確版本。只允許讀取metadata，不pull其他版本作替代。
2. 若目前image沒有可驗證RepoDigest，或host與registry資訊不一致，標記no-go並先釐清
   image來源；不得自動升級或以新`alpine`結果代替。
3. 將`docker-compose.prod.yml`的Nginx reference改為literal版本加digest。此pin獨立於
   application release manifest；既有application image promotion流程不因此改變。
4. Render Compose並驗證Nginx設定、health、public readiness及rollback至前一個exact digest。

驗收：Compose不得包含Nginx floating tag；缺少或格式錯誤的digest必須在部署前失敗。

## Wave 2：收斂Fetcher state與Raw marker

此波只使用目前仍具相容能力的release收斂live state，不移除reader或migrator；版本gate依各provider的state implementation判定。

1. 對每個已部署provider停止single writer，備份SQLite state與Raw bucket marker，再執行
   SQLite integrity／schema檢查。尚未建立的target以資源盤點證據記為N/A。
2. 讓Twelve Data與FinLab的通用`SchedulerState`由現行runtime升至SQLite `PRAGMA user_version=3`；
   Shioaji不使用此通用state，僅驗證獨立state的`meta.schema_version=9`，不做generic legacy
   slot migration。通用state／schedule job必須確認沒有`legacy`、舊slot alias或仍依賴
   `legacy_schedule_id`；任何帶舊slot的pending、running或retry prepared request都必須先安全
   完成，provider-specific版本或identity不符時fail closed，不得直接刪除。
3. 以job identity、attempt及terminal state檢查migration碰撞。無法唯一判定、資料不一致或
   integrity check失敗時停止rollout，另開具operator核准的remediation工作。
4. 讓Twelve Data執行既有accept-once流程，確認marker已使用account、bucket、provider共同
   計算的fingerprint；所有provider marker owner／mode必須為`10001:10001`／`0600`。
5. 從備份複製品演練SQLite及marker還原，重新執行相同preflight；不得覆寫live state驗證。

驗收：所有已部署target均有對應版本備份（Twelve Data／FinLab通用state為SQLite
`PRAGMA user_version=3`；Shioaji為`meta.schema_version=9`）、成功的restore rehearsal、
canonical slot及provider-scoped marker，且沒有legacy nonterminal request。未達任一條件即不得
進入Wave 3。

## Wave 3：發布strict Fetcher

1. 移除v1 schedule config／loader、`legacy_schedule_id`、舊slot map、contract validation
   compatibility copy、Twelve Data／FinLab通用`SchedulerState`的v1／v2 state migrator及
   `legacy` defaults；Shioaji獨立state不引入或套用通用migrator。
2. Scheduler CLI及runtime只接受current manifest與provider-specific current state：Twelve
   Data／FinLab通用`SchedulerState`必須是SQLite `PRAGMA user_version=3`；Shioaji獨立state
   必須是`meta.schema_version=9`，且不經generic legacy slot migration。舊config、舊state或
   舊delivery slot在啟動／validation階段fail closed，不進入delivery。
3. 刪除Twelve Data舊Raw marker fingerprint與特殊接受分支；三個provider共用相同的
   provider-scoped marker拒絕規則。
4. 先逐一部署provider runtime，確認single writer、checkpoint、delivery及graceful stop，再
   觀察至少兩個有效交易日週期。失敗時只有在state仍相容時才回切前一exact image；否則
   保持writer停止並forward-fix。

驗收：Twelve Data／FinLab通用state的current SQLite `PRAGMA user_version=3` recovery測試，
以及Shioaji獨立state的`meta.schema_version=9` strict recovery／fail-closed測試保留；只服務
v1／v2、舊slot及舊marker接受行為的測試與fixture移除，並由拒絕測試取代。

## Wave 4：Backend application contract

1. Deployment preflight確認所有`scheduler_control.slot_id`及dataset delivery schedule皆為
   canonical slot，舊FinLab scheduler key不存在，且每筆`dataset_keys` projection都與
   `scheduler_dataset` association一致。
2. Application停止normalize舊scheduler key／slot，停止讀取`dataset_keys` fallback，API的
   `dataset_keys` response一律由association產生。此版仍保留DB欄位，供expand／contract
   rollout期間新舊application共存與資料比對。
3. 舊scheduler poll／update key回`404`；舊Admin slot filter與ingress delivery slot回`422`。
   Canonical request、response shape及generated contract不變。
4. 部署後驗證Fetcher只使用canonical key／slot，Admin、Source及Dashboard canonical流程正常，
   並確認沒有projection fallback read。

驗收：舊輸入不得被靜默改寫；canonical API response與現行consumer行為不變。

## Wave 5：移除DB projection

1. 新增Alembic migration；不得修改既有canonical slot migration。
2. Upgrade先驗證association與JSON projection集合完全一致、slot全為canonical且舊scheduler
   key不存在。任何不一致都raise並停止，不能自動補值或刪值。
3. 驗證通過後移除`dataset_keys` JSONB及其constraint；可對已封閉的canonical slot vocabulary
   加DB constraint，但不得將可擴充的scheduler key寫成封閉清單。
4. Downgrade只供本機migration round-trip，由association重建projection；staging採forward-only，
   migration後rollback依schema相容性選擇previous digest或forward-fix。

驗收：空DB及受支援舊revision可upgrade；不一致fixture會fail closed；
upgrade／downgrade／upgrade round-trip通過。

## Wave 6：移除legacy public routes

1. 刪除`/instrument-lookup`、`/skill-install` route handlers、constants及兩份舊HTML，不新增
   redirect或replacement stub。
2. 保留Dashboard `/dashboard/lookup`、`/dashboard/skill`、static mount、`findb-api.skill`、
   `/test`及`test_page.html`。
3. Nginx Serve key Referer規則移除舊`/instrument-lookup`分支，只允許Dashboard lookup頁的
   既有注入行為；其他request仍依原規則passthrough。
4. 同步清除README、Agent指南、測試與操作文件中的舊route／HTML敘述。

驗收：兩條舊route及對應HTML path回`404`；Dashboard替代頁、skill archive下載、`/test`及
Dashboard lookup的Serve key注入仍正常。

## Wave 7：移除deprecated TW futures修復工具

1. 使用exact application image在每個既存staging及production RDS執行
   `fix_misrouted_tw_futures.py` dry-run，保存target、image digest、時間及bounded candidate
   counts；不得在輸出記錄連線資訊或完整資料內容。
2. exact-image dry-run候選範圍必須涵蓋歷史`TW/equity 0050` duplicate及工具目前辨識的
   `TW/equity`期貨／選擇權routing residues；這是既存資料清理，不代表現行有futures feed。
   目前repo沒有active TW futures contract feed或current futures normalizer，不得以不存在的
   API／feed作為gate。尚未建立的production target可用AWS資源盤點記為N/A，但任何預定
   promotion至production的snapshot、dump或seed也必須完成相同零候選驗證。
3. 任一資料面有候選時立即no-go。另開具RDS snapshot、writer pause、dry-run review、
   `--apply`、FK／canonical／Serve驗證及cache重生的獨立修復工作；完成後重新執行dry-run，
   直到所有適用資料面為零。
4. Active regression coverage必須透過現行Shioaji／market-minute path驗證`0050`（`tw_etf_minute`）
   持續寫入`TW/etf`；歷史TW futures／option residues只由上述exact-image dry-run覆蓋。
   不新增或假設current futures API、normalizer或feed，避免移除工具後重建相同殘留。
5. Gate通過後刪除`backend/scripts/fix_misrouted_tw_futures.py`，並從data maintenance、root
   Agent指南及所有repo引用移除。`cleanup_stale_instruments.py`不在此波範圍內。

驗收：所有適用資料面及production promotion來源的歷史TW futures／option residues exact-image
dry-run皆為零候選；現行Shioaji／market-minute的0050 regression持續為`TW/etf`；repo不再
包含腳本或引用；canonical ingest、Serve及instrument cache測試通過。不得保留stub、deprecated
wrapper或以GitHub artifact封存腳本。

## Public interfaces

- 不新增API或schema。
- 舊scheduler key不再alias到canonical key，回`404`。
- 舊slot ID不再被ingress或Admin filter接受，回`422`。
- `dataset_keys` API欄位保留，來源改為`SchedulerDataset` association。
- `/instrument-lookup`與`/skill-install`永久移除並回`404`。
- 舊Raw bucket marker、Fetcher schedule及state格式不再自動轉換。

## 驗證矩陣

- Compose：Nginx使用literal版本與digest，所有production services可render。
- Fetcher：schedule、contract、CLI、Twelve Data／FinLab通用state（SQLite `PRAGMA user_version=3`）、
  Shioaji獨立state（`meta.schema_version=9`）、provider scheduler及deployment marker測試。
- Backend：scheduler control、Source ingress、Admin filter及association-only response測試。
- Migration：空DB、舊revision、不一致fixture及upgrade／downgrade／upgrade。
- Static／Nginx：舊route `404`、Dashboard與skill archive `200`、Referer規則只保留Dashboard。
- Deprecated tool：staging／production與promotion來源的歷史TW futures／option residues以
  exact-image dry-run零候選，現行Shioaji／market-minute的0050 `TW/etf`回歸測試通過；repo
  沒有active futures contract feed／normalizer，腳本及引用已移除。
- Repo：搜尋所有已移除symbol、config、route與檔名，執行Markdown link檢查及
  `git diff --check`。

## 文件與交付限制

- 每個PR記錄該波的preflight證據、exact image digest、測試、acceptance、停止條件及rollback
  決策；不得記錄credential、bucket ID或可比較的secret片段。
- 本計畫不修改API、runtime或workflow；後續實作PR才依波次變更。
- `docs/dev/staging-aws-deployment-plan.md`不屬於本計畫的修改範圍；若需同步，必須另行明確
  授權。
