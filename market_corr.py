# -*- coding: utf-8 -*-
"""
market_corr.py  --  Global-Markets-monitor 傳導鏈資料產生器

每日收盤後執行，抓取六個節點的日線資料，計算：
  1. 訊號燈狀態（油價 / 美10年殖利率 / 美元指數 / 日圓 / 費半 / 台股台幣同向）
  2. 20 日與 60 日滾動相關係數矩陣
  3. 領先落後相關（台股 t 對其他資產 t-1）
  4. 近 180 日滾動相關的歷史序列（給折線圖）

輸出：data/correlations.json

資料來源：yfinance 為主，Stooq CSV 為備援（兩者皆免費、免金鑰）。

用法：
    python market_corr.py                 # 寫到 ./data/correlations.json
    python market_corr.py --out X:\\path\\correlations.json
    python market_corr.py --offline-test  # 用合成資料跑一遍，驗證流程
"""

from __future__ import annotations

import argparse
import io
import json
import math
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

TPE = timezone(timedelta(hours=8))

# 傳導鏈順序即為顯示順序，不要隨意調換
ASSETS = [
    # key,      中文名,        簡稱,   yfinance,    stooq,      單位型別
    ("BRENT",   "布蘭特原油",   "油價", "BZ=F",      "cb.f",     "price"),
    ("US10Y",   "美10年殖利率", "10Y",  "^TNX",      "10usy.b",  "yield"),
    ("DXY",     "美元指數",     "美元", "DX-Y.NYB",  "dx.f",     "price"),
    ("USDJPY",  "美元/日圓",    "日圓", "JPY=X",     "usdjpy",   "price"),
    ("SOX",     "費城半導體",   "費半", "^SOX",      "^sox",     "price"),
    ("TAIEX",   "台股加權",     "台股", "^TWII",     "^twse",    "price"),
]

# 輔助資產：不進傳導鏈，只用來做「股匯同向」確認與美元參照
AUX = [
    ("USDTWD",  "美元/台幣",    "台幣", "TWD=X",     "usdtwd",   "price"),
    ("EURUSD",  "歐元/美元",    "歐元", "EURUSD=X",  "eurusd",   "price"),
]

WINDOWS = [20, 60]
HISTORY_DAYS = 180
LOOKBACK_PERIOD = "2y"

# 門檻可自行調整；規則說明會一併寫進 JSON，網頁上 hover 就看得到
THRESHOLDS = {
    "BRENT":  {"watch": 2.5,  "alert": 5.0},    # 5 日漲幅 %
    "US10Y":  {"watch": 5.0,  "alert": 8.0},    # 單日變動 bp
    "DXY":    {"watch": 99.5, "alert": 100.5},  # 絕對水位
    "USDJPY": {"watch": -1.5, "alert": -3.0},   # 5 日變動 %（負值＝日圓急升）
    "SOX":    {"watch": -0.8, "alert": -2.0},   # 單日漲跌 %
}

STATE_ORDER = {"calm": 0, "watch": 1, "alert": 2}


# --------------------------------------------------------------------------
# 抓資料
# --------------------------------------------------------------------------

def _fetch_yfinance(tickers: list[str], period: str) -> pd.DataFrame:
    import yfinance as yf
    raw = yf.download(
        tickers, period=period, interval="1d",
        auto_adjust=False, progress=False, group_by="column", threads=True,
    )
    if raw is None or len(raw) == 0:
        raise RuntimeError("yfinance 回傳空資料")
    if isinstance(raw.columns, pd.MultiIndex):
        close = raw["Close"]
    else:
        close = raw[["Close"]].rename(columns={"Close": tickers[0]})
    return close


def _fetch_stooq(symbol: str) -> pd.Series:
    url = "https://stooq.com/q/d/l/?s=" + urllib.parse.quote(symbol, safe="") + "&i=d"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    if "Close" not in body:
        # 把實際回應印出來，才知道是符號錯、被擋、還是超過每日次數
        snippet = " ".join(body.split())[:90] or "(空白回應)"
        raise RuntimeError(f"{symbol} → {snippet}")
    df = pd.read_csv(io.StringIO(body))
    df["Date"] = pd.to_datetime(df["Date"])
    return df.set_index("Date")["Close"].sort_index()


