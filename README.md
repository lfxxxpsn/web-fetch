# web-fetch

本儲存庫專責取得與保存目標網站網頁：處理低頻率 HTTP 請求、回應驗證、原始 HTML 保存與抓取診斷。網頁內容只保存完整 HTML，後續表格提取、資料清洗、資料庫與分析可在各自的儲存庫處理。

此目錄是獨立的 Git 儲存庫，與其他專案分別存放於同一父目錄。

提供 GitHub Actions 單頁測試、三場試跑與整個賽事日的手動分批備份，結果可保存至 GitHub Releases。

目標網址僅從 `TARGET_URL` 環境變數讀取。GitHub Actions 使用同名 Repository Secret 注入；程式、workflow 與文件都不保存實際目標網址。

## 請求行為

- 每次實際抓取先等待 1 秒，且只發出一次 GET。
- 不自動重試、不跟隨重新導向、不執行 JavaScript，也不下載圖片、CSS 或其他資源。
- Workflow 使用固定 concurrency group，避免這個儲存庫的測試工作重疊。
- 403、429、其他非 200 回應或缺少賽事表格時，以失敗狀態結束；429 的 Retry-After 會記入報告，本次不重試。
- 使用已實測成功的瀏覽器相容 HTTP 標頭，包含 User-Agent、Accept、Accept-Language 與導覽用途標頭。仍由 Python 發出請求，沒有啟動瀏覽器、登入或使用 Cookie。
- 接受 gzip／deflate 並在保存前解壓；傳輸及解壓後大小各自限制為 8 MiB，避免把壓縮資料誤存為 HTML。

## 本機檢查（不連線到目標網站）

