# Instrument Name Backfill — 部署手冊

修正生產環境 `instruments` 表 `name` / `currency` 多數為空的問題，並順手把舊
routing bug 殘留的錯誤 asset_class 修掉。所有 script 預設 dry-run，加
`--apply` 才寫入。每個 script 都是 idempotent（只覆寫 NULL 欄位），可重複執行。

## 影響範圍與來源

| Script | 來源 | 涵蓋 | 預期覆蓋率 |
| --- | --- | --- | --- |
| `backfill_instrument_names.py` | TWSE/TPEX 公開 ISIN 名單 | TW equity + etf | ~92% |
| `fix_misrouted_tw_futures.py` | 無外部來源（純 reclassify） | 0050 重複 + 75 筆 TXF*/*F1 期貨代碼 | 100% |
| `backfill_world_names.py` | NASDAQ Trader / HKEX / Tencent / 內建 dict | US/HK/CN equity、FX、indices | ~99% |

## 前置條件

- script 程式碼必須已部署到 EC2（透過 main 分支推送 → CI build → 新 image）。確認方法：
  ```bash
  docker compose -f docker-compose.prod.yml run --rm raw-cleanup \
    ls /app/scripts/ | grep -E 'backfill_world_names|fix_misrouted_tw_futures|backfill_instrument_names'
  ```
- 容器內 `DATABASE_URL` 已指向 prod DB（生產 docker-compose.prod.yml 已設）。
- 容器內可以外送 HTTPS（curl）：TWSE、NASDAQ Trader、HKEX、Tencent 都需要對外連線。
- 回填 script 一律以 one-off container 執行，禁止 `docker exec findb-serve` 或 `docker exec findb-ingest`，避免搶 serve/ingest process 資源。

## 階段 A — TW 與 routing bug 修正

### 0. 備份 prod DB（強烈建議）

Prod DB 在 AWS Aurora/RDS，EC2 上沒有 postgres 容器；以下二擇一。

**方案 A — RDS/Aurora snapshot（推薦）**

```bash
# 先查 identifier
aws rds describe-db-clusters --query 'DBClusters[].DBClusterIdentifier' --output table
aws rds describe-db-instances --query 'DBInstances[].DBInstanceIdentifier' --output table

# Aurora cluster
aws rds create-db-cluster-snapshot \
  --db-cluster-identifier <your-cluster-id> \
  --db-cluster-snapshot-identifier "findb-pre-backfill-$(date -u +%Y%m%dT%H%M%SZ)"

# 或一般 RDS instance
aws rds create-db-snapshot \
  --db-instance-identifier <your-instance-id> \
  --db-snapshot-identifier "findb-pre-backfill-$(date -u +%Y%m%dT%H%M%SZ)"
```

**方案 B — 從 EC2 邏輯備份**

注意三個雷：

1. ``pg_dump`` 版本必須 ``≥`` server。Aurora 目前是 16.x，Ubuntu 22.04
   預設只有 14.x，要從 PGDG repo 裝 16。
2. DATABASE_URL 帶 SQLAlchemy 的 ``+asyncpg`` driver suffix，``pg_dump``
   不認得，要剝掉。
3. asyncpg 用 ``ssl=require`` query param，libpq 只認 ``sslmode=...``，
   要替換。

```bash
# 1) 裝 postgresql-client-16 (PGDG)
sudo install -d /usr/share/postgresql-common/pgdg
sudo curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
  -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc
. /etc/os-release
sudo sh -c "echo 'deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt $VERSION_CODENAME-pgdg main' > /etc/apt/sources.list.d/pgdg.list"
sudo apt-get update && sudo apt-get install -y postgresql-client-16

# 2) 從 findb-app 讀 DATABASE_URL、剝 +asyncpg、把 ssl= 改成 sslmode=
DB_URL=$(docker exec findb-serve printenv DATABASE_URL \
  | sed -E 's|\+asyncpg||; s/([?&])ssl=/\1sslmode=/')

# 3) 跑 pg_dump（單行避免續行符出包）
mkdir -p ~/backups
/usr/lib/postgresql/16/bin/pg_dump "$DB_URL" --table=instruments --table=market_data_eod --table=instrument_identifiers > "$HOME/backups/findb_$(date -u +%Y%m%dT%H%M%SZ).sql"
ls -lh ~/backups/
```

### 1. TW 名稱與 currency 補上

> **編碼說明（PR #79 後）**：TWSE / TPEX ISIN 頁面宣告為 `text/html;charset=MS950`，
> Python 對應 codec 為 `cp950`。早期 backfill 使用 `big5` + `errors="ignore"` 會默默丟掉
> Microsoft 自訂區字元（例如 `恒`、`凃`），名稱會整體後移一個 byte 導致亂碼。
> 目前 script 會讀 Response `Content-Type` 動態決定 codec，缺省 fallback 為 `cp950`。
> 跑新版前若 DB 已落地舊版 mojibake 名稱，請改用 `--overwrite-existing`。

```bash
# dry-run，確認預估更新筆數
docker compose -f docker-compose.prod.yml run --rm raw-cleanup python /app/scripts/backfill_instrument_names.py

# 預期輸出尾巴：would apply: name=2173 currency=2508 unmatched=334
# unmatched 應該都是 TXF*/期貨代碼 + 0050 重複 + 已下市股票

