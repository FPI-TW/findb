# Cloudflare 後方的 nginx Source API Allowlist 事故筆記

## 背景

FinDB 的 Source API 用來接收資料寫入請求，因此除了 API key 外，也需要限制來源 IP。

一開始，Source API 的 IP allowlist 是在 FastAPI 層透過 `SOURCE_ALLOWLIST_CIDRS` 判斷。後來在 commit `8171420`：

```text
[YHY-1] refactor: 將 Source API IP allowlist 移至 nginx (#45)
```

將 Source API 的 IP allowlist 移到 nginx。部署流程會根據 GitHub Actions Variable `SOURCE_ALLOWLIST_CIDRS` 產生：

```text
/home/ubuntu/etc/nginx/source-allowlist.conf
```

nginx container 內則掛載為：

```text
/etc/nginx/source-allowlist.conf
```

實際內容類似：

```nginx
# Generated during deployment from SOURCE_ALLOWLIST_CIDRS.
# Only /api/v1/source/* includes this file.
allow 10.0.0.0/8;
allow 118.163.3.173/32;
deny all;
```

因此，最新架構下 `SOURCE_ALLOWLIST_CIDRS` 不會再出現在 `findb-app` container 的 environment variables 裡。這是正常的，因為 allowlist 已經改由 nginx 執行。

## 現象

GitHub Variables 已經設定：

```env
SOURCE_ALLOWLIST_CIDRS=10.0.0.0/8,118.163.3.173/32
```

EC2 上也確認 nginx allowlist 檔案正確產生：

```bash
cat /home/ubuntu/etc/nginx/source-allowlist.conf
docker exec findb-nginx cat /etc/nginx/source-allowlist.conf
```

輸出：

```nginx
# Generated during deployment from SOURCE_ALLOWLIST_CIDRS.
# Only /api/v1/source/* includes this file.
allow 10.0.0.0/8;
allow 118.163.3.173/32;
deny all;
```

但是從允許的來源 IP `118.163.3.173` 呼叫 Source API 時，nginx 仍然回 `403`。

nginx log 顯示：

```text
2026/04/28 06:42:05 [error] 21#21: *19 access forbidden by rule, client: 172.69.176.98, server: findb.tingfong.com, request: "GET /api/v1/source/ingest/usstock/direct HTTP/1.1", host: "findb.tingfong.com"
172.69.176.98 - - [28/Apr/2026:06:42:05 +0000] "GET /api/v1/source/ingest/usstock/direct HTTP/1.1" 403 49 "-" "Mozilla/5.0 ..." "118.163.3.173"
```

最關鍵的是這兩個 IP：

```text
client: 172.69.176.98
```

以及 access log 最後一欄：

```text
"118.163.3.173"
```

## 排查過程

第一個直覺是檢查 app container 有沒有吃到 `SOURCE_ALLOWLIST_CIDRS`：

```bash
docker exec findb-app env | grep SOURCE
```

結果只看到：

```env
SOURCE_TRUST_PROXY_HEADERS=false
SOURCE_API_KEYS=...
```

一開始看起來像是 GitHub Variables 沒有注入成功，但後來確認最新 commit 已經把 Source API allowlist 從 FastAPI 移到 nginx，因此 app container 裡沒有 `SOURCE_ALLOWLIST_CIDRS` 是預期行為。

正確的檢查目標應該是 nginx：

```bash
cat /home/ubuntu/etc/nginx/source-allowlist.conf
docker exec findb-nginx cat /etc/nginx/source-allowlist.conf
```

這兩個檔案都正確，表示 GitHub Variable 有成功渲染成 nginx allowlist。

接著看 nginx log，才發現 nginx 實際用來做 `allow` / `deny` 判斷的 client IP 是 `172.69.176.98`，而不是預期中的 `118.163.3.173`。

## Root Cause

服務前面有 Cloudflare proxy。

請求路徑實際上是：

```text
Client 118.163.3.173
  -> Cloudflare 172.69.176.98
  -> EC2 nginx
  -> FastAPI app
```

對 nginx 來說，直接連進來的是 Cloudflare edge，因此 `$remote_addr` 會是：

```text
172.69.176.98
```

而不是使用者真實 IP：

```text
118.163.3.173
```

