# -*- coding: utf-8 -*-
"""
etf_flow.py  --  ETF 資金流向（上市＋上櫃全部 ETF）

產生 data/etf_flow.json，給 etf_flow.html 用。三個榜：
  1. 受益權單位數當日增加（申購流入）  —— 證交所「ETF 申贖資訊及即時淨值揭露專區」
     mis.twse.com.tw/stock/data/all_etf.txt，欄位 d＝與前日已發行單位數差異
  2. 外資買超  —— 上市：證交所 T86（selectType=0099P＝ETF）；上櫃：櫃買中心三大法人買賣明細
  3. 投信買超  —— 同上
金額＝股數 × 當日成交價（億元，估算值）。排序方式（金額／張數）在網頁上切換，
所以這裡把每一檔都寫進 JSON，不在 Python 端先取前 5。

交易日以 all_etf.txt 的資料日期為準，法人資料抓同一天，三個榜一定是同一天的資料。

用法：
    python etf_flow.py                  # 寫 data/etf_flow.json
    python etf_flow.py --force          # 沒有新資料也重寫
    python etf_flow.py --fixtures DIR   # 從 DIR 讀 all_etf.json / t86.json / tpex.json（離線測試）
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

TPE = timezone(timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json,text/plain,*/*"}

URL_UNITS = "https://mis.twse.com.tw/stock/data/all_etf.txt"
URL_T86 = "https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=0099P&response=json"
URL_TPEX = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade?type=Daily&sect=EW&date={ad}&response=json"

# 欄位位置（2026-11 確認）
#   T86：4＝外陸資買賣超股數(不含外資自營商)，10＝投信買賣超股數
#   櫃買：4＝外資及陸資(不含外資自營商)買賣超股數，13＝投信買賣超股數
T86_FI, T86_IT = 4, 10
TPEX_FI, TPEX_IT = 4, 13


def _ssl_context() -> ssl.SSLContext:
    """證交所憑證缺 Subject Key Identifier，Python 3.13 起的嚴格模式會拒絕；只關掉這一項。"""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
    ctx.verify_flags &= ~getattr(ssl, "VERIFY_X509_STRICT", 0)
    return ctx


_SSL = _ssl_context()


def _get_json(url: str, tries: int = 3):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30, context=_SSL) as r:
                return json.loads(r.read().decode("utf-8-sig"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 + i * 3)
    raise RuntimeError(f"{url} → {last}")


def _num(v) -> float | None:
    """all_etf.txt 各家投信格式不一：數字、字串、千分位、小數點都有。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _clean_name(s: str) -> str:
    s = re.sub(r"[（(]原名稱.*?[）)]", "", str(s or "")).strip()
    for suf in ("證券投資信託基金", "指數股票型期貨信託基金", "期貨信託基金", "ETF基金", "ETF", "基金"):
        if s.endswith(suf):
            s = s[: -len(suf)]
    return s.strip()


def category(code: str) -> str:
    c = code.upper()
    if c.endswith(("L", "R")):
        return "槓反"
    if c.endswith(("B", "C", "D")):
        return "債券"
    if c.endswith("U"):
        return "期貨"
    if c.endswith("A"):
        return "主動"
    return "股票"


# --------------------------------------------------------------------------
# 抓資料
# --------------------------------------------------------------------------

def load_units(j) -> tuple[str, dict]:
    rows = {}
    for blk in j.get("a1", []) or []:
        for r in blk.get("msgArray", []) or []:
            code = str(r.get("a", "")).strip()
            if not code:
                continue
            rows[code] = {
                "name_long": _clean_name(r.get("b", "")),
                "units": _num(r.get("c")),
                "d_units": _num(r.get("d")),
                "price": _num(r.get("e")),
                "nav_prev": _num(r.get("h")),
                "date": str(r.get("i", "")).strip(),
            }
    dates = Counter(v["date"] for v in rows.values() if re.fullmatch(r"\d{8}", v["date"]))
    if not dates:
        raise RuntimeError("all_etf.txt 沒有資料日期")
    day = dates.most_common(1)[0][0]
    # 只留當天的（少數投信若還停在前一天，單位數差異不是同一天，不納入排名）
    return day, {k: v for k, v in rows.items() if v["date"] == day}


def load_t86(j) -> dict | None:
    if not j or str(j.get("stat", "")).upper() != "OK" or not j.get("data"):
        return None
    out = {}
    for r in j["data"]:
        out[r[0].strip()] = {"name": r[1].strip(), "market": "上市",
                             "fi": _num(r[T86_FI]), "it": _num(r[T86_IT])}
    return out


