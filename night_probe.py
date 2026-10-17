# -*- coding: utf-8 -*-
"""
night_probe.py  --  測試台指期夜盤（盤後交易時段）的兩種資料能不能在早上抓到
  1. 夜盤收盤價（凌晨 5:00 收盤）
  2. 夜盤外資交易口數（期交所約早上 6 點公布；夜盤不公布未平倉）

用法：python night_probe.py
把整個畫面截圖回傳即可，這支程式不會寫任何檔案。
"""
import io
import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from market_corr import TPE, UA, _SSL

today = datetime.now(TPE).date()
start = today - timedelta(days=6)
end = today + timedelta(days=3)          # 週五夜盤會算到下一個交易日


def post(url, data, as_json=False, timeout=40):
    if as_json:
        body = json.dumps(data).encode()
        headers = dict(UA, **{"Content-Type": "application/json"})
    else:
        body = urllib.parse.urlencode(data).encode()
        headers = UA
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as r:
        return r.read()


def show(title, fn):
    print("\n" + "=" * 70 + f"\n{title}")
    try:
        raw = fn()
        for enc in ("utf-8-sig", "cp950"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        lines = text.splitlines()
        if "<html" in text[:2000].lower() or "<table" in text.lower():
            import pandas as pd
            try:
                tabs = pd.read_html(io.StringIO(text))
            except Exception as exc:  # noqa: BLE001
                tabs = []
                print(f"   HTML 沒有表格：{exc}")
            m = re.search(r"日期\s*[:：]?\s*(\d{4}/\d{1,2}/\d{1,2})", text)
            print(f"   HTML，{len(tabs)} 個表格；頁面日期 {m.group(1) if m else '找不到'}")
            for t in tabs:
                s_ = t.to_string()
                if "外資" in s_ or "臺股期貨" in s_ or "盤後" in s_:
                    print("   " + s_[:1500].replace("\n", "\n   "))
                    break
            return
        print(f"   長度 {len(raw)} bytes，{len(lines)} 行；前 12 行：")
        for ln in lines[:12]:
            print("   " + ln[:220])
        hits = [ln for ln in lines if ("盤後" in ln or "外資" in ln)]
        if hits:
            print(f"   含『盤後/外資』的行（最多 8 行）：")
            for ln in hits[:8]:
                print("   >> " + ln[:220])
    except Exception as exc:  # noqa: BLE001
        print(f"   失敗：{exc}")


# 1A 期貨每日交易行情下載（CSV，含一般與盤後時段）
show("[1A] 夜盤價格：futDataDown（每日交易行情 CSV）",
     lambda: post("https://www.taifex.com.tw/cht/3/futDataDown", {
         "down_type": "1", "commodity_id": "TX",
         "queryStartDate": start.strftime("%Y/%m/%d"), "queryEndDate": today.strftime("%Y/%m/%d")}))

# 1B 期交所行情資訊網（即時報價，夜盤收盤後到早上開盤前應停在夜盤最後價）
show("[1B] 夜盤價格：mis.taifex.com.tw getQuoteList（MarketType=1 盤後）",
     lambda: post("https://mis.taifex.com.tw/futures/api/getQuoteList", {
         "MarketType": "1", "SymbolType": "F", "KindID": "1", "CID": "TXF",
         "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}, as_json=True))

# 2A 三大法人-區分各期貨契約（夜盤）下載
show("[2A] 夜盤外資口數：futContractsDateAhDown（CSV）",
     lambda: post("https://www.taifex.com.tw/cht/3/futContractsDateAhDown", {
         "queryStartDate": start.strftime("%Y/%m/%d"), "queryEndDate": end.strftime("%Y/%m/%d"),
         "commodityId": "TXF"}))

# 2B 三大法人-區分各期貨契約（夜盤）查詢頁
for q in [today + timedelta(days=k) for k in (3, 2, 1, 0, -1)]:
    show(f"[2B] 夜盤外資口數：futContractsDateAh 查詢頁 queryDate={q:%Y/%m/%d}",
         lambda q=q: post("https://www.taifex.com.tw/cht/3/futContractsDateAh", {
             "queryType": "1", "goDay": "", "doQuery": "1", "dateaddcnt": "",
             "queryDate": q.strftime("%Y/%m/%d"), "commodityId": "TXF"}))

print("\n完成。請把整個畫面截圖（可能要往上捲，分兩三張）。")
