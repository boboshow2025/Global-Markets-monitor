# -*- coding: utf-8 -*-
"""
panic_gauge.py  --  恐慌儀表（Global-Markets-monitor）

回答一個問題：這波跌勢是不是接近尾聲、可以開始布局了？

三個指標，各自和「自己過去一段時間」比較（百分位），不用固定數字：
  1. 波動度      台指VIX（期交所，累積滿 120 天後啟用）；之前用加權指數 10 日實現波動率代替
  2. 外資期貨空單 外資台指期淨空單口數（期交所三大法人 CSV）
  3. 融資餘額    上市融資金額 10 日變化（證交所信用交易統計）
  4. 融資維持率  Σ(個股融資餘額張 × 1000 × 收盤價) ÷ 融資金額（上市＋上櫃）
                 個股融資張數與收盤價讀 stock-screener 的 prices.db（chip_screener.py 每晚 21:45 更新），
                 融資金額用第 3 項的快取；< 160% 算恐慌，< 140% 進入斷頭區（個股 130% 追繳）

狀態（四項中幾項進入恐慌區）：
  平靜      四項都正常
  警戒      一項進入恐慌區
  恐慌升溫  兩項以上進入恐慌區
  融資斷頭  上市融資 10 日減少 15% 以上（斷頭潮），觸發後 5 個交易日內顯示
            → 回測兩次都出現在最低點前 1 天：恐慌可能見底，可小量試單（約 1/5）
  恐慌退燒  近 15 日曾恐慌升溫，且「波動回落」或「空單回補」出現，融資也已大減過
            → 可能接近底部，可先布局約 1/3（第四階段「底部確認」看資金輪動儀表轉攻）

輸出 data/panic.json（網頁讀這個），快取在 data/panic/。
沒有新資料（週末、假日）就不重寫，排程也就不會產生空 commit。

用法：
    python panic_gauge.py                 # 第一次會補約兩年歷史（融資要逐日抓，約 10 分鐘）
    python panic_gauge.py --offline-test  # 合成資料，驗證流程
    python panic_gauge.py --probe         # 只測試三個資料來源抓不抓得到，印出原始格式
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# 共用 market_corr.py 的連線設定與加權指數快取（同一個資料夾）
from market_corr import TPE, UA, _SSL, _get, _num, _r, fetch_taiex_twse

# --------------------------------------------------------------------------
# 參數
# --------------------------------------------------------------------------
PCT_WINDOW = 120            # 百分位：和自己過去 120 個交易日比
VOL_PANIC_PCT = 0.90        # 波動度 ≥ 第 90 百分位 → 恐慌
SHORT_PANIC_PCT = 0.80      # 外資淨空單 ≥ 第 80 百分位 → 恐慌
MARGIN_PANIC_CHG = -0.04    # 融資 10 日減少 4% 以上 → 恐慌（去槓桿）
VOL_EASE_DROP = 0.20        # 波動度從近 10 日高點回落 20% 以上
SHORT_EASE_DROP = 0.25      # 淨空單從近 10 日高點減少 25% 以上
HEAT_LOOKBACK = 15          # 「近期曾恐慌」的回看天數
MARGIN_WASH_LOOKBACK = 15   # 「融資已大減過」的回看天數
MAINT_WASH_DROP = 0.0       # 「已洗」另需維持率同步下滑：去槓桿當天的維持率 10 日變化 < 此值（百分點）；缺維持率資料時只看融資
MARGIN_CAPIT_CHG = -0.15    # 融資 10 日減少 15% 以上 → 斷頭潮（可能的恐慌底）
CAPIT_SHOW_DAYS = 5         # 斷頭警示觸發後維持顯示幾個交易日
MAINT_PANIC = 160.0        # 融資維持率 < 160% → 恐慌（2024/10 以來 5 段，都在波段低點附近）
MAINT_CAPIT = 140.0        # 融資維持率 < 140% → 斷頭區（2025/4/7–4/9，最低 130%）
PRICES_DB = Path(__file__).resolve().parent.parent / "stock-screener" / "prices.db"   # 個股融資張數＋收盤價
VIX_MIN_DAYS = 120          # 台指VIX 累積滿這麼多天才取代實現波動率
BACKFILL_DAYS = 500         # 第一次補歷史的交易日數（約兩年）
HORIZONS = [5, 10, 20]

# 「今天市場」情緒分數（0~100，越高越恐慌）：五項各換算成 0~100 後平均，只是把現有指標濃縮成一個數字，不是回測門檻
BREADTH_CSV = PRICES_DB.parent / "market_breadth.csv"   # stock-screener 的市場寬度（每晚 18:30 更新）
FLOW_CSV = Path("data") / "foreign_flow.csv"             # market_corr.py 寫的外資現貨買賣超（億元）
MOOD_BANDS = [(70, "恐慌", "red"), (50, "警戒", "orange"), (30, "中性", "calm"), (0, "平靜", "gold")]

DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "panic"

STATE_TEXT = {"calm": "平靜", "watch": "警戒", "heat": "恐慌升溫", "capit": "融資斷頭", "ease": "恐慌退燒", "unknown": "無資料"}
ACTION_TEXT = {
    "calm": "沒有恐慌跡象，依資金輪動儀表操作",
    "watch": "單一指標轉差，留意是否擴散",
    "heat": "恐慌擴散中，不急著接刀",
    "capit": "融資斷頭潮，恐慌可能見底：可小量試單約 1/5，停損設在觸發日低點下方（觸發後仍可能再跌約 5%）",
    "ease": "恐慌開始退燒，可能接近底部：可先布局約 1/3，等資金輪動轉攻再加碼",
    "unknown": "資料不足，不做判讀",
}


# --------------------------------------------------------------------------
# 抓資料
# --------------------------------------------------------------------------

def _month_list(n: int) -> list[date]:
    today = datetime.now(TPE).date()
    y, m, out = today.year, today.month, []
    for _ in range(n):
        out.append(date(y, m, 1))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def _load_cache(path: Path, col: str) -> pd.Series:
    if not path.exists():
        return pd.Series(dtype=float)
    df = pd.read_csv(path, parse_dates=["date"])
    return df.set_index("date")[col].astype(float).sort_index()


def _save_cache(path: Path, s: pd.Series, col: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    s = s[~s.index.duplicated(keep="last")].sort_index()
    pd.DataFrame({"date": s.index.strftime("%Y-%m-%d"), col: s.values}).to_csv(path, index=False)
    return s


def fetch_vix(verbose: bool) -> pd.Series:
    """期交所台指VIX每日收盤檔（只保留當月與前 3 個月），抓到就併進快取，逐日累積。"""
    cache = CACHE_DIR / "vix_tw.csv"
    old = _load_cache(cache, "vix")
    got = {}
    for ms in _month_list(4):
        url = f"https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{ms:%Y%m}new.txt"
        try:
            text = _get(url, timeout=30).decode("cp950", errors="replace")
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"[vix] {ms:%Y-%m} 抓不到：{exc}")
            continue
        for line in text.splitlines():
            tok = line.split()
            if len(tok) >= 3 and tok[0].isdigit() and len(tok[0]) == 8:
                v = _num(tok[2])
                if v:
                    got[pd.Timestamp(datetime.strptime(tok[0], "%Y%m%d"))] = v
        time.sleep(0.5)
    s = pd.concat([x for x in (old, pd.Series(got, dtype=float)) if len(x)]) if (len(old) or got) else old
    if len(s):
        s = _save_cache(cache, s, "vix")
    if verbose:
        print(f"[vix] 台指VIX 累積 {len(s)} 天" + (f"，最新 {s.index[-1]:%Y-%m-%d} = {s.iloc[-1]:.2f}" if len(s) else ""))
    return s


def _oi_window(start: date, end: date) -> pd.DataFrame | None:
    body = urllib.parse.urlencode({
        "queryStartDate": start.strftime("%Y/%m/%d"), "queryEndDate": end.strftime("%Y/%m/%d"),
        "commodityId": "TXF",
    }).encode()
    req = urllib.request.Request("https://www.taifex.com.tw/cht/3/futContractsDateDown", data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=40, context=_SSL) as resp:
        t = resp.read().decode("cp950", errors="replace")
    if not t.lstrip("﻿").startswith("日期,"):
        return None
    df = pd.read_csv(io.StringIO(t))
    df = df[df["身份別"].astype(str).str.startswith("外資")]
    if not len(df):
        return None
    return pd.DataFrame({"date": pd.to_datetime(df["日期"]),
                         "net": df["多空未平倉口數淨額"].astype(int)})


def fetch_foreign_short(tw_dates: list[pd.Timestamp], verbose: bool) -> pd.Series:
    """外資台指期淨未平倉（口）。第一次往回補約兩年，之後每次只補最近 40 天。"""
    cache = CACHE_DIR / "fut_oi_long.csv"
    old = _load_cache(cache, "net")
    today = datetime.now(TPE).date()
    last_tw = tw_dates[-1].date() if tw_dates else today
    if len(old) >= 300:
        windows = [(last_tw - timedelta(days=40), last_tw)]
    else:
        windows, end = [], last_tw
        while end > today - timedelta(days=int(BACKFILL_DAYS * 1.45)):
            windows.append((end - timedelta(days=29), end))
            end = end - timedelta(days=30)
    parts, fails = [], 0
    for i, (a, b) in enumerate(windows):
        ok = None
        # 結束日不能晚於期交所最新資料日：最近一段從台股最近交易日往前試
        ends = [b] if i > 0 else [d.date() for d in reversed(tw_dates[-4:])] or [b]
        for e in ends:
            try:
                ok = _oi_window(a, e)
            except Exception as exc:  # noqa: BLE001
                if verbose:
                    print(f"[oi] {a}~{e} 失敗：{exc}")
            if ok is not None:
                break
            time.sleep(0.8)
        if ok is None:
            fails += 1
            if fails >= 3:
                if verbose:
                    print("[oi] 連續失敗 3 次，先用已有資料")
                break
        else:
            fails = 0
            parts.append(ok)
        if verbose and len(windows) > 3 and i % 6 == 0:
            print(f"[oi] 補歷史中 {i + 1}/{len(windows)} ...")
        time.sleep(1.0)
    new = pd.concat(parts).set_index("date")["net"].astype(float) if parts else pd.Series(dtype=float)
    s = pd.concat([x for x in (old, new) if len(x)]) if (len(old) or len(new)) else old
    if len(s):
        s = _save_cache(cache, s, "net")
    if verbose:
        print(f"[oi] 外資台指期淨未平倉 {len(s)} 天" + (f"，最新 {s.index[-1]:%Y-%m-%d} {s.iloc[-1]:+,.0f} 口" if len(s) else ""))
    return s


def _margin_one_day(d: pd.Timestamp) -> float | None:
    """證交所 MI_MARGN 信用交易統計：融資金額(仟元) 今日餘額 → 億元"""
    url = ("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=MS&date="
           + d.strftime("%Y%m%d"))
    j = json.loads(_get(url, timeout=30).decode("utf-8"))
    if j.get("stat") not in (None, "OK"):
        return None
    rows = []
    for t in j.get("tables", []) or []:
        if "信用交易統計" in str(t.get("title", "")) or any("融資金額" in str(r[0]) for r in t.get("data", []) if r):
            rows = t.get("data", [])
            break
    if not rows and "creditList" in j:            # 舊版格式
        rows = j["creditList"]
    for r in rows:
        if r and "融資金額" in str(r[0]):
            v = _num(r[-1])
            return v / 1e5 if v else None
    return None


def _fetch_daily(cache_name: str, label: str, one_day, tw_dates: list[pd.Timestamp], verbose: bool) -> pd.Series:
    """逐日抓、存快取：第一次補 BACKFILL_DAYS 天，之後只補缺的日子；中斷也不會白抓"""
    cache = CACHE_DIR / cache_name
    old = _load_cache(cache, "margin_100m")
    want = [d for d in tw_dates[-BACKFILL_DAYS:] if d not in old.index]
    if verbose and len(want) > 20:
        print(f"[{label}] 需要補 {len(want)} 天融資資料（限制查詢頻率，約 {len(want) * 1.3 / 60:.0f} 分鐘）...")
    got, fails = {}, 0
    for i, d in enumerate(want):
        try:
            v = one_day(d)
            fails = 0
            if v:
                got[d] = v
        except Exception as exc:  # noqa: BLE001
            fails += 1
            if verbose:
                print(f"[{label}] {d:%Y-%m-%d} 失敗：{exc}")
            if fails >= 5:
                print(f"[{label}] 連續失敗 5 次，先用已有資料（下次執行會接著補）")
                break
        if verbose and len(want) > 20 and i % 50 == 0:
            print(f"[{label}] {i}/{len(want)} ...")
        if len(got) and len(got) % 40 == 0:
            _save_cache(cache, pd.concat([x for x in (old, pd.Series(got, dtype=float)) if len(x)]), "margin_100m")
        time.sleep(1.2)
    s = pd.concat([x for x in (old, pd.Series(got, dtype=float)) if len(x)]) if (len(old) or got) else old
    if len(s):
        s = _save_cache(cache, s, "margin_100m")
    if verbose:
        print(f"[{label}] 融資餘額 {len(s)} 天" + (f"，最新 {s.index[-1]:%Y-%m-%d} {s.iloc[-1]:,.0f} 億" if len(s) else ""))
    return s


def fetch_margin(tw_dates: list[pd.Timestamp], verbose: bool) -> pd.Series:
    """上市融資餘額（億元）"""
    return _fetch_daily("margin.csv", "margin上市", _margin_one_day, tw_dates, verbose)


# ---- 上櫃（櫃買中心）----
TPEX_URLS = [
    # 新版網站
    "https://www.tpex.org.tw/www/zh-tw/margin/balance?date={ad}&response=json",
    # 舊版
    "https://www.tpex.org.tw/web/stock/margin_trading/margin_balance/margin_bal_result.php?l=zh-tw&o=json&d={roc}",
]


def _is_amount_row(r) -> bool:
    # 櫃買中心實際格式：['', '融資金(仟元)', 前資餘額, 資買, 資賣, 現償, 資餘額, '', ...]，標籤在第二欄
    return (isinstance(r, list) and len(r) > 2 and not any(isinstance(x, (list, dict)) for x in r[:3])
            and any("融資金" in str(x) for x in r[:2]))


def _find_amount_row(obj):
    """在任意結構的 JSON 裡找『融資金額』那一列，回傳 (列, 欄位名稱或 None)"""
    if isinstance(obj, dict):
        fields = obj.get("fields") or obj.get("columns")
        for key in ("summary", "data", "tfootData_one", "tfootData_two", "tfootData"):
            rows = obj.get(key)
            if isinstance(rows, list):
                if _is_amount_row(rows):
                    return rows, fields
                for r in rows:
                    if _is_amount_row(r):
                        return r, fields
        for v in obj.values():
            hit = _find_amount_row(v)
            if hit:
                return hit
    elif isinstance(obj, list):
        if _is_amount_row(obj):
            return obj, None
        for v in obj:
            hit = _find_amount_row(v)
            if hit:
                return hit
    return None


def _parse_tpex_amount(j) -> float | None:
    hit = _find_amount_row(j)
    if not hit:
        return None
    row, fields = hit
    lab_i = next(k for k, x in enumerate(row[:2]) if "融資金" in str(x))
    label = str(row[lab_i])
    idx = None
    if fields:
        for k, f in enumerate(fields):
            if str(f).strip() in ("資餘額", "今日餘額", "本日餘額") or "今日餘額" in str(f):
                idx = k
                break
    nums = [_num(x) for x in row[lab_i + 1:]]
    nums = [x for x in nums if x is not None]
    if idx is not None and idx < len(row) and _num(row[idx]) is not None:
        v = _num(row[idx])
    elif nums:
        v = nums[-1]                              # 欄位順序：前日餘額、買進、賣出、現償、今日餘額
    else:
        return None
    v = v / 1e8 if ("元" in label and "仟" not in label and "千" not in label) else v / 1e5   # → 億元
    return v if 50 <= v <= 20000 else None       # 合理範圍外視為解析錯誤


def _margin_otc_one_day(d: pd.Timestamp) -> float | None:
    roc = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
    last_err = None
    for u in TPEX_URLS:
        try:
            j = json.loads(_get(u.format(ad=d.strftime("%Y/%m/%d"), roc=roc), timeout=30).decode("utf-8-sig"))
            v = _parse_tpex_amount(j)
            if v:
                return v
        except Exception as exc:  # noqa: BLE001
            last_err = exc
    if last_err:
        raise last_err
    return None


def fetch_margin_otc(tw_dates: list[pd.Timestamp], verbose: bool) -> pd.Series:
    """上櫃融資餘額（億元）"""
    return _fetch_daily("margin_otc.csv", "margin上櫃", _margin_otc_one_day, tw_dates, verbose)


# 融資維持率的分子：每日 Σ(個股融資餘額張 × 1000 × 收盤價)，依市場分開（億元）
MV_SQL = """
SELECT m.date, m.market,
       SUM(CASE WHEN p.close > 0 THEN m.bal * 1000.0 * p.close ELSE 0 END) / 1e8 AS mv
