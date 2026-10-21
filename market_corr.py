# -*- coding: utf-8 -*-
"""
market_corr.py  --  Global-Markets-monitor 開盤前訊號產生器（v2）

只回答一個問題：台股開盤前，要不要調整部位？

核心訊號（決定總燈號）
  1. 費城半導體隔夜漲跌      —— 與台股隔日報酬相關最高的單一變數
  2. 台指期夜盤漲跌（選配）  —— 抓得到就優先用，它已經消化了美股
  3. 外資現貨買賣超          —— 確認用，不單獨決定燈號

背景（只顯示，不進總燈號）
  油價 5 日、美10年殖利率單日、美元指數 5 日、美元/日圓 5 日、布蘭特−WTI 價差

所有門檻都是「跟自己過去 60 個交易日比」的 z 分數，不再用固定水位，
所以市場換了區間也不會一直亮紅燈。

另外會：
  - 用近兩年資料回測同一套規則，算出各燈號之後台股隔日的實際表現
  - 每天把燈號寫進 data/signal_log.csv，等台股收盤後回填結果，累積實戰命中率
  - 沒有新資料（週末、假日）就不重寫 JSON，排程也就不會產生空 commit

資料來源（皆免費免金鑰）
  台股加權指數、外資買賣超：證交所（官方，當日收盤後即更新）
  台指期夜盤：期交所 OpenAPI
  其餘：yfinance 為主，Stooq 備援

用法：
    python market_corr.py
    python market_corr.py --force          # 就算沒有新資料也重寫
    python market_corr.py --offline-test   # 合成資料，驗證流程
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

TPE = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

# key, 中文名, yfinance, stooq, 型別
MARKET = [
    ("SOX",    "費城半導體",   "^SOX",     "^sox",    "price"),
    ("BRENT",  "布蘭特原油",   "BZ=F",     "cb.f",    "price"),
    ("WTI",    "西德州原油",   "CL=F",     "cl.f",    "price"),
    ("US10Y",  "美10年殖利率", "^TNX",     "10usy.b", "yield"),
    ("DXY",    "美元指數",     "DX-Y.NYB", "dx.f",    "price"),
    ("USDJPY", "美元/日圓",    "JPY=X",    "usdjpy",  "price"),
    ("USDTWD", "美元/台幣",    "TWD=X",    "usdtwd",  "price"),
    ("TWII",   "台股（Yahoo 備援）", "^TWII", "^twse", "price"),
]

LOOKBACK_PERIOD = "2y"
Z_WINDOW = 60

# 總燈號門檻（z 分數；負值＝利空）
CORE_Z = {"watch": -1.0, "alert": -2.0, "up": 1.0}
# 背景項目：|z| 超過多少算「異常」
BG_Z = {"watch": 1.5, "alert": 2.5}

STATE_TEXT = {
    "alert": "強烈偏空",
    "watch": "偏空",
    "calm":  "中性",
    "up":    "偏多",
    "unknown": "無資料",
}
ACTION_TEXT = {
    "alert": "開盤不追價，考慮先減碼或避險",
    "watch": "保守操作，新倉等盤中確認",
    "calm":  "照原計畫",
    "up":    "環境有利，可依計畫進場",
    "unknown": "資料不足，不做判讀",
}

DATA_DIR = Path("data")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _r(x, nd=3):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, nd)


def _num(s) -> float | None:
    if s is None:
        return None
    s = str(s).replace(",", "").replace("+", "").strip()
    if s in ("", "-", "--", "X"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _ssl_context() -> ssl.SSLContext:
    """證交所憑證缺 Subject Key Identifier，Python 3.13 起的嚴格模式會拒絕。
    這裡只關掉 X509_STRICT 這一項額外檢查，憑證鏈與主機名稱照常驗證。"""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


_SSL = _ssl_context()


def _get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return resp.read()


def _roc_to_date(s: str) -> date:
    y, m, d = s.strip().split("/")
    return date(int(y) + 1911, int(m), int(d))


def zscore_last(series: pd.Series, window: int = Z_WINDOW) -> float | None:
    """最後一筆相對於「它之前 window 筆」的 z 分數（不含自己，避免稀釋）。"""
    s = series.dropna()
    if len(s) < window // 2 + 1:
        return None
    hist = s.iloc[-window - 1:-1]
    sd = hist.std()
    if not sd or math.isnan(sd):
        return None
    return float((s.iloc[-1] - hist.mean()) / sd)


def rolling_z(series: pd.Series, window: int = Z_WINDOW) -> pd.Series:
    s = series.dropna()
    mu = s.shift(1).rolling(window, min_periods=window // 2).mean()
    sd = s.shift(1).rolling(window, min_periods=window // 2).std()
    return (s - mu) / sd


# --------------------------------------------------------------------------
# 抓資料：證交所
# --------------------------------------------------------------------------

def _month_starts(n_months: int) -> list[date]:
    today = datetime.now(TPE).date()
    out, y, m = [], today.year, today.month
    for _ in range(n_months):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def fetch_taiex_twse(cache: Path, verbose: bool) -> pd.Series:
    """證交所 FMTQIK：每月一包，含每日加權指數收盤。快取後每次只補最近兩個月。"""
    old = pd.Series(dtype=float)
    if cache.exists():
        df = pd.read_csv(cache, parse_dates=["date"])
        old = df.set_index("date")["close"]
    months = _month_starts(2 if len(old) > 300 else 25)
    got = {}
    fails = 0
    for i, ms in enumerate(months):
        if fails >= 3:
            if verbose:
                print("[twse] 加權指數連續失敗 3 次，這次先跳過")
            break
        url = ("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json&date="
               + ms.strftime("%Y%m%d"))
        fails += 1
        try:
            j = json.loads(_get(url).decode("utf-8"))
            fails = 0
            if j.get("stat") != "OK":
                continue
            fi = j["fields"].index("發行量加權股價指數")
            for row in j.get("data", []):
                v = _num(row[fi])
                if v:
                    got[pd.Timestamp(_roc_to_date(row[0]))] = v
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"[twse] {ms:%Y-%m} 失敗：{exc}")
        if i < len(months) - 1:
            time.sleep(1.2)   # 證交所會擋太密集的請求
    parts = [x for x in (old, pd.Series(got, dtype=float)) if len(x)]
    s = pd.concat(parts) if parts else pd.Series(dtype=float)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if len(s):
        cache.parent.mkdir(parents=True, exist_ok=True)
        pd.DataFrame({"date": s.index.strftime("%Y-%m-%d"), "close": s.values}).to_csv(cache, index=False)
    if verbose:
        print(f"[twse] 加權指數 {len(s)} 筆，最新 {s.index[-1]:%Y-%m-%d} = {s.iloc[-1]:,.2f}" if len(s)
              else "[twse] 加權指數抓不到")
    return s


def fetch_foreign_flow(cache: Path, tw_dates: list[pd.Timestamp], verbose: bool) -> pd.Series:
    """證交所 BFI82U：外資買賣差額＝「外資及陸資(不含外資自營商)」＋「外資自營商」，單位：億元。
    與新聞、期貨商盤後快報的「外資買賣超」口徑相同（三大法人合計＝自營商＋投信＋這個數字）。
    舊快取只含「不含外資自營商」那一列（欄名 net_100m），偵測到就整批重抓。"""
    old = pd.Series(dtype=float)
    if cache.exists():
        df = pd.read_csv(cache, parse_dates=["date"])
        if "net_100m_all" in df.columns:
            old = df.set_index("date")["net_100m_all"]
    want = [d for d in tw_dates[-30:] if d not in old.index]
    got = {}
    fails = 0
    for i, d in enumerate(want):
        if fails >= 3:
            if verbose:
                print("[twse] 外資連續失敗 3 次，這次先跳過")
            break
        url = ("https://www.twse.com.tw/rwd/zh/fund/BFI82U?type=day&response=json&dayDate="
               + d.strftime("%Y%m%d"))
        fails += 1
        try:
            j = json.loads(_get(url).decode("utf-8"))
            fails = 0
            if j.get("stat") == "OK":
                vals = [_num(row[3]) for row in j.get("data", [])
                        if str(row[0]).strip().startswith(("外資及陸資", "外資自營商"))]
                vals = [v for v in vals if v is not None]
                if vals:
                    got[d] = sum(vals) / 1e8
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"[twse] 外資 {d:%Y-%m-%d} 失敗：{exc}")
        if i < len(want) - 1:
            time.sleep(1.2)
    parts = [x for x in (old, pd.Series(got, dtype=float)) if len(x)]
    s = pd.concat(parts) if parts else pd.Series(dtype=float)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    if len(s):
        pd.DataFrame({"date": s.index.strftime("%Y-%m-%d"), "net_100m_all": s.round(2).values}).to_csv(cache, index=False)
    if verbose:
        print(f"[twse] 外資買賣超 {len(s)} 筆" + (f"，最新 {s.index[-1]:%Y-%m-%d} {s.iloc[-1]:+.1f} 億" if len(s) else ""))
    return s


# --------------------------------------------------------------------------
# 抓資料：期交所夜盤
# --------------------------------------------------------------------------

def fetch_tx_night(verbose: bool) -> dict | None:
    """期交所 OpenAPI 每日行情。取台指期（TX）近月，比較盤後時段 vs 一般時段。
    欄位名稱以關鍵字比對，抓不到就回 None，不影響其他訊號。"""
    try:
        raw = _get("https://openapi.taifex.com.tw/v1/DailyMarketReportFut", timeout=40)
        if raw[:2] == b"\x1f\x8b":
            import gzip
            raw = gzip.decompress(raw)
        rows = json.loads(raw.decode("utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"[taifex] 夜盤抓不到：{exc}")
        return None

    def key(rec, *words):
        for k in rec:
            if all(w.lower() in k.lower() for w in words):
                return k
        return None

    if not rows:
        return None
    r0 = rows[0]
    k_con, k_mon = key(r0, "contract"), key(r0, "month")
    k_last, k_ses = key(r0, "last"), key(r0, "session")
    k_date, k_set = key(r0, "date"), key(r0, "settlement")
    if not all([k_con, k_mon, k_last, k_ses, k_date]):
        if verbose:
            print(f"[taifex] 欄位對不上，實際欄位：{list(r0.keys())}")
        return None

    tx = [r for r in rows if str(r.get(k_con, "")).strip() == "TX"
          and "/" not in str(r.get(k_mon, ""))]
    if not tx:
        return None
    near = min(str(r[k_mon]).strip() for r in tx)
    tx = [r for r in tx if str(r[k_mon]).strip() == near]

    def is_night(r):
        s = str(r.get(k_ses, ""))
        return ("盤後" in s) or ("after" in s.lower())

    night = [r for r in tx if is_night(r)]
    day = [r for r in tx if not is_night(r)]
    if not night or not day:
        if verbose:
            print(f"[taifex] 找不到夜盤或日盤（時段值：{sorted({str(r.get(k_ses)) for r in tx})}）")
        return None
    n, d = night[0], day[0]
    n_last = _num(n.get(k_last))
    d_ref = _num(d.get(k_set)) if k_set else None
    d_ref = d_ref or _num(d.get(k_last))
    if not n_last or not d_ref:
        return None
    out = {
        "date": str(n.get(k_date)), "day_date": str(d.get(k_date)), "month": near,
        "last": n_last, "ref": d_ref, "pct": (n_last / d_ref - 1) * 100,
    }
    if verbose:
        print(f"[taifex] 夜盤 {near} {n_last:,.0f}（日盤結算 {d_ref:,.0f}）{out['pct']:+.2f}%")
    return out


def fetch_tx_night_csv(verbose: bool) -> dict | None:
    """期交所「期貨每日交易行情下載」CSV（futDataDown），夜盤收盤（05:00）後就有『盤後』那一列，
    比 OpenAPI（下午才更新）早得多。日期是『交易歸屬日』：標 D 的盤後＝D 開盤前那一晚。"""
    today = datetime.now(TPE).date()
    body = urllib.parse.urlencode({
        "down_type": "1", "commodity_id": "TX",
        "queryStartDate": (today - timedelta(days=10)).strftime("%Y/%m/%d"),
        "queryEndDate": (today + timedelta(days=4)).strftime("%Y/%m/%d"),
    }).encode()
    try:
        req = urllib.request.Request("https://www.taifex.com.tw/cht/3/futDataDown", data=body, headers=UA)
        with urllib.request.urlopen(req, timeout=40, context=_SSL) as resp:
            text = resp.read().decode("cp950", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        head = [h.strip() for h in rows[0]]
        ix = {k: head.index(k) for k in ("交易日期", "到期月份(週別)", "收盤價", "漲跌價", "交易時段")}
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"[taifex] 夜盤 CSV 抓不到：{exc}")
        return None

    recs = []
    for r in rows[1:]:
        if len(r) <= max(ix.values()):
            continue
        month = r[ix["到期月份(週別)"]].strip()
        if not (month.isdigit() and len(month) == 6):        # 排除價差、週契約
            continue
        recs.append({"date": r[ix["交易日期"]].strip(), "month": month, "session": r[ix["交易時段"]].strip(),
                     "last": _num(r[ix["收盤價"]]), "chg": _num(r[ix["漲跌價"]])})
    night = [x for x in recs if x["session"] == "盤後" and x["last"] and x["chg"] is not None]
    if not night:
        return None
    d = max(x["date"] for x in night)
    cand = sorted([x for x in night if x["date"] == d], key=lambda x: x["month"])
    n = cand[0]                                            # 近月
    ref = n["last"] - n["chg"]                             # 漲跌的參考價＝前一個日盤的結算價
    out = {"date": d.replace("/", ""), "day_date": "", "month": n["month"],
           "last": n["last"], "ref": ref, "pct": (n["last"] / ref - 1) * 100, "src": "csv"}
    if verbose:
        print(f"[taifex] 夜盤（CSV，歸屬日 {d}）{n['month']} {n['last']:,.0f}（參考 {ref:,.0f}）{out['pct']:+.2f}%")
    return out


def _night_foreign_one(day: date, commodity: str) -> dict | None:
    """期交所三大法人-區分各期貨契約（夜盤）查詢頁，回傳外資的多/空/淨交易口數；當天沒資料回 None"""
    body = urllib.parse.urlencode({"queryType": "1", "goDay": "", "doQuery": "1", "dateaddcnt": "",
                                   "queryDate": day.strftime("%Y/%m/%d"), "commodityId": commodity}).encode()
    req = urllib.request.Request("https://www.taifex.com.tw/cht/3/futContractsDateAh", data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=40, context=_SSL) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    if "<table" not in text.lower():
        return None
    try:
        tabs = pd.read_html(io.StringIO(text))
    except Exception:  # noqa: BLE001  沒有表格＝當天沒有夜盤資料
        return None
    for t in tabs:
        for _, row in t.iterrows():
            cells = [str(x).strip() for x in row.values]
            if "外資" in cells and any(("臺股期貨" in c) or ("小型臺指" in c) for c in cells):
                k = cells.index("外資")
                nums = [_num(c) for c in cells[k + 1:]]
                nums = [x for x in nums if x is not None]
                if len(nums) >= 6:     # 多方口數、多方金額、空方口數、空方金額、淨額口數、淨額金額
                    return {"long": int(nums[0]), "short": int(nums[2]), "net": int(nums[4])}
    return None


def fetch_night_foreign(verbose: bool) -> dict | None:
    """外資在最近一場夜盤的台指期交易口數（期交所約 06:00 公布；夜盤不公布未平倉）。
    淨口數＝大台＋小台÷4（折算大台）。"""
    today = datetime.now(TPE).date()
    for k in range(4, -4, -1):                     # 週五夜盤歸屬到下週一，所以從未來幾天往回找
        d = today + timedelta(days=k)
        if d.weekday() >= 5:
            continue
        try:
            tx = _night_foreign_one(d, "TXF")
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"[taifex] 夜盤外資 {d} 失敗：{exc}")
            tx = None
        if tx:
            try:
                mtx = _night_foreign_one(d, "MXF")
            except Exception:  # noqa: BLE001
                mtx = None
            equiv = tx["net"] + (mtx["net"] / 4 if mtx else 0)
            out = {"date": d.strftime("%Y-%m-%d"), "tx_net": tx["net"], "tx_long": tx["long"], "tx_short": tx["short"],
                   "mtx_net": mtx["net"] if mtx else None, "net_equiv": int(round(equiv))}
            if verbose:
                print(f"[taifex] 夜盤外資（歸屬日 {out['date']}）大台淨 {tx['net']:+,} 口"
                      + (f"、小台淨 {mtx['net']:+,} 口" if mtx else "") + f"，折算大台 {out['net_equiv']:+,} 口")
            return out
        time.sleep(0.6)
    if verbose:
        print("[taifex] 夜盤外資口數抓不到")
    return None


def _oi_from_csv(verbose: bool, end_candidates: list[date]) -> pd.DataFrame | None:
    """期交所「三大法人-區分各期貨契約」CSV 下載，可一次抓一段日期，算得出每日增減。
    結束日期不能晚於期交所最新資料日，否則回錯誤頁；所以從台股最近交易日開始往前試。"""
    text = None
    try:
        for end in end_candidates[:4]:
            body = urllib.parse.urlencode({
                "queryStartDate": (end - timedelta(days=40)).strftime("%Y/%m/%d"),
                "queryEndDate": end.strftime("%Y/%m/%d"), "commodityId": "TXF",
            }).encode()
            req = urllib.request.Request("https://www.taifex.com.tw/cht/3/futContractsDateDown",
                                         data=body, headers=UA)
            with urllib.request.urlopen(req, timeout=40, context=_SSL) as resp:
                t = resp.read().decode("cp950", errors="replace")
            if t.lstrip("\ufeff").startswith("日期,"):
                text = t
                break
            time.sleep(1)
        if text is None:
            raise RuntimeError("期交所回傳的不是 CSV（日期可能還沒有資料）")
        df = pd.read_csv(io.StringIO(text))
        df = df[df["身份別"].astype(str).str.startswith("外資")]
        out = pd.DataFrame({
            "date": pd.to_datetime(df["日期"]),
            "net": df["多空未平倉口數淨額"].astype(int),
            "long": df["多方未平倉口數"].astype(int),
            "short": df["空方未平倉口數"].astype(int),
        })
        return out if len(out) else None
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"[taifex] 未平倉 CSV 失敗，改用 OpenAPI：{exc}")
        return None


def _oi_from_openapi() -> pd.DataFrame:
    raw = _get("https://openapi.taifex.com.tw/v1/"
               "MarketDataOfMajorInstitutionalTradersDetailsOfFuturesContractsBytheDate", timeout=40)
    if raw[:2] == b"\x1f\x8b":
        import gzip
        raw = gzip.decompress(raw)
    rows = json.loads(raw.decode("utf-8-sig"))
    rec = next(r for r in rows if str(r.get("ContractCode", "")).strip() == "臺股期貨"
               and str(r.get("Item", "")).startswith("外資"))
    return pd.DataFrame([{
        "date": pd.Timestamp(str(rec["Date"])),
        "net": int(_num(rec["OpenInterest(Net)"])),
        "long": int(_num(rec["OpenInterest(Long)"])),
        "short": int(_num(rec["OpenInterest(Short)"])),
    }])


def fetch_foreign_futures_oi(cache: Path, verbose: bool,
                             tw_dates: list[pd.Timestamp] | None = None) -> dict | None:
    """外資及陸資在臺股期貨的未平倉口數（淨額＝多單−空單）。CSV 為主、OpenAPI 備援，結果存快取。"""
    today = datetime.now(TPE).date()
    ends = [d.date() for d in reversed(tw_dates or [])][:4] or [today - timedelta(days=i) for i in range(4)]
    new = _oi_from_csv(verbose, ends)
    if new is None:
        try:
            new = _oi_from_openapi()
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"[taifex] 外資期貨未平倉抓不到：{exc}")
            new = None

    hist = pd.DataFrame(columns=["date", "net", "long", "short"])
    if cache.exists():
        hist = pd.read_csv(cache, parse_dates=["date"])
    if new is not None:
        hist = hist[~hist["date"].isin(new["date"])]
        hist = pd.concat([x for x in (hist, new) if len(x)])
    if not len(hist):
        return None
    hist = hist.sort_values("date").drop_duplicates("date", keep="last").tail(250)
    hist.assign(date=hist["date"].dt.strftime("%Y-%m-%d")).to_csv(cache, index=False)

    last = hist.iloc[-1]
    prev = hist.iloc[-2] if len(hist) > 1 else None
    chg = int(last["net"] - prev["net"]) if prev is not None else None
    net = int(last["net"])
    if verbose:
        print(f"[taifex] 外資台指期淨未平倉 {net:+,} 口"
              + (f"（較前日 {chg:+,}）" if chg is not None else "（首日，明天起有增減）"))
    return {"date": last["date"].strftime("%Y-%m-%d"), "net": net,
            "long": int(last["long"]), "short": int(last["short"]), "chg": chg,
            "prev_date": prev["date"].strftime("%Y-%m-%d") if prev is not None else None}


# --------------------------------------------------------------------------
# 抓資料：yfinance / Stooq
# --------------------------------------------------------------------------

def _fetch_stooq(symbol: str) -> pd.Series:
    url = "https://stooq.com/q/d/l/?s=" + urllib.parse.quote(symbol, safe="") + "&i=d"
    body = _get(url).decode("utf-8", errors="replace")
    if "Close" not in body:
        raise RuntimeError(f"{symbol} → {' '.join(body.split())[:90] or '(空白回應)'}")
    df = pd.read_csv(io.StringIO(body))
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date")["Close"].sort_index()


def load_market(verbose: bool) -> pd.DataFrame:
    frames: dict[str, pd.Series] = {}
    try:
        import yfinance as yf
        tickers = [m[2] for m in MARKET]
        raw = yf.download(tickers, period=LOOKBACK_PERIOD, interval="1d",
                          auto_adjust=False, progress=False, group_by="column", threads=True)
        close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
        for key, _n, ysym, _s, _k in MARKET:
            if ysym in close.columns:
                s = pd.to_numeric(close[ysym], errors="coerce").dropna()
                if len(s) > 100:
                    frames[key] = s
        if verbose:
            print(f"[yfinance] 取得 {len(frames)}/{len(MARKET)} 檔")
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"[yfinance] 失敗，改用 Stooq：{exc}")
    for key, name, _y, ssym, _k in MARKET:
        if key in frames:
            continue
        try:
            frames[key] = _fetch_stooq(ssym)
            if verbose:
                print(f"[stooq] 補上 {key}（{name}）")
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"[warn] {key}（{name}）兩個來源都抓不到：{exc}")
    if "SOX" not in frames:
        raise RuntimeError("費半抓不到，核心訊號無法計算")
    df = pd.DataFrame(frames).sort_index()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


# --------------------------------------------------------------------------
# 計算
# --------------------------------------------------------------------------

def next_tw_session(us_date: pd.Timestamp, tw_index: pd.DatetimeIndex) -> pd.Timestamp | None:
    later = tw_index[tw_index > us_date]
    return later[0] if len(later) else None


def classify(z: float | None) -> str:
    if z is None:
        return "unknown"
    if z <= CORE_Z["alert"]:
        return "alert"
    if z <= CORE_Z["watch"]:
        return "watch"
    if z >= CORE_Z["up"]:
        return "up"
    return "calm"


def bg_state(z: float | None) -> str:
    if z is None:
        return "unknown"
    a = abs(z)
    return "alert" if a >= BG_Z["alert"] else "watch" if a >= BG_Z["watch"] else "calm"


def backtest(sox_ret: pd.Series, taiex: pd.Series) -> dict:
    """同一套費半 z 規則套在過去每一天，看下一個台股交易日的報酬。"""
    z = rolling_z(sox_ret)
    tw_ret = taiex.pct_change() * 100
    rows = []
    for d, zv in z.dropna().items():
        t = next_tw_session(d, tw_ret.index)
        if t is None or pd.isna(tw_ret.get(t)):
            continue
        rows.append((classify(float(zv)), float(tw_ret[t])))
    if not rows:
        return {"n": 0, "buckets": []}
    df = pd.DataFrame(rows, columns=["state", "ret"])
    out = []
    for st in ["alert", "watch", "calm", "up"]:
        g = df[df.state == st]["ret"]
        out.append({
            "state": st, "label": STATE_TEXT[st], "n": int(len(g)),
            "avg": _r(g.mean(), 2) if len(g) else None,
            "median": _r(g.median(), 2) if len(g) else None,
            "down_pct": _r((g < 0).mean() * 100, 0) if len(g) else None,
            "worse_than_1pct": _r((g <= -1).mean() * 100, 0) if len(g) else None,
        })
    allr = df["ret"]
    return {
        "n": int(len(df)),
        "start": z.dropna().index[0].strftime("%Y-%m-%d"),
        "base": {"avg": _r(allr.mean(), 2), "down_pct": _r((allr < 0).mean() * 100, 0)},
        "buckets": out,
    }


LOG_FIELDS = ["us_date", "made_at", "state", "sox_pct", "sox_z", "night_pct", "night_z",
              "target_tw_date", "taiex_ret"]


def update_log(path: Path, entry: dict, taiex: pd.Series) -> list[dict]:
    rows: list[dict] = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
    if entry:
        new = {k: entry.get(k, "") for k in LOG_FIELDS}
        hit = [i for i, r in enumerate(rows) if r["us_date"] == entry["us_date"]]
        if not hit:
            rows.append(new)
        elif not rows[hit[0]].get("taiex_ret"):
            rows[hit[0]] = new   # 結果還沒出來前，以最新一次判讀為準
    tw_ret = taiex.pct_change() * 100
    for r in rows:
        if r.get("taiex_ret"):
            continue
        t = next_tw_session(pd.Timestamp(r["us_date"]), tw_ret.index)
        if t is not None and not pd.isna(tw_ret.get(t)):
            r["target_tw_date"] = t.strftime("%Y-%m-%d")
            r["taiex_ret"] = f"{tw_ret[t]:.3f}"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows


def live_score(rows: list[dict]) -> dict:
    done = [r for r in rows if r.get("taiex_ret")]
    out = []
    for st in ["alert", "watch", "calm", "up"]:
        g = [float(r["taiex_ret"]) for r in done if r["state"] == st]
        out.append({"state": st, "label": STATE_TEXT[st], "n": len(g),
                    "avg": _r(np.mean(g), 2) if g else None,
                    "down_pct": _r(np.mean([x < 0 for x in g]) * 100, 0) if g else None})
    recent = [{"us_date": r["us_date"], "state": r["state"],
               "target": r.get("target_tw_date") or None,
               "ret": _r(_num(r.get("taiex_ret")), 2)} for r in rows[-10:]]
    return {"n": len(done), "pending": len(rows) - len(done), "buckets": out, "recent": recent[::-1]}


def data_signature(mk, taiex, flow, night, taiex_src) -> str:
    return "|".join([
        mk["SOX"].dropna().index[-1].strftime("%Y-%m-%d"),
        taiex.index[-1].strftime("%Y-%m-%d"), taiex_src,
        flow.index[-1].strftime("%Y-%m-%d") if len(flow) else "-",
        str((night or {}).get("date", "-")),
    ])


def build_payload(mk: pd.DataFrame, taiex: pd.Series, flow: pd.Series,
                  night: dict | None, log_path: Path, sig: str = "",
                  fut_oi: dict | None = None, night_fx: dict | None = None) -> dict:
    sox = mk["SOX"].dropna()
    sox_ret = sox.pct_change() * 100
    us_date = sox.index[-1]
    sox_pct = float(sox_ret.iloc[-1])
    sox_z = zscore_last(sox_ret)

    tw_ret = taiex.pct_change() * 100
    tw_last = taiex.index[-1]
    tw_sd = float(tw_ret.tail(Z_WINDOW).std())

    # 期交所把「前一晚」的夜盤算在隔天的交易日。報表日期若不晚於台股最新收盤日，
    # 代表這場夜盤在台股收盤前就結束了，已經反映過，不能拿來判斷下一個交易日。
    night_z = None
    night_stale = False
    if night:
        try:
            night_stale = pd.Timestamp(str(night["date"])) <= tw_last
        except Exception:  # noqa: BLE001
            night_stale = True
        if not night_stale and tw_sd:
            night_z = night["pct"] / tw_sd

    # 決策：夜盤優先（它已含美股收盤後的資訊），抓不到才用費半
    driver = "night" if night_z is not None else "sox"
    z_used = night_z if driver == "night" else sox_z
    state = classify(z_used)

    # 外資確認
    flow_info = None
    notes = []
    if len(flow):
        f1 = float(flow.iloc[-1])
        f5 = float(flow.tail(5).sum())
        fz = zscore_last(flow) if len(flow) >= 20 else None
        flow_info = {"date": flow.index[-1].strftime("%Y-%m-%d"), "net": _r(f1, 1),
                     "sum5": _r(f5, 1), "z": _r(fz, 2)}
        last_tw = float(tw_ret.iloc[-1])
        if last_tw > 0.3 and f1 < -100:
            notes.append(f"前一日台股上漲但外資賣超 {abs(f1):.0f} 億，漲勢不是外資推的。")
        if f5 < -500:
            notes.append(f"外資近 5 日累計賣超 {abs(f5):.0f} 億，籌碼面偏弱。")
        if state in ("calm", "up") and f5 < -500:
            notes.append("美股訊號雖不差，但外資持續撤出，不宜追高。")
    if night_fx:
        flow_info = flow_info or {}
        nf = dict(night_fx)
        nf["fresh"] = pd.Timestamp(nf["date"]) > tw_last      # 歸屬日晚於台股最新收盤日＝收盤後那一晚
        flow_info["night"] = nf
        if nf["fresh"] and nf["net_equiv"] <= -3000:
            notes.append(f"外資昨晚夜盤台指期淨賣 {abs(nf['net_equiv']):,} 口（折算大台），開盤偏空。")
        elif nf["fresh"] and nf["net_equiv"] >= 3000:
            notes.append(f"外資昨晚夜盤台指期淨買 {nf['net_equiv']:,} 口（折算大台），開盤偏多。")
    if fut_oi:
        flow_info = flow_info or {}
        flow_info["fut"] = fut_oi
        if fut_oi.get("chg") is not None and fut_oi["chg"] <= -3000:
            notes.append(f"外資台指期淨空單增加 {abs(fut_oi['chg']):,} 口，期貨端也在加碼避險。")
        elif fut_oi.get("chg") is not None and fut_oi["chg"] >= 3000:
            notes.append(f"外資台指期淨空單減少 {fut_oi['chg']:,} 口，空方在回補。")

    core = [{
        "key": "SOX", "label": "費城半導體", "sub": f"美股 {us_date:%m/%d} 收盤",
        "value": _r(sox.iloc[-1], 2), "pct": _r(sox_pct, 2), "z": _r(sox_z, 2),
        "state": classify(sox_z), "used": driver == "sox",
        "rule": "台股隔日報酬與費半隔夜漲跌的相關最高。z 分數＝今天的漲跌是近 60 日波動的幾倍。",
    }, {
        "key": "NIGHT", "label": "台指期夜盤",
        "sub": f"近月 {night['month']}" if night else "期交所",
        "value": _r(night["last"], 0) if night else None,
        "pct": _r(night["pct"], 2) if night and not night_stale else None, "z": _r(night_z, 2),
        "chg": _r(night["last"] - night["ref"], 0) if night and not night_stale and night.get("ref") else None,
        "ref": _r(night["ref"], 0) if night and night.get("ref") else None,
        "state": classify(night_z) if night_z is not None else "unknown", "used": driver == "night",
        "date": night["date"] if night else None, "stale": night_stale,
        "rule": ("期交所開盤前公布的是更早一場夜盤（台股收盤前已結束），不能用來判斷今天，燈號改看費半。"
                 if night_stale else
                 "夜盤收在美股收盤之後，已經消化美股的影響，是開盤前最直接的指標。以台股近 60 日波動換算 z 分數。"),
    }]

    # 背景
    bg = []

    def add_bg(key, label, series, n, unit, as_bp=False, level=False):
        s = series.dropna()
        if len(s) <= n + Z_WINDOW // 2:
            return
        chg = s.diff(n) * 100 if as_bp else (s if level else s.pct_change(n) * 100)
        z = zscore_last(chg.iloc[::1])
        bg.append({"key": key, "label": label, "value": _r(s.iloc[-1], 2),
                   "metric": _r(chg.iloc[-1], 2 if not as_bp else 1), "unit": unit,
                   "z": _r(z, 2), "state": bg_state(z)})

    if "BRENT" in mk:
        add_bg("BRENT", "布蘭特 5 日", mk["BRENT"], 5, "%")
    if "US10Y" in mk:
        add_bg("US10Y", "美10Y 單日", mk["US10Y"], 1, "bp", as_bp=True)
    if "DXY" in mk:
        add_bg("DXY", "美元指數 5 日", mk["DXY"], 5, "%")
    if "USDJPY" in mk:
        add_bg("USDJPY", "美元/日圓 5 日", mk["USDJPY"], 5, "%")
    if "BRENT" in mk and "WTI" in mk:
        pair = mk[["BRENT", "WTI"]].dropna()
        add_bg("SPREAD", "布蘭特−WTI", pair["BRENT"] - pair["WTI"], 0, "美元", level=True)
    for b in bg:
        if b["state"] == "alert":
            notes.append(f"{b['label']} 波動異常（z={b['z']:+.1f}），留意是否擴散到股市。")

    # 規則是否還成立：費半→台股（隔夜）60 日滾動相關
    pair = []
    for d, r in sox_ret.dropna().items():
        t = next_tw_session(d, tw_ret.index)
        if t is not None and not pd.isna(tw_ret.get(t)):
            pair.append((t, r, tw_ret[t]))
    roll = []
    if pair:
        p = pd.DataFrame(pair, columns=["t", "sox", "tw"]).drop_duplicates("t").set_index("t")
        c = p["sox"].rolling(60, min_periods=30).corr(p["tw"]).dropna().tail(250)
        roll = [{"d": d.strftime("%Y-%m-%d"), "v": _r(v)} for d, v in c.items()]

    target = next_tw_session(us_date, tw_ret.index)
    log_rows = update_log(log_path, {
        "us_date": us_date.strftime("%Y-%m-%d"),
        "made_at": datetime.now(TPE).isoformat(timespec="minutes"),
        "state": state,
        "sox_pct": f"{sox_pct:.3f}", "sox_z": "" if sox_z is None else f"{sox_z:.3f}",
        "night_pct": "" if not night else f"{night['pct']:.3f}",
        "night_z": "" if night_z is None else f"{night_z:.3f}",
    } if target is None else None, taiex)
    # target 不是 None 代表下一個台股交易日已經開過了（資料是舊的），不寫新紀錄

    return {
        "schema": 2,
        "sig": sig,
        "generated_at": datetime.now(TPE).isoformat(timespec="seconds"),
        "us_date": us_date.strftime("%Y-%m-%d"),
        "tw_last_date": tw_last.strftime("%Y-%m-%d"),
        "tw_last_close": _r(taiex.iloc[-1], 2),
        "tw_last_pct": _r(tw_ret.iloc[-1], 2),
        "fresh": target is None,
        "overall": state, "overall_text": STATE_TEXT[state], "action": ACTION_TEXT[state],
        "driver": driver, "z_used": _r(z_used, 2),
        "thresholds": CORE_Z,
        "core": core, "flow": flow_info, "background": bg, "notes": notes,
        "backtest": backtest(sox_ret, taiex),
        "live": live_score(log_rows),
        "rolling_corr": roll,
    }


# --------------------------------------------------------------------------
# 離線測試
# --------------------------------------------------------------------------

def synthetic(n: int = 480):
    rng = np.random.default_rng(20261017)
    us = pd.bdate_range(end=pd.Timestamp("2026-10-16"), periods=n)
    sox_r = rng.normal(0, 1.8, n)
    oil = rng.normal(0, 1.6, n)
    y10 = 0.3 * oil + rng.normal(0, 4, n)
    walk = lambda r, s: s * np.exp(np.cumsum(r / 100))  # noqa: E731
    mk = pd.DataFrame({
        "SOX": walk(sox_r, 9000), "BRENT": walk(oil, 80), "WTI": walk(oil * .95 + rng.normal(0, .4, n), 75),
        "US10Y": 4.2 + np.cumsum(y10) / 1000, "DXY": walk(rng.normal(0, .35, n), 99),
        "USDJPY": walk(rng.normal(0, .5, n), 150), "USDTWD": walk(rng.normal(0, .25, n), 31),
    }, index=us)
    # 台股：下一個交易日跟著費半走（0.45 倍）
    tw_idx = us[1:]
    tw_r = 0.45 * sox_r[:-1] + rng.normal(0, 0.8, n - 1)
    taiex = pd.Series(walk(tw_r, 30000), index=tw_idx)
    flow = pd.Series(rng.normal(0, 150, 30), index=tw_idx[-30:])
    night = {"date": "20261017", "day_date": "20261016", "month": "202611",
             "last": float(taiex.iloc[-1]) * 0.99, "ref": float(taiex.iloc[-1]), "pct": -1.0}
    # 模擬「美股新收盤、台股尚未開」：台股少最後一天
    return mk, taiex.iloc[:-1], flow.iloc[:-1], night


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/correlations.json")
    ap.add_argument("--offline-test", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    verbose = not args.quiet
    out = Path(args.out)
    data_dir = out.parent

    try:
        if args.offline_test:
            mk, taiex, flow, night = synthetic()
            night_fx = {"date": "2026-10-19", "tx_net": 2143, "tx_long": 23937, "tx_short": 21794,
                        "mtx_net": -1200, "net_equiv": 1843}
            fut_oi = {"date": "2026-10-16", "net": -93675, "long": 9812, "short": 103487,
                      "chg": -4210, "prev_date": "2026-10-15"}
            taiex_src = "test"
            log_path = data_dir / "signal_log.test.csv"
            if log_path.exists():
                log_path.unlink()
        else:
            taiex = fetch_taiex_twse(data_dir / "taiex_twse.csv", verbose)
            taiex_src = "twse"
            mk = load_market(verbose)
            if len(taiex) < 200:
                if "TWII" not in mk:
                    raise RuntimeError("台股資料兩個來源都抓不到")
                print("[warn] 證交所資料不足，台股改用 Yahoo（可能晚一天）")
                taiex = mk["TWII"].dropna()
                taiex_src = "yahoo"
            flow = fetch_foreign_flow(data_dir / "foreign_flow.csv", list(taiex.index), verbose)
            night = fetch_tx_night_csv(verbose) or fetch_tx_night(verbose)   # CSV 早上就有；抓不到才用 OpenAPI
            night_fx = fetch_night_foreign(verbose)
            fut_oi = fetch_foreign_futures_oi(data_dir / "foreign_fut_oi.csv", verbose, list(taiex.index))
            log_path = data_dir / "signal_log.csv"

        # 沒有新資料就不重寫（週末／假日）
        if out.exists() and not args.force and not args.offline_test:
            try:
                prev = json.loads(out.read_text(encoding="utf-8"))
                same = (prev.get("sig") == data_signature(mk, taiex, flow, night, taiex_src)
                        and ((prev.get("flow") or {}).get("fut") or None) == (fut_oi or None)
                        and ((prev.get("flow") or {}).get("night") or {}).get("date") == (night_fx or {}).get("date"))
                if same:
                    if verbose:
                        print("[info] 沒有新資料（週末或假日），不更新")
                    return 0
            except Exception:  # noqa: BLE001
                pass

        payload = build_payload(mk, taiex, flow, night, log_path,
                                data_signature(mk, taiex, flow, night, taiex_src), fut_oi, night_fx)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 產生失敗：{exc}", file=sys.stderr)
        return 1

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)

    if verbose:
        print(f"[ok] {out}")
        print(f"     美股 {payload['us_date']} → 台股 {payload['tw_last_date']} 之後的下一個交易日"
              + ("" if payload["fresh"] else "（注意：台股已開過，資料不是最新）"))
        print(f"     總燈號：{payload['overall_text']}（依據 {payload['driver']}，z={payload['z_used']}）"
              f" → {payload['action']}")
        for c in payload["core"]:
            print(f"     {c['label']:<6} {c['pct']}%  z={c['z']}")
        bt = payload["backtest"]
        print(f"     回測 {bt.get('n')} 天（基準：隔日平均 {bt.get('base', {}).get('avg')}%）")
        for b in bt.get("buckets", []):
            print(f"       {b['label']:<4} n={b['n']:<4} 隔日平均 {b['avg']}%  下跌機率 {b['down_pct']}%")
        for n in payload["notes"]:
            print(f"     ※ {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