需要 Python 3.14。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy fetch_page.py archive_capture.py batch_capture.py
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m compileall -q fetch_page.py archive_capture.py batch_capture.py tests
```

單元測試使用假回應；重新導向測試只連線到本機 loopback HTTP server，不接觸目標網站。這是腳本專案，沒有另行打包的 build 步驟；compileall 檢查 Python 語法編譯。

## GitHub Actions 手動測試

先在 GitHub 儲存庫的 **Settings → Secrets and variables → Actions → New repository secret** 建立：

- Name：`TARGET_URL`
- Secret：完整目標 HTTPS 網址（不含登入帳密與 fragment）。

將檔案提交到 GitHub 儲存庫的預設分支後，在 Actions 選擇 **Web page fetch test → Run workflow**。

Secret 只注入實際抓取的步驟；離線檢查不需要 Secret。未設定、空值或格式不合規時，程式立即回報設定錯誤，不等待、不請求網站。網址不作為命令列參數傳遞。

此工作區尚未連接 GitHub 遠端，因此目前已完成程式與 workflow 的 Secret 讀取設定，尚未在 GitHub 建立實際 Secret。設定方式參閱 [GitHub 官方說明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)。

- `live_probe=false`（預設）：僅執行 lint、format、型別、離線測試與編譯檢查。
- `live_probe=true`：上述檢查通過後，等待 1 秒並實際請求一頁，成功後打包並發布至 Releases。

成功或被網站拒絕時，都可在該次執行的 Artifacts 下載診斷結果，保留 14 天。若前置檢查失敗，尚未執行抓取，便不會產生抓取報告。

## 賽事批次備份

在 **Actions → Race batch pilot → Run workflow** 輸入一個已結束的賽事日（`race_day`，格式 `YYYY-MM-DD`）。程式讀取站方 robots.txt、主 sitemap 和賽事子清單。每場保存賽事頁與頁內實際連結的正式賽果頁。單頁與批次共用 workflow 的 concurrency group，網站請求逐一執行，回應完成後至少間隔 1 秒。

- `mode=pilot` 選三場。測試續跑時先將 `pause_after` 設為 `1`，保存一頁後暫停。第二次用相同日期，填入前一次 Release tag，將 `pause_after` 設為 `6` 以補齊。
- `mode=day-list` 列出當天所有 sitemap 賽事並固定清單。可在 `resume_tag` 指定同日的三場試跑 Release，驗證後沿用已保存的 HTML。此模式只發送盤點請求。
- `mode=race-day` 以清單為準，每次處理最多十場尚未完成的賽事。下一次用同一天和最新的 `pilot-<run_id>-<run_attempt>` tag 續跑。若報告還有 pending，繼續執行下一批。

每份 `batch.zip` 可獨立還原。新的 runner 下載並核對 ZIP 與逐頁雜湊，再跳過已保存的 HTML。各 HTML 檔名由相對路徑的 SHA-256 產生，`checkpoint.json` 記錄賽事清單、相對路徑、雜湊、缺頁原因與本次網站請求數。遇缺少正式結果連結、404 或 410 時，整日模式會列明原因並接著處理其他賽事。最終每個預期頁面都有 HTML 或缺頁原因，狀態會標成 `complete` 或 `complete_with_missing`。

遇到 403 或 429 會停止該批次，能發布時先保留已保存的頁面和狀態，再將 Actions 標為失敗。403 的進度會拒絕續跑；429 會記下 Retry-After，缺少有效值時等一小時。續跑以已發布的進度為準，runner 若在 ZIP 上傳前被強制終止，該次尚未持久化的頁面需再抓一次。

## GitHub Releases 長期備份

每次成功抓取建立一個獨立 Release，tag 為 `capture-<run_id>-<run_attempt>`。重新執行整個 workflow 會使用新的 attempt，不覆蓋舊備份；每個 Release 只有兩個自行上傳的附件：

- `snapshot.zip`：完整 `page.html`、診斷 `report.json`，以及逐檔大小與 SHA-256 的 `manifest.json`。
- `snapshot.zip.sha256`：整個 ZIP 的 SHA-256，可在下載後確認完整性。

打包前檢查成功狀態、HTML 大小與雜湊。只從指定目錄讀取 `page.html` 與 `report.json`，所以有 `tables.json` 的舊抓取目錄也可用相同方式打包。每個附件必須小於 2 GiB，超限會中止，不自動分卷。

抓取 job 只有 `contents: read`。獨立備份 job 取得 Artifact 後再次核對 ZIP 雜湊及大小，使用內建 `GITHUB_TOKEN` 與 `contents: write` 建立 Release，不需要額外 PAT Secret。先建立草稿並上傳附件，完成後才發布；不標成 Latest。tag 指向該次 workflow 的程式 commit，不把下載內容提交進 Git。

抓取或打包失敗不會發布 Release；發布失敗會讓 workflow 失敗，已上傳的 Artifact 仍保留 14 天供取回。上傳中斷可能留下草稿，需手動檢查後處理；相同 tag 不會自動覆寫。若只重跑失敗的備份 job，會沿用原抓取 Artifact 並以新的 attempt 建立 Release。

Releases 不使用 Artifact 的 14 天保留設定，本 workflow 不會定期刪除備份。實際保留仍取決於儲存庫與 Release 是否存在及 GitHub 服務政策；不是不可刪除或永久保存的保證。官方目前規定每個 Release 最多 1,000 個附件、每個檔案小於 2 GiB，未另設 Release 總大小或頻寬上限，見 [GitHub Releases 說明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。

備份內容的可見性跟隨儲存庫：公開儲存庫的 Release 附件可被公開下載。若不希望原始 HTML 中的來源連結公開，請使用私人儲存庫；`TARGET_URL` Secret 不會替 HTML 去除連結。

本機也可打包已成功保存的目錄，不會重新請求網站或上傳 GitHub：

```powershell
python archive_capture.py "artifacts/<成功抓取目錄>"
```

下載 Release 的兩個附件至同一資料夾後，可在 PowerShell 驗證並解壓：

```powershell
$expectedHash = ((Get-Content .\snapshot.zip.sha256 -Raw).Trim() -split '\s+')[0]
$actualHash = (Get-FileHash .\snapshot.zip -Algorithm SHA256).Hash
if ($actualHash -ne $expectedHash) { throw '備份校驗失敗' }
Expand-Archive .\snapshot.zip -DestinationPath .\restored
```

Linux 可執行 `sha256sum --check snapshot.zip.sha256` 後再解壓。ZIP 內的 `manifest.json` 可用於核對各個解壓檔案。不要把 GitHub 自動生成的 Source code 壓縮檔誤認為網頁備份；網頁資料在自行上傳的 `snapshot.zip`。

## 本機抓取

手動執行同樣的實際請求：

```powershell
# 使用 PowerShell 7.1 以上；輸入值不會顯示在畫面或寫入命令歷史。
$env:TARGET_URL = Read-Host "輸入目標 HTTPS 網址" -MaskInput
try {
    python fetch_page.py
} finally {
    Remove-Item Env:\TARGET_URL
}
```

請勿同時從多個本機程序或不同儲存庫執行。腳本不實作跨程序／跨儲存庫鎖；GitHub concurrency 只涵蓋本儲存庫的 workflow。需要更長等待時可傳入 `--delay-seconds 180`，無法低於 1 秒。

## 產出

每次執行使用獨立的 `artifacts/<UTC時間>-<隨機ID>/`，不覆蓋先前成功結果。

- `report.json`：成功／失敗、時間、HTTP 狀態、傳輸與解壓後大小、壓縮方式、請求設定版本與 HTML SHA-256；網路失敗可能沒有 HTTP 狀態。新報告不記錄目標網址或重新導向網址，網路錯誤採固定訊息。
- `page.html`：僅在狀態碼、HTML 類型與賽事表格標記通過驗證後保存 HTML 位元組；若 HTTP 回應有壓縮，先解壓再保存，不修改 HTML 內容。
報告保留表格數量供診斷。完整 HTML 可供離線重新解析表格，新抓取結果不再產生 `tables.json`。

驗證要求可見內容含「出走馬」，且至少一張表格有兩列以上並含「馬名」「オッズ」。這只能排除常見錯誤／驗證頁，無法保證所有欄位完整或站方未改版。只有 script 內含這些字串不會被視為成功。

## 已知限制與先前證據

2026-11-16 的先前本機測試使用簡化標頭收到 403；僅改 User-Agent 或等待 90 秒也沒有成功。使用者確認同一電腦的 Chrome 可正常開啟後，完整瀏覽器相容標頭的單次診斷請求取得 HTTP 200，解壓後 364,672 bytes。既有解析器從這份真實 HTML 取得 30 張表格、484 列，其中出走馬表格含表頭共 14 列。

本次修正採用成功的標頭設定並新增受大小限制的 HTTP 解壓處理。尚未逐一隔離站方檢查哪個標頭，也不能排除站方封鎖狀態隨時間改變；本機成功不代表所有來源 IP 或 GitHub runner 都一定成功。更換標頭不會觸發額外重試。

同日曾檢查目標網站的 robots.txt，當時未列出禁止目標路徑。本腳本只測試指定賽事頁，不會額外抓取 robots.txt；擴充排程或批次抓取前需重新確認站方規則。延長間隔不保證解除 403；本工具不會繞過網站存取檢查。

尚未配置 GitHub 遠端、提交、推送或啟用排程。Releases 備份流程已寫入 workflow，實際 GitHub 上傳仍待遠端就緒後驗證；Artifacts 僅供短期診斷及備份失敗時取回。

Actions 使用官方 [checkout](https://github.com/actions/checkout)、[setup-python](https://github.com/actions/setup-python)、[upload-artifact](https://github.com/actions/upload-artifact) 與 [download-artifact](https://github.com/actions/download-artifact)，並透過 [GitHub CLI](https://cli.github.com/manual/gh_release_create) 發布備份。

## 修正後完整實測

2026-11-16 日本時間 23:58，當時版本的抓取程式等待 90 秒、發出一次 GET 後，以退出碼 0 完成。HTTP 200，HTML 共 364672 bytes，保存 30 張表格、484 列；出走馬表格含表頭共 14 列。已核對 SHA-256、位元組大小，並重新解析存檔確認與表格 JSON 完全一致。24 項離線測試、lint、型別與語法編譯檢查通過。這是本機端到端實測；GitHub runner 尚未執行。

## Secret 與保存內容

程式只從環境變數讀取目標，沒有預設網址或 `--url` 參數；本機 `.env` 類檔案已加入 Git 忽略規則，但程式不會自動載入這些檔案。

原始 HTML 和舊版存檔的表格仍可能含來源連結；GitHub Secret 不會自動清除下載內容。既有本機抓取結果保持原樣，舊診斷報告也可能包含先前網址；`artifacts/` 已被 Git 忽略，不會隨一般提交納入儲存庫。

## 設定移轉驗證

2026-11-17：完成名稱與 Secret 設定調整，31 項離線測試、lint、型別與語法編譯檢查通過。已確認 workflow 只在抓取步驟注入 `TARGET_URL`，未設定時 CLI 以退出碼 2 結束且不建立抓取報告。可提交檔案已通過命名檢查，歷史成功抓取的 HTML 雜湊保持一致。本次沒有發出網站請求，也沒有在 GitHub 建立 Secret、提交或推送。

## Releases 備份驗證

2026-11-17：42 項離線測試、lint、格式、兩個程式模組的型別及語法編譯檢查通過。另以既有成功抓取資料進行打包及還原：HTML 364,672 bytes、30 張表格，ZIP 64,378 bytes；解壓後三個來源檔案逐位元組一致，ZIP 與各檔案的 SHA-256 均通過校驗。

Workflow YAML 與 Bash 語法通過本機檢查；以假的 GitHub CLI 執行發布腳本，確認成功時先上傳草稿再發布，上傳失敗不發布、校驗失敗或超過大小限制不呼叫 GitHub CLI。這些是離線驗證，尚未完成真實 GitHub Actions／Releases 上傳實測；沒有新增目標網站請求、commit 或 push。