FROM margin m JOIN prices p ON p.code = m.code AND p.date = m.date
WHERE m.date >= ?
GROUP BY m.date, m.market
"""


def fetch_margin_mv(verbose: bool, since: str = "2024-01-01") -> dict:
    """讀 stock-screener/prices.db，回傳 {'twse': Series, 'otc': Series}（融資市值，億元）。
    結果快取在 data/panic/margin_mv_{twse,otc}.csv；每次只重算快取最後 5 天之後的日子。
    找不到資料庫就只用快取（沒有快取則回傳空的，網頁不顯示維持率）。"""
    out = {}
    caches = {"twse": CACHE_DIR / "margin_mv_twse.csv", "otc": CACHE_DIR / "margin_mv_otc.csv"}
    old = {k: _load_cache(p, "mv_100m") for k, p in caches.items()}
    if not PRICES_DB.exists():
        if verbose:
            print(f"[maint] 找不到 {PRICES_DB}，融資維持率只用快取")
        return {k: v for k, v in old.items() if len(v)}
    last = min((v.index.max() for v in old.values() if len(v)), default=None)
    start = (last - timedelta(days=7)).strftime("%Y-%m-%d") if last is not None else since
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{PRICES_DB}?mode=ro", uri=True)
        try:
            raw = pd.read_sql(MV_SQL, con, params=[start])
        finally:
            con.close()
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"[maint] 讀取 prices.db 失敗（{exc}），只用快取")
        return {k: v for k, v in old.items() if len(v)}
    raw["date"] = pd.to_datetime(raw["date"])
    for k, mkt in (("twse", "TWSE"), ("otc", "TPEx")):
        new = raw[raw["market"] == mkt].set_index("date")["mv"].astype(float)
        merged = pd.concat([x for x in (old[k], new) if len(x)]) if (len(old[k]) or len(new)) else pd.Series(dtype=float)
        if len(merged):
            out[k] = _save_cache(caches[k], merged, "mv_100m")
    if verbose and out:
        print(f"[maint] 融資市值 {start} 起重算，最新 {max(v.index.max() for v in out.values()):%Y-%m-%d}")
    return out


def maintenance_ratio(mv: dict, amt: dict) -> tuple[pd.Series, dict]:
    """融資維持率（%）＝融資市值 ÷ 融資金額。回傳（合計, {'twse':..., 'otc':...}）；
    上櫃那天缺資料時，合計只用上市（避免分子分母不一致）。"""
    parts = {}
    for k in ("twse", "otc"):
        if k in mv and k in amt and len(amt[k]):
            r = (mv[k] / amt[k] * 100).dropna()
            parts[k] = r[(r > 50) & (r < 400)]          # 排除明顯錯誤（資料缺漏）
    if not parts:
        return pd.Series(dtype=float), {}
    if "otc" in parts:
        idx = parts["twse"].index.intersection(parts["otc"].index)
        both = (mv["twse"].reindex(idx) + mv["otc"].reindex(idx)) / (amt["twse"].reindex(idx) + amt["otc"].reindex(idx)) * 100
        total = pd.concat([parts["twse"].drop(idx, errors="ignore"), both]).sort_index()
    else:
        total = parts["twse"]
    return total, parts


# --------------------------------------------------------------------------
# 指標與狀態
# --------------------------------------------------------------------------

def _pct_rank(s: pd.Series, window: int) -> pd.Series:
    """今天的值在過去 window 天（含今天）的百分位，0~1"""
    return s.rolling(window, min_periods=window // 2).apply(lambda x: float((x <= x[-1]).mean()), raw=True)


def realized_vol(close: pd.Series, n: int = 10) -> pd.Series:
    r = np.log(close).diff()
    return r.rolling(n, min_periods=n).std() * math.sqrt(252) * 100


def compute(close: pd.Series, vol: pd.Series, net_oi: pd.Series, margin: pd.Series,
            parts: dict | None = None, maint: pd.Series | None = None,
            maint_parts: dict | None = None) -> pd.DataFrame:
    """margin＝判斷用的融資餘額（上市＋上櫃，或只有上市）；parts＝{'twse':..., 'otc':...} 另外算各自的10日變化
    maint＝融資維持率（%，可為空）；沒有資料的日子不算恐慌"""
    idx = close.index
    df = pd.DataFrame(index=idx)
    df["close"] = close
    df["vol"] = vol.reindex(idx)
    df["short"] = (-net_oi).reindex(idx)                  # 淨空單口數（正數＝空單）
    df["margin"] = margin.reindex(idx)
    df["maint"] = (maint if maint is not None else pd.Series(dtype=float)).reindex(idx)
    for k, ser in (maint_parts or {}).items():
        df[f"{k}_maint"] = ser.reindex(idx)

    df["vol_pct"] = _pct_rank(df["vol"], PCT_WINDOW)
    df["short_pct"] = _pct_rank(df["short"], PCT_WINDOW)
    df["margin_chg10"] = df["margin"] / df["margin"].shift(10) - 1
    df["maint_chg10"] = df["maint"] - df["maint"].shift(10)   # 維持率 10 日變化（百分點）

    df["vol_panic"] = df["vol_pct"] >= VOL_PANIC_PCT
    df["short_panic"] = (df["short_pct"] >= SHORT_PANIC_PCT) & (df["short"] > 0)
    df["margin_panic"] = df["margin_chg10"] <= MARGIN_PANIC_CHG
    df["maint_panic"] = df["maint"] < MAINT_PANIC          # NaN → False
    df["maint_capit"] = df["maint"] < MAINT_CAPIT

    vmax10 = df["vol"].rolling(10, min_periods=3).max()
    smax10 = df["short"].rolling(10, min_periods=3).max()
    df["vol_ease"] = (df["vol_panic"].astype(float).rolling(10, min_periods=1).max() == 1) & \
                     (df["vol"] <= vmax10 * (1 - VOL_EASE_DROP)) & (df["vol"] < df["vol"].rolling(5, min_periods=3).mean())
    df["short_ease"] = (df["short_panic"].astype(float).rolling(10, min_periods=1).max() == 1) & \
                       (df["short"] <= smax10 * (1 - SHORT_EASE_DROP))
    has_margin = df["margin_chg10"].notna()
    # 已洗＝近 N 日內曾「融資 10 日大減」且同一天維持率也在下滑（價跌＋砍倉，才是真的洗）；
    # 維持率持平或上升時的融資減少多半是自願還款，不算。當天沒有維持率資料就只看融資。
    maint_down = (df["maint_chg10"] < MAINT_WASH_DROP) | df["maint_chg10"].isna()
    df["wash_event"] = df["margin_panic"] & maint_down
    df["margin_washed"] = (df["wash_event"].astype(float).rolling(MARGIN_WASH_LOOKBACK, min_periods=1).max() == 1) | ~has_margin
    df["margin_stable"] = (df["margin"].diff() >= 0) & (df["margin"].diff().shift(1) >= 0)

    df["n_panic"] = df[["vol_panic", "short_panic", "margin_panic", "maint_panic"]].astype(int).sum(axis=1)
    df["heat"] = df["n_panic"] >= 2
    recent_heat = df["heat"].astype(float).rolling(HEAT_LOOKBACK, min_periods=1).max() == 1
    df["ease"] = recent_heat & (df["vol_ease"] | df["short_ease"]) & df["margin_washed"] & \
                 ((df["n_panic"] <= 1) | (df["vol_ease"] & df["short_ease"]))

    df["capit"] = df["margin_chg10"] <= MARGIN_CAPIT_CHG
    for k, ser in (parts or {}).items():          # 上市、上櫃各自的斷頭（比較誰先觸發）
        m = ser.reindex(idx)
        df[f"{k}_margin"] = m
        df[f"{k}_chg10"] = m / m.shift(10) - 1
        df[f"capit_{k}"] = df[f"{k}_chg10"] <= MARGIN_CAPIT_CHG
    df["capit_recent"] = df["capit"].astype(float).rolling(CAPIT_SHOW_DAYS, min_periods=1).max() == 1

    ready = df["vol_pct"].notna() & df["short_pct"].notna()
    df["state"] = np.select([~ready, df["ease"], df["capit_recent"], df["heat"], df["n_panic"] == 1],
                            ["unknown", "ease", "capit", "heat", "watch"], default="calm")
    return df


def backtest(df: pd.DataFrame) -> dict:
    """各狀態之後加權指數 5/10/20 日表現（收盤到收盤），以及每次「恐慌退燒」首日離真正低點多遠"""
    d = df[df["state"] != "unknown"].copy()
    for h in HORIZONS:
        d[f"r{h}"] = (df["close"].shift(-h) / df["close"] - 1).reindex(d.index) * 100
    out = {"n_days": int(len(d)),
           "start": d.index[0].strftime("%Y-%m-%d") if len(d) else None,
           "end": d.index[-1].strftime("%Y-%m-%d") if len(d) else None,
           "buckets": []}
    for key in ["all", "calm", "watch", "heat", "capit", "ease"]:
        g = d if key == "all" else d[d["state"] == key]
        row = {"key": key, "label": "全部日子" if key == "all" else STATE_TEXT[key], "n": int(len(g))}
        for h in HORIZONS:
            x = g[f"r{h}"].dropna()
            row[f"avg{h}"] = _r(x.mean(), 2) if len(x) else None
            row[f"win{h}"] = _r((x > 0).mean() * 100, 1) if len(x) else None
        out["buckets"].append(row)

    out["events"] = _events(df, d, "ease")[-12:]
    out["capit_events"] = _events(df, d, "capit")[-12:]
    for k in ("twse", "otc"):                     # 上市、上櫃各自單獨觸發的時間（只供比較）
        if f"capit_{k}" in df:
            tmp = d.copy()
            tmp["state"] = np.where(df[f"capit_{k}"].reindex(d.index).fillna(False), f"capit_{k}", "x")
            out[f"capit_{k}_events"] = _events(df, tmp, f"capit_{k}")[-12:]
    return out


def _events(df: pd.DataFrame, d: pd.DataFrame, state: str) -> list:
    """某狀態每一段的首日：離前後 20 日內的波段低點幾天、之後再跌多少、之後 N 日報酬"""
    starts = d.index[(d["state"] == state) & (d["state"].shift(1) != state)]
    events = []
    for t in starts:
        i = df.index.get_loc(t)
        win = df["close"].iloc[max(0, i - 20): i + 21]
        low_day = win.idxmin()
        fut = df["close"].iloc[i: i + 21]
        ev = {"date": t.strftime("%Y-%m-%d"), "close": _r(df["close"].iloc[i], 2),
              "low_date": low_day.strftime("%Y-%m-%d"),
              "days_from_low": int(df.index.get_loc(low_day) - i),
              "pct_above_low": _r((df["close"].iloc[i] / win.min() - 1) * 100, 2),
              "further_drop": _r((fut.min() / df["close"].iloc[i] - 1) * 100, 2)}
        for h in HORIZONS:
            ev[f"r{h}"] = _r((df["close"].shift(-h).iloc[i] / df["close"].iloc[i] - 1) * 100, 2) \
                if i + h < len(df) else None
        events.append(ev)
    return events


# --------------------------------------------------------------------------
# 輸出
# --------------------------------------------------------------------------

def _read_breadth() -> pd.DataFrame:
    try:
        b = pd.read_csv(BREADTH_CSV, encoding="utf-8-sig", parse_dates=["日期"]).set_index("日期").sort_index()
        return b[~b.index.duplicated(keep="last")]
    except Exception:  # noqa: BLE001   沒有 stock-screener 也能跑，只是少了家數
        return pd.DataFrame()


def mood_score(df: pd.DataFrame, breadth: pd.DataFrame) -> tuple[pd.Series, pd.DataFrame]:
    """情緒分數 0~100（越高越恐慌）＝下列各項 0~100 的平均（缺資料的項目不算）：
       下跌家數占比、空方擴散度（60% 視為滿分）、外資期貨淨空單百分位、波動度百分位、融資維持率（200%→0、140%→100）"""
    idx = df.index
    parts = pd.DataFrame(index=idx)
    if len(breadth):
        b = breadth.reindex(idx)
        parts["下跌家數占比"] = b["下跌"] / (b["上漲"] + b["下跌"]) * 100
        parts["空方擴散度"] = (b["空方擴散度%"] / 60 * 100).clip(0, 100)
    parts["外資期貨空單"] = df["short_pct"] * 100
    parts["波動度"] = df["vol_pct"] * 100
    parts["融資維持率"] = ((200 - df["maint"]) / 60 * 100).clip(0, 100)
    return parts.mean(axis=1, skipna=True).round(0), parts


def ma_keys(close: pd.Series, ns=(20, 60), ahead: int = 5) -> dict:
    """均線扣抵價位：下一交易日要扣掉的收盤（＝MA 翻轉門檻）＋之後幾天的扣抵值
    下一交易日收盤 > 扣抵值 → 該均線上揚；< 扣抵值 → 下彎"""
    c = close.dropna()
    out = {}
    for n in ns:
        if len(c) < n + 1:
            continue
        ma, ma_prev = c.iloc[-n:].mean(), c.iloc[-n - 1:-1].mean()
        ded = c.iloc[-n:-n + ahead] if ahead < n else c.iloc[-n:]
        out[f"ma{n}"] = {
            "n": n, "ma": _r(ma, 2), "chg": _r(ma - ma_prev, 2),
            "dir": "up" if ma > ma_prev else ("down" if ma < ma_prev else "flat"),
            "ded": [{"date": d.strftime("%Y-%m-%d"), "v": _r(v, 2)} for d, v in ded.items()],
        }
    return out


def today_block(df: pd.DataFrame) -> dict:
    """首頁「今天市場」卡片：大盤漲跌、外資買賣超（近 10 日）、情緒分數、漲跌家數"""
    t = df.index[-1]
    close = df["close"]
    prev = close.iloc[-2] if len(close) >= 2 else np.nan
    breadth = _read_breadth()
    score, parts = mood_score(df, breadth)
    sc = score.iloc[-1]
    band = next((b for b in MOOD_BANDS if pd.notna(sc) and sc >= b[0]), (None, "無資料", "off"))
    out = {
        "date": t.strftime("%Y-%m-%d"),
        "close": _r(close.iloc[-1], 2),
        "chg_pct": _r((close.iloc[-1] / prev - 1) * 100, 2) if pd.notna(prev) else None,
        "score": int(sc) if pd.notna(sc) else None,
        "score_prev": int(score.iloc[-2]) if len(score) >= 2 and pd.notna(score.iloc[-2]) else None,
        "label": band[1], "c": band[2],
        "parts": [{"name": k, "v": _r(v, 0)} for k, v in parts.iloc[-1].items() if pd.notna(v)],
        "bands": [[b[0], b[1]] for b in MOOD_BANDS],
    }
    if len(breadth) and t in breadth.index:
        r = breadth.loc[t]
        up, dn = int(r["上漲"]), int(r["下跌"])
        out.update({"up": up, "down": dn, "flat": int(r["平盤"]) if pd.notna(r.get("平盤")) else None,
                    "down_ratio": _r(dn / (up + dn) * 100, 1) if up + dn else None})
    try:
        f = pd.read_csv(FLOW_CSV, parse_dates=["date"]).set_index("date")["net_100m_all"].sort_index()
        f = f.loc[:t].tail(10)
        out["flow"] = {"date": f.index[-1].strftime("%Y-%m-%d"), "net": _r(f.iloc[-1], 1),
                       "hist": [_r(x, 1) for x in f.values], "dates": [d.strftime("%Y-%m-%d") for d in f.index]}
    except Exception:  # noqa: BLE001
        out["flow"] = None
    return out


def build_payload(df: pd.DataFrame, vol_source: str, vix: pd.Series, bt: dict,
                  margin_src: str = "twse", otc_days: int = 0) -> dict:
    margin_src_label = "上市＋上櫃（億元）" if margin_src == "all" else "上市（億元）"
    last = df.iloc[-1]
    t = df.index[-1]

    def flag(p, e):
        return "ease" if e else ("panic" if p else "normal")

    def spark(col, n=120):
        s = df[col].tail(n)
        return [None if pd.isna(v) else _r(v, 3) for v in s]

    vol_label = "台指VIX" if vol_source == "vix" else "加權指數10日實現波動率"
    # 融資餘額證交所較晚公布（約晚上 9 點後）。今天還沒出來就沿用最近一個有資料的交易日，並標出日期。
    has_m = df["margin"].notna()
    m = df[has_m].iloc[-1] if pd.isna(last["margin"]) and has_m.any() else last
    m_date = (df.index[has_m][-1] if pd.isna(last["margin"]) and has_m.any() else t).strftime("%Y-%m-%d")

    def chg1(col):
        """融資餘額單日增減（億元）＝這一天 − 前一個有資料的交易日"""
        if col not in df:
            return None
        s_ = df[col].loc[:pd.Timestamp(m_date)].dropna()
        return _r(s_.iloc[-1] - s_.iloc[-2], 2) if len(s_) >= 2 else None
    # 融資維持率：跟融資餘額一樣晚公布，沿用最近一個有資料的交易日
    has_mt = df["maint"].notna()
    mt_row = df[has_mt].iloc[-1] if has_mt.any() else None
    mt_date = df.index[has_mt][-1].strftime("%Y-%m-%d") if has_mt.any() else None

    def mt_chg1(col):
        if col not in df or mt_date is None:
            return None
        s_ = df[col].loc[:pd.Timestamp(mt_date)].dropna()
        return _r(s_.iloc[-1] - s_.iloc[-2], 1) if len(s_) >= 2 else None

    comps = [
        {"key": "vol", "label": "波動度", "sub": vol_label, "value": _r(last["vol"], 2), "unit": "%" if vol_source != "vix" else "",
         "pct": _r(last["vol_pct"] * 100, 0) if pd.notna(last["vol_pct"]) else None,
         "flag": flag(last["vol_panic"], last["vol_ease"]),
         "rule": f"≥ 近{PCT_WINDOW}日第{int(VOL_PANIC_PCT * 100)}百分位＝恐慌；從10日高點回落{int(VOL_EASE_DROP * 100)}%以上＝退燒",
         "spark": spark("vol")},
        {"key": "short", "label": "外資期貨淨空單", "sub": "台指期（口）", "value": _r(last["short"], 0), "unit": "口",
         "pct": _r(last["short_pct"] * 100, 0) if pd.notna(last["short_pct"]) else None,
         "flag": flag(last["short_panic"], last["short_ease"]),
         "rule": f"≥ 近{PCT_WINDOW}日第{int(SHORT_PANIC_PCT * 100)}百分位＝恐慌；從10日高點減少{int(SHORT_EASE_DROP * 100)}%以上＝回補",
         "spark": spark("short")},
        {"key": "margin", "label": "融資餘額", "sub": margin_src_label, "value": _r(m["margin"], 0), "unit": "億",
         "date": m_date, "lagged": m_date != t.strftime("%Y-%m-%d"), "chg1": chg1("margin"),
         "parts": {k: {"value": _r(m.get(f"{k}_margin"), 0), "chg1": chg1(f"{k}_margin"),
                       "chg10": _r(m.get(f"{k}_chg10", np.nan) * 100, 2) if pd.notna(m.get(f"{k}_chg10", np.nan)) else None}
                   for k in ("twse", "otc") if f"{k}_margin" in df},
         "chg10": _r(m["margin_chg10"] * 100, 2) if pd.notna(m["margin_chg10"]) else None,
         "flag": "capit" if last["capit_recent"] else "panic" if m["margin_panic"] else ("washed" if (m["margin_washed"] and pd.notna(m["margin_chg10"])) else "normal"),
         "stable": bool(m["margin_stable"]) if pd.notna(m["margin"]) else None,
         "rule": f"10日減少{abs(int(MARGIN_PANIC_CHG * 100))}%以上＝去槓桿（恐慌）；{abs(int(MARGIN_CAPIT_CHG * 100))}%以上＝斷頭潮；近{MARGIN_WASH_LOOKBACK}日內發生過且當時維持率10日同步下滑＝籌碼已洗",
         "spark": spark("margin")},
    ]
    if mt_row is not None:
        v = mt_row["maint"]
        comps.append(
            {"key": "maint", "label": "融資維持率", "sub": "上市＋上櫃（融資市值 ÷ 融資金額）", "value": _r(v, 1), "unit": "%",
             "date": mt_date, "lagged": mt_date != t.strftime("%Y-%m-%d"), "chg1": mt_chg1("maint"),
             "parts": {k: {"value": _r(mt_row.get(f"{k}_maint"), 1), "chg1": mt_chg1(f"{k}_maint")}
                       for k in ("twse", "otc") if f"{k}_maint" in df and pd.notna(mt_row.get(f"{k}_maint"))},
             "pct": _r(_pct_rank(df["maint"].dropna(), PCT_WINDOW).iloc[-1] * 100, 0)
             if df["maint"].notna().sum() >= PCT_WINDOW // 2 else None,
             "flag": "capit" if v < MAINT_CAPIT else "panic" if v < MAINT_PANIC else "normal",
             "rule": f"< {MAINT_PANIC:.0f}%＝恐慌；< {MAINT_CAPIT:.0f}%＝斷頭區（個股低於 130% 會被追繳）",
             "spark": spark("maint")})
    hist = [[d.strftime("%Y-%m-%d"), s] for d, s in df["state"].tail(60).items()]
    flips = df["state"][df["state"] != df["state"].shift(1)].tail(8)
    payload = {
        "schema": 1,
        "generated_at": datetime.now(TPE).isoformat(timespec="minutes"),
        "date": t.strftime("%Y-%m-%d"),
        "close": _r(last["close"], 2),
        "state": last["state"], "state_text": STATE_TEXT[last["state"]], "action": ACTION_TEXT[last["state"]],
        "n_panic": int(last["n_panic"]),
        "n_total": len(comps),
        "capit": ({"date": df.index[df["capit"]][-1].strftime("%Y-%m-%d"),
                   "close": _r(df.loc[df["capit"], "close"].iloc[-1], 2)} if bool(last["capit_recent"]) else None),
        # 最近一次斷頭（給網頁判斷「斷頭後恐慌又升溫」時，試單部位該續抱）
        "last_capit": ({"date": df.index[df["capit"]][-1].strftime("%Y-%m-%d"),
                        "close": _r(df.loc[df["capit"], "close"].iloc[-1], 2),
                        "days_ago": int(len(df) - 1 - df.index.get_loc(df.index[df["capit"]][-1]))}
                       if bool(df["capit"].any()) else None),
        "since": _since(df),
        "vol_source": vol_source,
        "vix": {"value": _r(vix.iloc[-1], 2), "date": vix.index[-1].strftime("%Y-%m-%d"), "days": int(len(vix))} if len(vix) else None,
        "components": comps,
        "today": today_block(df),
        "spark_dates": [d.strftime("%Y-%m-%d") for d in df.index[-120:]],
        "history": hist,
        "flips": [[d.strftime("%Y-%m-%d"), s, STATE_TEXT[s]] for d, s in flips.items()],
        "backtest": bt,
        "notes": [],
    }
    if margin_src != "all":
        payload["notes"].append(f"上櫃融資已累積 {otc_days} 天，補齊後自動改用上市＋上櫃合計判斷；目前只用上市。")
    if vol_source != "vix":
        payload["notes"].append(f"台指VIX 已累積 {len(vix)} 天，滿 {VIX_MIN_DAYS} 天後自動改用VIX判斷；目前用加權指數實現波動率代替。")
    payload["sig"] = (f"v5|{margin_src}|{payload['date']}|{payload['close']}|{comps[1]['value']}|{comps[2]['value']}|{payload['vix']}"
                      f"|{comps[3]['value'] if len(comps) > 3 else None}|{comps[3]['date'] if len(comps) > 3 else None}"
                      f"|{payload['today'].get('up')}|{payload['today'].get('down')}|{(payload['today'].get('flow') or {}).get('date')}")
    return payload


def _since(df: pd.DataFrame) -> str:
    s = df["state"]
    cur = s.iloc[-1]
    i = len(s) - 1
    while i > 0 and s.iloc[i - 1] == cur:
        i -= 1
    return s.index[i].strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# 合成資料（離線測試）
# --------------------------------------------------------------------------

def synthetic(n: int = 520):
    rng = np.random.default_rng(7)
    idx = pd.bdate_range(end=pd.Timestamp("2026-10-16"), periods=n)
    r = rng.normal(0.0006, 0.009, n)
    for c in (150, 330, 470):                          # 三次急跌 + 反彈
        r[c:c + 12] -= 0.012
        r[c + 12:c + 22] += 0.010
    close = pd.Series(18000 * np.exp(np.cumsum(r)), idx)
    short = pd.Series(30000 + rng.normal(0, 4000, n), idx)
    margin = pd.Series(3000 + np.cumsum(rng.normal(1.5, 6, n)), idx)
    for c in (150, 330, 470):
        short.iloc[c + 4:c + 14] += np.linspace(5000, 45000, 10)
        short.iloc[c + 14:c + 22] += np.linspace(40000, 5000, 8)
        margin.iloc[c + 6:c + 14] -= np.linspace(20, 220, 8)
        margin.iloc[c + 14:] -= 220
    otc = margin * 0.35 + pd.Series(rng.normal(0, 3, n), idx)
    return close, -short, margin, otc


def synthetic_mv(close: pd.Series, m_twse: pd.Series, m_otc: pd.Series) -> dict:
    """離線測試用：融資市值跟著指數漲跌（基準維持率約 170%）"""
    k = close / close.rolling(60, min_periods=1).mean()
    return {"twse": m_twse * 1.70 * k, "otc": m_otc * 1.70 * k}


# --------------------------------------------------------------------------

def probe():
    print("=== 測試三個資料來源 ===")
    today = datetime.now(TPE).date()
    try:
        ms = _month_list(1)[0]
        t = _get(f"https://www.taifex.com.tw/file/taifex/Dailydownload/vix/log2data/{ms:%Y%m}new.txt").decode("cp950", "replace")
        print("[vix] OK，前 4 行：\n" + "\n".join(t.splitlines()[:4]))
    except Exception as exc:  # noqa: BLE001
        print(f"[vix] 失敗：{exc}")
    try:
        df = _oi_window(today - timedelta(days=20), today - timedelta(days=1))
        print("[oi] OK\n" + (df.tail(3).to_string() if df is not None else "回傳不是CSV（結束日可能還沒有資料）"))
    except Exception as exc:  # noqa: BLE001
        print(f"[oi] 失敗：{exc}")
    for k in range(1, 6):
        d = pd.Timestamp(today - timedelta(days=k))
        try:
            url = ("https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&selectType=MS&date="
                   + d.strftime("%Y%m%d"))
            j = json.loads(_get(url).decode("utf-8"))
            print(f"[margin] {d:%Y-%m-%d} stat={j.get('stat')} keys={list(j.keys())}")
            for tb in j.get("tables", [])[:2]:
                print("   title:", tb.get("title"), "| fields:", tb.get("fields"))
                for row in (tb.get("data") or [])[:3]:
                    print("   ", row)
            print("   解析結果（億元）：", _margin_one_day(d))
            if j.get("stat") == "OK":
                break
        except Exception as exc:  # noqa: BLE001
            print(f"[margin] {d:%Y-%m-%d} 失敗：{exc}")
        time.sleep(1.2)


def probe_tpex():
    print("=== 測試上櫃融資（櫃買中心）===")
    today = datetime.now(TPE).date()
    for k in range(1, 8):
        d = pd.Timestamp(today - timedelta(days=k))
        if d.weekday() >= 5:
            continue
        roc = f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"
        for u in TPEX_URLS:
            url = u.format(ad=d.strftime("%Y/%m/%d"), roc=roc)
            print(f"\n[otc] {d:%Y-%m-%d} {url}")
            try:
                raw = _get(url, timeout=30).decode("utf-8-sig")
                j = json.loads(raw)
                print("   最上層欄位：", list(j.keys()) if isinstance(j, dict) else type(j).__name__)
                if isinstance(j, dict):
                    for key, v in j.items():
                        if isinstance(v, list):
                            print(f"   {key}: list({len(v)})", str(v[:2])[:300])
                        elif isinstance(v, dict):
                            print(f"   {key}: dict keys={list(v.keys())[:10]}")
                        else:
                            print(f"   {key}: {str(v)[:120]}")
                    for t in (j.get("tables") or [])[:4]:
                        print("   table:", t.get("title"), "| fields:", t.get("fields"))
                        for key in ("summary", "data"):
                            rows = t.get(key) or []
                            print(f"     {key}({len(rows)}) 前2列:", str(rows[:2])[:300], " 末2列:", str(rows[-2:])[:300])
                hit = _find_amount_row(j)
                print("   找到的融資金額列：", hit[0] if hit else None)
                print("   解析結果（億元）：", _parse_tpex_amount(j))
            except Exception as exc:  # noqa: BLE001
                print(f"   失敗：{exc}")
            time.sleep(1.2)
        return


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/panic.json")
    ap.add_argument("--offline-test", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()
    verbose = not args.quiet
    if args.probe:
        probe()
        probe_tpex()
        return 0
    out = Path(args.out)

    try:
        if args.offline_test:
            close, net_oi, m_twse, m_otc = synthetic()
            vix = pd.Series(dtype=float)
            mv = synthetic_mv(close, m_twse, m_otc)
        else:
            close = fetch_taiex_twse(DATA_DIR / "taiex_twse.csv", verbose)
            if len(close) < 150:
                raise RuntimeError("加權指數歷史不足（請先執行 market_corr.py 建立 data/taiex_twse.csv）")
            dates = list(close.index)
            vix = fetch_vix(verbose)
            net_oi = fetch_foreign_short(dates, verbose)
            m_twse = fetch_margin(dates, verbose)
            m_otc = fetch_margin_otc(dates, verbose)
            try:
                mv = fetch_margin_mv(verbose)
            except Exception as exc:  # noqa: BLE001   維持率失敗不影響其他三項
                print(f"[maint] 略過融資維持率：{exc}")
                mv = {}
            if len(net_oi) < 60:
                raise RuntimeError("外資期貨未平倉資料不足")

        # 上櫃補齊（覆蓋上市 95% 以上的日子）才用合計，否則先只用上市
        both = m_twse.index.intersection(m_otc.index)
        if len(m_otc) and len(both) >= 0.95 * len(m_twse):
            margin, margin_src = (m_twse.reindex(both) + m_otc.reindex(both)), "all"
        else:
            margin, margin_src = m_twse, "twse"
        parts = {"twse": m_twse, "otc": m_otc} if len(m_otc) else {"twse": m_twse}

        maint, maint_parts = maintenance_ratio(mv, {"twse": m_twse, "otc": m_otc})

        use_vix = len(vix) >= VIX_MIN_DAYS
        vol = vix if use_vix else realized_vol(close)
        df = compute(close, vol, net_oi, margin, parts, maint, maint_parts)
        # 最後一天三項資料都要有（避免期交所/證交所還沒公布時誤判）
        df = df[df["short"].notna() & df["vol"].notna()]
        if not len(df):
            raise RuntimeError("沒有可計算的日子")
        bt = backtest(compute(close, realized_vol(close), net_oi, margin, parts, maint, maint_parts))   # 回測一律用實現波動率（歷史較長）
        payload = build_payload(df, "vix" if use_vix else "rv", vix, bt, margin_src, len(m_otc))
        # 均線扣抵用完整的加權收盤（df 已濾掉期貨/波動率缺資料的日子，不能拿來算均線）
        try:
            payload["today"]["ma_keys"] = ma_keys(close.loc[:df.index[-1]])
        except Exception as exc:  # noqa: BLE001   扣抵失敗不影響恐慌儀表
            print(f"[ma_keys] 略過均線扣抵：{exc}")
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 恐慌儀表產生失敗：{exc}", file=sys.stderr)
        return 1

    if out.exists() and not args.force and not args.offline_test:
        try:
            if json.loads(out.read_text(encoding="utf-8")).get("sig") == payload["sig"]:
                if verbose:
                    print("[info] 恐慌儀表沒有新資料，不更新")
                return 0
        except Exception:  # noqa: BLE001
            pass

    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)

    if verbose:
        print(f"[ok] {out}")
        print(f"     恐慌儀表（{payload['date']}）：【{payload['state_text']}】自 {payload['since']} 起 → {payload['action']}")
        td = payload["today"]
        print(f"     今天市場：大盤 {td['chg_pct']}%・情緒 {td['score']} {td['label']}・上漲 {td.get('up')}／下跌 {td.get('down')}"
              f"・外資 {(td.get('flow') or {}).get('net')} 億")
        for c in payload["components"]:
            extra = f"  百分位 {c['pct']}" if c.get("pct") is not None else ""
            extra += f"  10日變化 {c['chg10']:+.2f}%" if c.get("chg10") is not None else ""
            extra += f"  （{c['date']}）" if c.get("lagged") else ""
            print(f"     {c['label']:<8} {c['value']}{c['unit']}{extra}  [{c['flag']}]")
        b = payload["backtest"]
        print(f"     回測 {b['start']} ~ {b['end']}（{b['n_days']} 天，用實現波動率）")
        for r in b["buckets"]:
            print(f"       {r['label']:<6} n={r['n']:<4} 5日 {r['avg5']}% (勝率 {r['win5']}%)  "
                  f"10日 {r['avg10']}%  20日 {r['avg20']}% (勝率 {r['win20']}%)")
        for k, nm in (("twse", "上市"), ("otc", "上櫃")):
            for e in b.get(f"capit_{k}_events", []):
                print(f"       {nm}單獨斷頭 {e['date']}：距低點 {e['days_from_low']:+d} 天、之後再跌 {e['further_drop']}%、20日 {e['r20']}%")
        for e in b.get("capit_events", []):
            print(f"       斷頭 {e['date']}：距低點 {e['days_from_low']:+d} 天、之後再跌 {e['further_drop']}%、20日 {e['r20']}%")
        for e in b["events"]:
            print(f"       退燒 {e['date']}：距低點 {e['days_from_low']:+d} 天、高於低點 {e['pct_above_low']}%、"
                  f"之後再跌 {e['further_drop']}%、20日 {e['r20']}%")
        for n in payload["notes"]:
            print(f"     ※ {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