所以 nginx 的 allowlist 實際上是在拿 Cloudflare IP 去比對：

```nginx
allow 118.163.3.173/32;
deny all;
```

結果合法來源 IP 仍然被拒絕。

## 修正方式

在 nginx 加上 Cloudflare real IP 設定，讓 nginx 信任 Cloudflare proxy，並從 `CF-Connecting-IP` 還原真實 client IP。

修正位置是 repo 裡的：

```text
infra/nginx/nginx.conf
```

部署後會同步到 EC2：

```text
/home/ubuntu/etc/nginx/nginx.conf
```

nginx container 內則是：

```text
/etc/nginx/conf.d/default.conf
```

設定如下：

```nginx
real_ip_header CF-Connecting-IP;
real_ip_recursive on;

set_real_ip_from 103.21.244.0/22;
set_real_ip_from 103.22.200.0/22;
set_real_ip_from 103.31.4.0/22;
set_real_ip_from 104.16.0.0/13;
set_real_ip_from 104.24.0.0/14;
set_real_ip_from 108.162.192.0/18;
set_real_ip_from 131.0.72.0/22;
set_real_ip_from 141.101.64.0/18;
set_real_ip_from 162.158.0.0/15;
set_real_ip_from 172.64.0.0/13;
set_real_ip_from 173.245.48.0/20;
set_real_ip_from 188.114.96.0/20;
set_real_ip_from 190.93.240.0/20;
set_real_ip_from 197.234.240.0/22;
set_real_ip_from 198.41.128.0/17;

set_real_ip_from 2400:cb00::/32;
set_real_ip_from 2606:4700::/32;
set_real_ip_from 2803:f800::/32;
set_real_ip_from 2405:b500::/32;
set_real_ip_from 2405:8100::/32;
set_real_ip_from 2a06:98c0::/29;
set_real_ip_from 2c0f:f248::/32;
```

這段要放在 `server { ... }` 裡，並且在 `location` 規則之前。

修正後，nginx 會把 `$remote_addr` 還原成 `CF-Connecting-IP` 裡的真實來源 IP。如此一來：

```nginx
allow 118.163.3.173/32;
deny all;
```

才會拿 `118.163.3.173` 進行比對，而不是拿 Cloudflare edge IP 比對。

## 驗證方式

先確認 nginx allowlist：

```bash
docker exec findb-nginx cat /etc/nginx/source-allowlist.conf
```

確認 nginx 主設定：

```bash
docker exec findb-nginx cat /etc/nginx/conf.d/default.conf
```

測試 nginx 設定：

```bash
docker exec findb-nginx nginx -t
```

reload nginx：

```bash
docker exec findb-nginx nginx -s reload
```

再次呼叫 Source API，然後看 nginx log：

```bash
docker logs findb-nginx --tail=50
```

修正前會看到：

```text
client: 172.69.176.98
```

修正後應該要看到：

```text
client: 118.163.3.173
```

## 額外注意

這次測試時打到的路徑是：

```text
GET /api/v1/source/ingest/usstock/direct
```

但這個 API endpoint 在 FastAPI 裡是 `POST`。因此 allowlist 修好後，如果仍然用 `GET` 測試，預期會從 nginx 的 `403` 變成 FastAPI 的 `405 Method Not Allowed`。這反而代表請求已經通過 nginx allowlist，成功到達 app。

## PR

本次修正 PR：

```text
https://github.com/FPI-TW/findb/pull/47
```

commit：

```text
7733ac3 fix(infra): trust Cloudflare client IPs in nginx
```

## 學到的事

如果 nginx 放在 Cloudflare 後方，不能直接用 `$remote_addr` 做 client IP allowlist。

在這種架構下，`$remote_addr` 預設會是 Cloudflare edge IP，而不是使用者真實 IP。若要在 nginx 做 IP allowlist，需要：

1. 信任 Cloudflare 官方 proxy IP ranges。
2. 使用 `real_ip_header CF-Connecting-IP;` 還原真實來源 IP。
3. 確認 allowlist 的比對發生在 real IP 還原之後。

否則會出現一個很容易誤判的狀況：GitHub Variables 正確、nginx allowlist 檔案正確，但合法來源仍然被 nginx 擋掉。