def load_tpex(j) -> dict | None:
    if not j or str(j.get("stat", "")).lower() != "ok":
        return None
    tbl = (j.get("tables") or [{}])[0]
    data = tbl.get("data") or []
    if not data:
        return None
    out = {}
    for r in data:
        code = str(r[0]).strip()
        if not code.startswith("00"):          # 只要 ETF（上櫃個股代號不會是 00 開頭）
            continue
        out[code] = {"name": str(r[1]).strip(), "market": "上櫃",
                     "fi": _num(r[TPEX_FI]), "it": _num(r[TPEX_IT])}
    return out


def fetch_all(fixtures: Path | None):
    if fixtures:
        rd = lambda n: json.loads((fixtures / n).read_text(encoding="utf-8"))  # noqa: E731
        units_j, t86_j, tpex_j = rd("all_etf.json"), rd("t86.json"), rd("tpex.json")
        day, units = load_units(units_j)
        return day, units, load_t86(t86_j), load_tpex(tpex_j)

    day, units = load_units(_get_json(URL_UNITS))
    t86 = tpex = None
    try:
        t86 = load_t86(_get_json(URL_T86.format(ymd=day)))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] T86 失敗：{e}", file=sys.stderr)
    time.sleep(1.5)
    try:
        tpex = load_tpex(_get_json(URL_TPEX.format(ad=f"{day[:4]}/{day[4:6]}/{day[6:]}")))
    except Exception as e:  # noqa: BLE001
        print(f"[warn] 櫃買法人失敗：{e}", file=sys.stderr)
    return day, units, t86, tpex


# --------------------------------------------------------------------------
# 組資料
# --------------------------------------------------------------------------

def _lots(shares):
    return None if shares is None else round(shares / 1000)


def _yi(shares, price):
    return None if shares is None or not price else round(shares * price / 1e8, 2)


def build(day, units, t86, tpex) -> dict:
    inst = {}
    for src in (tpex or {}, t86 or {}):        # 上市覆蓋上櫃（理論上不會重複）
        inst.update(src)

    rows = []
    for code in sorted(set(units) | set(inst)):
        u, i = units.get(code, {}), inst.get(code, {})
        price = u.get("price") or u.get("nav_prev")
        d = u.get("d_units")
        prev_units = (u["units"] - d) if u.get("units") is not None and d is not None else None
        # 單位數一天變成 1.5 倍以上 → 多半是分割／反分割，不是資金流入，不列入排名
        split = bool(prev_units and d and abs(d) >= 0.5 * prev_units)
        r = {
            "code": code,
            "name": i.get("name") or u.get("name_long") or code,
            "market": i.get("market", ""),
            "cat": category(code),
            "price": price,
            "units": _lots(u.get("units")),
            "d_units": _lots(d),
            "d_pct": round(d / prev_units * 100, 2) if prev_units and d is not None else None,
            "flow": _yi(d, price),
            "fi": _lots(i.get("fi")), "fi_amt": _yi(i.get("fi"), price),
            "it": _lots(i.get("it")), "it_amt": _yi(i.get("it"), price),
        }
        if split:
            r["split"] = True
        if any(r[k] for k in ("d_units", "fi", "it")):
            rows.append(r)

    return {
        "date": f"{day[:4]}-{day[4:6]}-{day[6:]}",
        "generated_at": datetime.now(TPE).strftime("%Y-%m-%dT%H:%M"),
        "n_units": len(units),
        "inst": {"twse": t86 is not None, "tpex": tpex is not None},
        "rows": rows,
    }


def should_write(new: dict, path: Path, force: bool) -> tuple[bool, str]:
    if force or not path.exists():
        return True, "new"
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return True, "old file unreadable"
    if new["date"] < old.get("date", ""):
        return False, f"資料日 {new['date']} 比現有 {old.get('date')} 舊"
    if new["date"] == old.get("date"):
        o, n = old.get("inst", {}), new["inst"]
        if sum(o.values()) > sum(n.values()):
            return False, "同一天但這次法人資料抓不到，保留舊檔"
        if old.get("rows") == new["rows"]:
            return False, "沒有變化"
    if not any(r.get("d_units") for r in new["rows"]) and not any(new["inst"].values()):
        return False, "單位數全為 0 且沒有法人資料（可能是盤前重置），不覆蓋"
    return True, "updated"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/etf_flow.json")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--fixtures", type=Path)
    a = ap.parse_args()

    day, units, t86, tpex = fetch_all(a.fixtures)
    out = build(day, units, t86, tpex)
    path = Path(a.out)
    ok, why = should_write(out, path, a.force)
    tag = f"{out['date']}｜單位數 {out['n_units']} 檔｜上市法人 {'OK' if t86 else '缺'}｜上櫃法人 {'OK' if tpex else '缺'}"
    if not ok:
        print(f"[skip] {why}（{tag}）")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"[ok] 寫入 {path}（{tag}，{len(out['rows'])} 檔有變動）")


if __name__ == "__main__":
    main()