# 套用（只填入 NULL 名稱，已有名稱不動）
docker compose -f docker-compose.prod.yml run --rm raw-cleanup python /app/scripts/backfill_instrument_names.py --apply

# 若 DB 已有舊版 big5 解碼產生的亂碼，需強制覆寫
docker compose -f docker-compose.prod.yml run --rm raw-cleanup python /app/scripts/backfill_instrument_names.py --apply --overwrite-existing
```

> `--overwrite-existing` 只影響 `name` 欄位；`currency` 仍只在 NULL 時補入。
> 兩個旗標都是 idempotent，重複執行不會破壞已正確填入的資料。

### 2. 修正 routing bug 殘留

```bash
docker compose -f docker-compose.prod.yml run --rm raw-cleanup python /app/scripts/fix_misrouted_tw_futures.py

# 預期輸出：
#   0050 duplicate: deleting <uuid>
#     children removed: {market_data_eod: 241, ...}
#   reclassify candidates: 75
#   dry-run: no changes committed (moved=75)

docker compose -f docker-compose.prod.yml run --rm raw-cleanup python /app/scripts/fix_misrouted_tw_futures.py --apply
```

### 3. 重生靜態 cache

`/static/data/instruments.json` 由 cron 每日 06:00 重生（見 `scripts/generate_instrument_cache.py` 註解），手動補一次：

```bash
docker compose -f docker-compose.prod.yml run --rm ingest python /app/scripts/generate_instrument_cache.py
```

確認 cache 有新名稱：

```bash
docker exec findb-serve head -c 800 /app/app/static/data/instruments.json
```

## 階段 B — US / HK / CN / FX / indices

```bash
# 完整 dry-run
docker compose -f docker-compose.prod.yml run --rm raw-cleanup python /app/scripts/backfill_world_names.py

# 預期摘要（樣本，實際數字以執行為準）：
#   US/equity    scanned=530   name+=524   currency+=530  unmatched=6
#   HK/equity    scanned=90    name+=90    currency+=90   unmatched=0
#   CN/equity    scanned=2260  name+=2260  currency+=2260 unmatched=0
#   FX/fx        scanned=31    name+=31    currency+=0    unmatched=0
#   indices      scanned=49    name+=48    currency+=49   unmatched=1   (BM7T 故意未匹配)

# 套用全部市場
docker compose -f docker-compose.prod.yml run --rm raw-cleanup python /app/scripts/backfill_world_names.py --apply

# 也可以分市場跑：
docker compose -f docker-compose.prod.yml run --rm raw-cleanup python /app/scripts/backfill_world_names.py --markets us --apply
docker compose -f docker-compose.prod.yml run --rm raw-cleanup python /app/scripts/backfill_world_names.py --markets hk,cn --apply
```

跑完後再重生 cache：

```bash
docker compose -f docker-compose.prod.yml run --rm ingest python /app/scripts/generate_instrument_cache.py
```

打開 https://findb.tingfong.com/instrument-lookup 強制重新整理（Ctrl+Shift+R）即可看到所有市場的 NAME。

## 驗證 SQL

```sql
-- 每個 (market, asset_class) 的覆蓋率
SELECT market, asset_class,
       COUNT(*)            AS total,
       COUNT(name)         AS with_name,
       COUNT(currency)     AS with_currency,
       ROUND(100.0 * COUNT(name) / COUNT(*), 1) AS name_pct
FROM instruments
GROUP BY market, asset_class
ORDER BY market, asset_class;

-- 留下還沒匹配到名稱的清單（可進一步處理）
SELECT market, asset_class, symbol
FROM instruments
WHERE name IS NULL
ORDER BY market, asset_class, symbol;
```

## 已知未匹配（不在本次自動補名範圍）

- **US/equity** ~6 筆：`CTRA / HEXAB / KGX / SIE / TCS / TECHM` — 這些是德國、印度等海外股票被誤分類成 US/equity 的 routing bug 殘留，與 TW 期貨同類問題。需要另寫 reclassify。
- **US/index 1 筆**：`BM7T` — 來源未明，刻意未加入內建 dict。
- **TW/equity ~258 筆**：歷史下市股票（1101 以下、1107/1258/1262 等），不在現行 TWSE ISIN 表中。

## 回滾

每個 script 都只動 instrument 的 `name` / `currency` 兩欄（routing 修正會搬 market/asset_class，但不刪 EOD）。要回滾：

```sql
-- 全市場名稱回滾
UPDATE instruments SET name = NULL, currency = NULL WHERE updated_at >= '<your_start_timestamp>';
```

或從 step 0 的備份還原 ``instruments`` 表：

- 方案 A 的 RDS snapshot：用 ``aws rds restore-db-cluster-from-snapshot`` 還原到新 cluster，再從新 cluster ``pg_dump --table=instruments``，最後 restore 到 prod。
- 方案 B 的 pg_dump：``psql "$DB_URL" < ~/backups/findb_<timestamp>.sql``（會 drop & recreate 三張表，請小心其他被同時寫入的資料）。

`fix_misrouted_tw_futures.py` 對 0050 的刪除是不可逆的，必須從 step 0 的備份還原。