def load_prices(verbose: bool = True) -> pd.DataFrame:
    """回傳一張以日期為索引、欄位為資產 key 的收盤價表。"""
    spec = ASSETS + AUX
    frames: dict[str, pd.Series] = {}

    if verbose:
        print("[symbols] " + "  ".join(f"{k}={y}" for k, _n, _sh, y, _st, _kd in spec))

    # 先試 yfinance 批次抓
    try:
        tickers = [yf_sym for _k, _n, _sh, yf_sym, _st, _kind in spec]
        close = _fetch_yfinance(tickers, LOOKBACK_PERIOD)
        for key, _name, _short, yf_sym, _stooq_sym, _kind in spec:
            if yf_sym in close.columns:
                s = pd.to_numeric(close[yf_sym], errors="coerce").dropna()
                if len(s) > 100:
                    frames[key] = s
        if verbose:
            print(f"[yfinance] 取得 {len(frames)}/{len(spec)} 檔")
    except Exception as exc:  # noqa: BLE001
        if verbose:
            print(f"[yfinance] 失敗，改用 Stooq：{exc}")

    # 缺的用 Stooq 補
    for key, name, _short, _yf_sym, stooq_sym, _kind in spec:
        if key in frames:
            continue
        try:
            frames[key] = _fetch_stooq(stooq_sym)
            if verbose:
                print(f"[stooq] 補上 {key}（{name}）")
        except Exception as exc:  # noqa: BLE001
            if verbose:
                print(f"[warn] {key}（{name}）兩個來源都抓不到：{exc}")

    if not frames:
        raise RuntimeError("所有資料來源都失敗，沒有東西可以算")

    df = pd.DataFrame(frames).sort_index()
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df


# --------------------------------------------------------------------------
# 計算
# --------------------------------------------------------------------------

def to_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """殖利率取水位差（bp），其餘取日報酬（%）。"""
    kinds = {k: kind for k, _n, _sh, _y, _s, kind in ASSETS + AUX}
    out = {}
    for col in prices.columns:
        s = prices[col].dropna()
        if kinds.get(col) == "yield":
            out[col] = s.diff() * 100.0          # ^TNX 以 % 報價，×100 得 bp
        else:
            out[col] = s.pct_change() * 100.0
    return pd.DataFrame(out).dropna(how="all")


def _r(x, nd=3):
    """安全四捨五入，NaN 轉 None 好讓 JSON 乾淨。"""
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else round(f, nd)


