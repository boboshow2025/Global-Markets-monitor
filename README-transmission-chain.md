# 傳導鏈監控模組

在既有的 `index.html` 上加一塊：六個節點的訊號燈 + 滾動相關係數。
TradingView widget 只負責看圖，這塊負責回答「現在該不該進場」。

## 檔案

| 檔案 | 放哪 | 做什麼 |
|---|---|---|
| `market_corr.py` | 專案根目錄 | 抓資料、算相關、寫 `data/correlations.json` |
| `run_market_corr.bat` | 專案根目錄 | Task Scheduler 排程用，含自動 push |
| `monitor_module.html` | 貼進 `index.html` | 前端顯示，無外部相依 |

## 安裝

```bat
pip install yfinance pandas numpy
python market_corr.py --offline-test    :: 先用合成資料確認流程通
python market_corr.py                   :: 抓真實資料
```

`--offline-test` 會造一組有已知結構的假資料（費半→台股刻意埋了一日落後相關）。
如果跑出來的 `SOX→TAIEX` 隔夜係數接近 0.78、同日接近 0，代表計算邏輯正常。

## 接上網頁

1. 把 `monitor_module.html` 整段內容貼進 `index.html`，位置建議在時鐘列下面、TradingView 區塊上面。
2. 深色底的話，把 `<section class="tx">` 改成 `<section class="tx is-dark">`。
3. 資料路徑不同就改 `data-src`。
4. 本機測試要開 server，直接雙擊開檔會被 CORS 擋掉：
   ```bat
   python -m http.server 8000
   ```

## 排程

Task Scheduler 新增工作 → 觸發程序：每週二至週六 07:30 → 動作：執行 `run_market_corr.bat`。
選週二到週六是因為要涵蓋前一夜的美股收盤，週一台北早上美股還沒開過。

`AUTO_PUSH=1` 會自動 commit 並推上 GitHub Pages。不想自動推就設 `0`。

## 六個節點的判讀規則

| 節點 | 看什麼 | 留意 | 警戒 |
|---|---|---|---|
| 布蘭特原油 | 5 日變動 | +2.5% | +5% |
| 美10年殖利率 | 單日變動 | +5bp | +8bp |
| 美元指數 | 絕對水位 | 99.5 | 100.5 |
| 美元/日圓 | 5 日變動 | −1.5% | −3% |
| 費城半導體 | 單日漲跌 | −0.8% | −2% |
| 台股加權 | 與台幣是否同向 | 背離 | — |

日圓那格看的是**速度不是方向**：緩貶是常態，急升才是套利平倉的警訊。
急貶超過 3% 也會亮黃燈，因為那是干預風險。

台股那格不是看漲跌，是看**台幣有沒有跟上**。台股漲但台幣不升，代表錢不是外資的，
這種反彈通常撐不久。

門檻全部集中在 `market_corr.py` 的 `THRESHOLDS`，要調自己改。

## 兩個要知道的限制

**相關係數會失效。** 這正是折線圖存在的理由——不是拿來確認規律成立，是拿來抓規律什麼時候不成立。
線在零軸附近亂跳的配對，此刻沒有交易價值，不要硬套。

**目前少了外資買賣超。** 「股匯同向」那格現在用台幣漲跌當代理變數。
真正的確認訊號是外資買賣超金額，但證交所那份資料要另外爬。要接的話再說。

## 資料來源

yfinance 為主，Stooq CSV 為備援，兩者都免費免金鑰。
yfinance 偶爾會因為 Yahoo 改介面而壞掉，屆時腳本會自動 fallback，
主控台會印 `[stooq] 補上 XXX`。兩邊都失敗才會 exit 1，並保留上一版 JSON 不覆蓋。
