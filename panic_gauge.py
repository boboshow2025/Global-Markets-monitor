# -*- coding: utf-8 -*-
"""
panic_gauge.py  --  恐慌儀表（Global-Markets-monitor）

回答一個問題：這波跌勢是不是接近尾聲、可以開始布局了？

三個指標，各自和「自己過去一段時間」比較（百分位），不用固定數字：
  1. 波動度      台指VIX（期交所，累積滿 120 天後啟用）；之前用加權指數 10 日實現波動率代替
  2. 外資期貨空單 外資台指期淨空單口數（期交所三大法人 CSV）
  3. 融資餘額    上市融資金額 10 日變化（證交所信用交易統計）

狀態：
  平靜      三項都正常
  警戒      一項進入恐慌區
  恐慌升溫  兩項以上進入恐慌區
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
VIX_MIN_DAYS = 120          # 台指VIX 累積滿這麼多天才取代實現波動率
BACKFILL_DAYS = 500         # 第一次補歷史的交易日數（約兩年）
HORIZONS = [5, 10, 20]

DATA_DIR = Path("data")
CACHE_DIR = DATA_DIR / "panic"

STATE_TEXT = {"calm": "平靜", "watch": "警戒", "heat": "恐慌升溫", "ease": "恐慌退燒", "unknown": "無資料"}
ACTION_TEXT = {
    "calm": "沒有恐慌跡象，依資金輪動儀表操作",
    "watch": "單一指標轉差，留意是否擴散",
    "heat": "恐慌擴散中，不急著接刀",
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


def fetch_margin(tw_dates: list[pd.Timestamp], verbose: bool) -> pd.Series:
    """上市融資餘額（億元）。逐日抓，第一次補 BACKFILL_DAYS 天（約 10 分鐘），之後只補缺的日子。"""
    cache = CACHE_DIR / "margin.csv"
    old = _load_cache(cache, "margin_100m")
    want = [d for d in tw_dates[-BACKFILL_DAYS:] if d not in old.index]
    if verbose and len(want) > 20:
        print(f"[margin] 需要補 {len(want)} 天融資資料（證交所限制查詢頻率，約 {len(want) * 1.3 / 60:.0f} 分鐘）...")
    got, fails = {}, 0
    for i, d in enumerate(want):
        try:
            v = _margin_one_day(d)
            fails = 0
            if v:
                got[d] = v
        except Exception as exc:  # noqa: BLE001
            fails += 1
            if verbose:
                print(f"[margin] {d:%Y-%m-%d} 失敗：{exc}")
            if fails >= 5:
                print("[margin] 連續失敗 5 次，先用已有資料（下次執行會接著補）")
                break
        if verbose and len(want) > 20 and i % 50 == 0:
            print(f"[margin] {i}/{len(want)} ...")
        if len(got) and len(got) % 40 == 0:      # 補歷史途中定期存檔，中斷也不會白抓
            _save_cache(cache, pd.concat([x for x in (old, pd.Series(got, dtype=float)) if len(x)]), "margin_100m")
        time.sleep(1.2)
    s = pd.concat([x for x in (old, pd.Series(got, dtype=float)) if len(x)]) if (len(old) or got) else old
    if len(s):
        s = _save_cache(cache, s, "margin_100m")
    if verbose:
        print(f"[margin] 上市融資餘額 {len(s)} 天" + (f"，最新 {s.index[-1]:%Y-%m-%d} {s.iloc[-1]:,.0f} 億" if len(s) else ""))
    return s


# --------------------------------------------------------------------------
# 指標與狀態
# --------------------------------------------------------------------------

def _pct_rank(s: pd.Series, window: int) -> pd.Series:
    """今天的值在過去 window 天（含今天）的百分位，0~1"""
    return s.rolling(window, min_periods=window // 2).apply(lambda x: float((x <= x[-1]).mean()), raw=True)


def realized_vol(close: pd.Series, n: int = 10) -> pd.Series:
    r = np.log(close).diff()
    return r.rolling(n, min_periods=n).std() * math.sqrt(252) * 100


def compute(close: pd.Series, vol: pd.Series, net_oi: pd.Series, margin: pd.Series) -> pd.DataFrame:
    idx = close.index
    df = pd.DataFrame(index=idx)
    df["close"] = close
    df["vol"] = vol.reindex(idx)
    df["short"] = (-net_oi).reindex(idx)                  # 淨空單口數（正數＝空單）
    df["margin"] = margin.reindex(idx)

    df["vol_pct"] = _pct_rank(df["vol"], PCT_WINDOW)
    df["short_pct"] = _pct_rank(df["short"], PCT_WINDOW)
    df["margin_chg10"] = df["margin"] / df["margin"].shift(10) - 1

    df["vol_panic"] = df["vol_pct"] >= VOL_PANIC_PCT
    df["short_panic"] = (df["short_pct"] >= SHORT_PANIC_PCT) & (df["short"] > 0)
    df["margin_panic"] = df["margin_chg10"] <= MARGIN_PANIC_CHG

    vmax10 = df["vol"].rolling(10, min_periods=3).max()
    smax10 = df["short"].rolling(10, min_periods=3).max()
    df["vol_ease"] = (df["vol_panic"].astype(float).rolling(10, min_periods=1).max() == 1) & \
                     (df["vol"] <= vmax10 * (1 - VOL_EASE_DROP)) & (df["vol"] < df["vol"].rolling(5, min_periods=3).mean())
    df["short_ease"] = (df["short_panic"].astype(float).rolling(10, min_periods=1).max() == 1) & \
                       (df["short"] <= smax10 * (1 - SHORT_EASE_DROP))
    has_margin = df["margin_chg10"].notna()
    df["margin_washed"] = (df["margin_panic"].astype(float).rolling(MARGIN_WASH_LOOKBACK, min_periods=1).max() == 1) | ~has_margin
    df["margin_stable"] = (df["margin"].diff() >= 0) & (df["margin"].diff().shift(1) >= 0)

    df["n_panic"] = df[["vol_panic", "short_panic", "margin_panic"]].astype(int).sum(axis=1)
    df["heat"] = df["n_panic"] >= 2
    recent_heat = df["heat"].astype(float).rolling(HEAT_LOOKBACK, min_periods=1).max() == 1
    df["ease"] = recent_heat & (df["vol_ease"] | df["short_ease"]) & df["margin_washed"] & \
                 ((df["n_panic"] <= 1) | (df["vol_ease"] & df["short_ease"]))

    ready = df["vol_pct"].notna() & df["short_pct"].notna()
    df["state"] = np.select([~ready, df["ease"], df["heat"], df["n_panic"] == 1],
                            ["unknown", "ease", "heat", "watch"], default="calm")
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
    for key in ["all", "calm", "watch", "heat", "ease"]:
        g = d if key == "all" else d[d["state"] == key]
        row = {"key": key, "label": "全部日子" if key == "all" else STATE_TEXT[key], "n": int(len(g))}
        for h in HORIZONS:
            x = g[f"r{h}"].dropna()
            row[f"avg{h}"] = _r(x.mean(), 2) if len(x) else None
            row[f"win{h}"] = _r((x > 0).mean() * 100, 1) if len(x) else None
        out["buckets"].append(row)

    # 恐慌退燒的每一段（首日）
    starts = d.index[(d["state"] == "ease") & (d["state"].shift(1) != "ease")]
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
    out["events"] = events[-12:]
    return out


# --------------------------------------------------------------------------
# 輸出
# --------------------------------------------------------------------------

def build_payload(df: pd.DataFrame, vol_source: str, vix: pd.Series, bt: dict) -> dict:
    last = df.iloc[-1]
    t = df.index[-1]

    def flag(p, e):
        return "ease" if e else ("panic" if p else "normal")

    def spark(col, n=120):
        s = df[col].tail(n)
        return [None if pd.isna(v) else _r(v, 3) for v in s]

    vol_label = "台指VIX" if vol_source == "vix" else "加權指數10日實現波動率"
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
        {"key": "margin", "label": "融資餘額", "sub": "上市（億元）", "value": _r(last["margin"], 0), "unit": "億",
         "chg10": _r(last["margin_chg10"] * 100, 2) if pd.notna(last["margin_chg10"]) else None,
         "flag": "panic" if last["margin_panic"] else ("washed" if (last["margin_washed"] and pd.notna(last["margin_chg10"])) else "normal"),
         "stable": bool(last["margin_stable"]) if pd.notna(last["margin"]) else None,
         "rule": f"10日減少{abs(int(MARGIN_PANIC_CHG * 100))}%以上＝去槓桿（恐慌）；近{MARGIN_WASH_LOOKBACK}日內發生過＝籌碼已洗",
         "spark": spark("margin")},
    ]
    hist = [[d.strftime("%Y-%m-%d"), s] for d, s in df["state"].tail(60).items()]
    flips = df["state"][df["state"] != df["state"].shift(1)].tail(8)
    payload = {
        "schema": 1,
        "generated_at": datetime.now(TPE).isoformat(timespec="minutes"),
        "date": t.strftime("%Y-%m-%d"),
        "close": _r(last["close"], 2),
        "state": last["state"], "state_text": STATE_TEXT[last["state"]], "action": ACTION_TEXT[last["state"]],
        "n_panic": int(last["n_panic"]),
        "since": _since(df),
        "vol_source": vol_source,
        "vix": {"value": _r(vix.iloc[-1], 2), "date": vix.index[-1].strftime("%Y-%m-%d"), "days": int(len(vix))} if len(vix) else None,
        "components": comps,
        "spark_dates": [d.strftime("%Y-%m-%d") for d in df.index[-120:]],
        "history": hist,
        "flips": [[d.strftime("%Y-%m-%d"), s, STATE_TEXT[s]] for d, s in flips.items()],
        "backtest": bt,
        "notes": [],
    }
    if vol_source != "vix":
        payload["notes"].append(f"台指VIX 已累積 {len(vix)} 天，滿 {VIX_MIN_DAYS} 天後自動改用VIX判斷；目前用加權指數實現波動率代替。")
    payload["sig"] = f"{payload['date']}|{payload['close']}|{comps[1]['value']}|{comps[2]['value']}|{payload['vix']}"
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
    return close, -short, margin


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
        return 0
    out = Path(args.out)

    try:
        if args.offline_test:
            close, net_oi, margin = synthetic()
            vix = pd.Series(dtype=float)
        else:
            close = fetch_taiex_twse(DATA_DIR / "taiex_twse.csv", verbose)
            if len(close) < 150:
                raise RuntimeError("加權指數歷史不足（請先執行 market_corr.py 建立 data/taiex_twse.csv）")
            dates = list(close.index)
            vix = fetch_vix(verbose)
            net_oi = fetch_foreign_short(dates, verbose)
            margin = fetch_margin(dates, verbose)
            if len(net_oi) < 60:
                raise RuntimeError("外資期貨未平倉資料不足")

        use_vix = len(vix) >= VIX_MIN_DAYS
        vol = vix if use_vix else realized_vol(close)
        df = compute(close, vol, net_oi, margin)
        # 最後一天三項資料都要有（避免期交所/證交所還沒公布時誤判）
        df = df[df["short"].notna() & df["vol"].notna()]
        if not len(df):
            raise RuntimeError("沒有可計算的日子")
        bt = backtest(compute(close, realized_vol(close), net_oi, margin))   # 回測一律用實現波動率（歷史較長）
        payload = build_payload(df, "vix" if use_vix else "rv", vix, bt)
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
        for c in payload["components"]:
            extra = f"  百分位 {c['pct']}" if c.get("pct") is not None else ""
            extra += f"  10日變化 {c['chg10']:+.2f}%" if c.get("chg10") is not None else ""
            print(f"     {c['label']:<8} {c['value']}{c['unit']}{extra}  [{c['flag']}]")
        b = payload["backtest"]
        print(f"     回測 {b['start']} ~ {b['end']}（{b['n_days']} 天，用實現波動率）")
        for r in b["buckets"]:
            print(f"       {r['label']:<6} n={r['n']:<4} 5日 {r['avg5']}% (勝率 {r['win5']}%)  "
                  f"10日 {r['avg10']}%  20日 {r['avg20']}% (勝率 {r['win20']}%)")
        for e in b["events"]:
            print(f"       退燒 {e['date']}：距低點 {e['days_from_low']:+d} 天、高於低點 {e['pct_above_low']}%、"
                  f"之後再跌 {e['further_drop']}%、20日 {e['r20']}%")
        for n in payload["notes"]:
            print(f"     ※ {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