def corr_matrix(rets: pd.DataFrame, keys: list[str], window: int) -> list[list[float | None]]:
    tail = rets[keys].tail(window)
    m = tail.corr(min_periods=max(10, window // 3))
    return [[_r(m.loc[a, b]) for b in keys] for a in keys]


def lead_lag(rets: pd.DataFrame, keys: list[str], window: int, target: str = "TAIEX"):
    """台股當日 vs 其他資產前一日。台股對上游沒有回饋能力，所以只算單向。"""
    out = []
    if target not in rets.columns:
        return out
    y = rets[target]
    for k in keys:
        if k == target or k not in rets.columns:
            continue
        pair = pd.concat([y, rets[k].shift(1)], axis=1).dropna().tail(window)
        same = pd.concat([y, rets[k]], axis=1).dropna().tail(window)
        out.append({
            "asset": k,
            "lagged": _r(pair.iloc[:, 0].corr(pair.iloc[:, 1])) if len(pair) > 10 else None,
            "same_day": _r(same.iloc[:, 0].corr(same.iloc[:, 1])) if len(same) > 10 else None,
        })
    return out


def chain_links(rets: pd.DataFrame, window: int):
    """相鄰節點的關聯強度，網頁用它決定連接線的粗細。"""
    keys = [a[0] for a in ASSETS]
    links = []
    for i in range(len(keys) - 1):
        a, b = keys[i], keys[i + 1]
        if a not in rets.columns or b not in rets.columns:
            links.append({"from": a, "to": b, "corr": None, "lag": 0})
            continue
        # 費半→台股是隔夜傳導，用 lag 1；其餘同步
        lag = 1 if (a == "SOX" and b == "TAIEX") else 0
        pair = pd.concat([rets[b], rets[a].shift(lag)], axis=1).dropna().tail(window)
        c = pair.iloc[:, 0].corr(pair.iloc[:, 1]) if len(pair) > 10 else None
        links.append({"from": a, "to": b, "corr": _r(c), "lag": lag})
    return links


def rolling_history(rets: pd.DataFrame, window: int, days: int):
    """幾條關鍵配對的滾動相關走勢，用來看『規律有沒有失效』。"""
    pairs = [
        ("TAIEX", "DXY",    0, "台股 vs 美元指數"),
        ("TAIEX", "SOX",    1, "台股 vs 費半（隔夜）"),
        ("TAIEX", "USDJPY", 0, "台股 vs 美元/日圓"),
        ("TAIEX", "US10Y",  0, "台股 vs 美10年殖利率"),
    ]
    series = []
    for a, b, lag, label in pairs:
        if a not in rets.columns or b not in rets.columns:
            continue
        x, y = rets[a], rets[b].shift(lag)
        c = x.rolling(window, min_periods=max(10, window // 2)).corr(y).dropna().tail(days)
        if c.empty:
            continue
        series.append({
            "id": f"{a}_{b}",
            "label": label,
            "latest": _r(c.iloc[-1]),
            "points": [{"d": d.strftime("%Y-%m-%d"), "v": _r(v)} for d, v in c.items()],
        })
    return series


# --------------------------------------------------------------------------
# 訊號燈
# --------------------------------------------------------------------------

def _grade(value, watch, alert, direction: str) -> str:
    """direction='up' 代表數字愈大愈危險；'down' 代表愈小愈危險。"""
    if value is None:
        return "unknown"
    if direction == "up":
        if value >= alert:
            return "alert"
        if value >= watch:
            return "watch"
    else:
        if value <= alert:
            return "alert"
        if value <= watch:
            return "watch"
    return "calm"


def _chg(prices: pd.DataFrame, key: str, n: int, as_bp: bool = False):
    if key not in prices.columns:
        return None, None
    s = prices[key].dropna()
    if len(s) <= n:
        return None, None
    last = float(s.iloc[-1])
    prev = float(s.iloc[-1 - n])
    if as_bp:
        return last, (last - prev) * 100.0
    if prev == 0:
        return last, None
    return last, (last - prev) / prev * 100.0


def build_signals(prices: pd.DataFrame) -> list[dict]:
    sig = []
    t = THRESHOLDS

    # 1 油價：鏈條的源頭
    last, c5 = _chg(prices, "BRENT", 5)
    sig.append({
        "key": "BRENT", "label": "布蘭特原油", "sub": "5 日變動",
        "value": _r(last, 2), "metric": _r(c5, 2), "unit": "%",
        "state": _grade(c5, t["BRENT"]["watch"], t["BRENT"]["alert"], "up"),
        "rule": "5 日漲逾 2.5% 留意、逾 5% 警戒。油價是通膨預期的源頭。",
    })

    # 2 美10年殖利率：目前比美元水位更關鍵
    last, cbp = _chg(prices, "US10Y", 1, as_bp=True)
    sig.append({
        "key": "US10Y", "label": "美10年殖利率", "sub": "單日變動",
        "value": _r(last, 2), "metric": _r(cbp, 1), "unit": "bp",
        "state": _grade(cbp, t["US10Y"]["watch"], t["US10Y"]["alert"], "up"),
        "rule": "單日彈逾 5bp 留意、逾 8bp 警戒。高殖利率殺的正是高本益比 AI 權值。",
    })

    # 3 美元指數：100 是分水嶺
    last, c1 = _chg(prices, "DXY", 1)
    sig.append({
        "key": "DXY", "label": "美元指數", "sub": "水位",
        "value": _r(last, 2), "metric": _r(c1, 2), "unit": "%",
        "state": _grade(last, t["DXY"]["watch"], t["DXY"]["alert"], "up"),
        "rule": "站上 99.5 留意、100.5 警戒。強美元壓抑外資匯入台股。",
    })

    # 4 日圓：看速度不看方向
    last, c5 = _chg(prices, "USDJPY", 5)
    state = _grade(c5, t["USDJPY"]["watch"], t["USDJPY"]["alert"], "down")
    if state == "calm" and c5 is not None and c5 >= 3.0:
        state = "watch"   # 反向：貶太快會招來干預
    sig.append({
        "key": "USDJPY", "label": "美元/日圓", "sub": "5 日變動",
        "value": _r(last, 2), "metric": _r(c5, 2), "unit": "%",
        "state": state,
        "rule": "日圓 5 日急升逾 1.5% 留意、逾 3% 警戒（套利平倉）；急貶逾 3% 則留意干預風險。緩貶為常態。",
    })

    # 5 費半：看它，不要看道瓊
    last, c1 = _chg(prices, "SOX", 1)
    sig.append({
        "key": "SOX", "label": "費城半導體", "sub": "單日漲跌",
        "value": _r(last, 2), "metric": _r(c1, 2), "unit": "%",
        "state": _grade(c1, t["SOX"]["watch"], t["SOX"]["alert"], "down"),
        "rule": "跌逾 0.8% 留意、逾 2% 警戒。台股跟的是費半，不是道瓊。",
    })

    # 6 台股：與台幣同向才算數
    tw_last, tw_c1 = _chg(prices, "TAIEX", 1)
    _twd_last, twd_c1 = _chg(prices, "USDTWD", 1)
    twd_appreciation = None if twd_c1 is None else -twd_c1   # 台幣升值為正
    if tw_c1 is None or twd_appreciation is None:
        state, note = "unknown", "缺台幣資料，無法確認"
    elif abs(tw_c1) < 0.15:
        state, note = "calm", "指數持平，不做判讀"
    elif (tw_c1 > 0) == (twd_appreciation > 0):
        state, note = "calm", "股匯同向，外資動能確認"
    else:
        state, note = "watch", "股匯背離，反彈可能沒有外資的錢"
    sig.append({
        "key": "TAIEX", "label": "台股加權", "sub": "與台幣同向",
        "value": _r(tw_last, 2), "metric": _r(tw_c1, 2), "unit": "%",
        "extra": {"twd_appreciation_pct": _r(twd_appreciation, 3)},
        "state": state, "note": note,
        "rule": "台股漲、台幣同步升＝有效訊號；台股漲但台幣不升＝錢不是外資的，反彈通常撐不久。",
    })
    return sig


def overall_state(signals: list[dict]) -> str:
    known = [s["state"] for s in signals if s["state"] in STATE_ORDER]
    if not known:
        return "unknown"
    alerts = sum(1 for s in known if s == "alert")
    watches = sum(1 for s in known if s == "watch")
    if alerts >= 2:
        return "alert"
    if alerts == 1 or watches >= 3:
        return "watch"
    return "calm"


# --------------------------------------------------------------------------
# 組裝
# --------------------------------------------------------------------------

def build_payload(prices: pd.DataFrame) -> dict:
    rets = to_returns(prices)
    keys = [a[0] for a in ASSETS if a[0] in rets.columns]
    names  = {k: n  for k, n, _sh, _y, _s, _kind in ASSETS + AUX}
    shorts = {k: sh for k, _n, sh, _y, _s, _kind in ASSETS + AUX}
    signals = build_signals(prices)

    payload = {
        "schema": 1,
        "generated_at": datetime.now(TPE).isoformat(timespec="seconds"),
        "as_of": prices.index[-1].strftime("%Y-%m-%d"),
        "overall": overall_state(signals),
        "assets": [{"key": k, "name": names[k], "short": shorts[k]} for k in keys],
        "signals": signals,
        "chain": chain_links(rets, 60),
        "correlations": {
            str(w): {"keys": keys, "matrix": corr_matrix(rets, keys, w)}
            for w in WINDOWS
        },
        "lead_lag": {"window": 60, "target": "TAIEX", "rows": lead_lag(rets, keys, 60)},
        "history": {"window": 60, "series": rolling_history(rets, 60, HISTORY_DAYS)},
        "missing": [a[0] for a in ASSETS + AUX if a[0] not in prices.columns],
    }
    return payload


def synthetic_prices(n: int = 400) -> pd.DataFrame:
    """離線測試用：造一組有已知結構的假資料。"""
    rng = np.random.default_rng(20260822)
    idx = pd.bdate_range(end=pd.Timestamp("2026-08-21"), periods=n)
    oil = rng.normal(0, 1.6, n)
    y10 = 0.35 * oil + rng.normal(0, 1.0, n)
    dxy = 0.30 * y10 + rng.normal(0, 0.35, n)
    jpy = 0.45 * y10 + rng.normal(0, 0.5, n)
    sox = -0.55 * y10 - 0.40 * dxy + rng.normal(0, 1.5, n)
    tw = 0.60 * np.roll(sox, 1) + rng.normal(0, 0.8, n)
    twd = -0.25 * np.roll(sox, 1) + rng.normal(0, 0.2, n)
    eur = -0.85 * dxy + rng.normal(0, 0.2, n)

    def walk(r, start):
        return start * np.exp(np.cumsum(r / 100.0))

    return pd.DataFrame({
        "BRENT": walk(oil, 68), "DXY": walk(dxy, 96), "USDJPY": walk(jpy, 150),
        "SOX": walk(sox, 5200), "TAIEX": walk(tw, 40000), "USDTWD": walk(twd, 32.5),
        "EURUSD": walk(eur, 1.10), "US10Y": 4.2 + np.cumsum(y10) / 100.0,
    }, index=idx)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/correlations.json")
    ap.add_argument("--offline-test", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    try:
        prices = synthetic_prices() if args.offline_test else load_prices(not args.quiet)
        payload = build_payload(prices)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] 產生失敗：{exc}", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)   # 原子寫入，避免網頁讀到寫到一半的檔

    if not args.quiet:
        print(f"[ok] {out}  資料日期 {payload['as_of']}  總體狀態 {payload['overall']}")
        for s in payload["signals"]:
            print(f"     {s['state']:<7} {s['label']}  {s['metric']}{s['unit']}")
        if payload["missing"]:
            print(f"[warn] 缺少資料：{', '.join(payload['missing'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
