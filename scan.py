"""Publish first-board candidates and opening-gap checks for the original 2j3 strategy."""

import argparse
import csv
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from market_data import ROOT, package

SHANGHAI = ZoneInfo("Asia/Shanghai")
REPORTS = Path("reports")
INDEX = "sh000852"


def request(url, encoding="utf-8"):
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=18) as response:
                return response.read().decode(encoding)
        except (OSError, UnicodeError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def index_days(day):
    start = date.fromisoformat(day) - timedelta(days=50)
    query = urlencode({"param": f"{INDEX},day,{start},{day},40,"})
    result = json.loads(request(f"https://web.ifzq.gtimg.cn/appstock/app/newfqkline/get?{query}"))
    bars = result["data"][INDEX]["day"]
    if len(bars) < 11:
        raise RuntimeError("中证1000日线不足11根")
    return {bar[0]: Decimal(bar[2]) for bar in bars}


def gate(closes):
    current = sum(closes[-10:]) / 10
    previous = sum(closes[-11:-1]) / 10
    deviation = closes[-1] / current - 1
    route = None
    if current < previous and deviation <= Decimal("-.02"):
        route = "ma10_down_below_2pct"
    elif current > previous and deviation < 0:
        route = "ma10_up_index_below"
    return route, current, previous, deviation


def sealed(bar):
    previous = Decimal(bar["prev_close"]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    ceiling = (previous * Decimal("1.10")).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    close = Decimal(bar["close"]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    return previous > 0 and close == ceiling and int(bar["volume"]) > 0


def prices(day):
    with (ROOT / f"{day}.csv").open(encoding="utf-8", newline="") as handle:
        return {row["code"]: row for row in csv.DictReader(handle)}


def first_boards(day, previous):
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(package, (day, previous)))
    prior = prices(previous)
    candidates = []
    for code, row in prices(day).items():
        if not code.startswith(("600", "601", "603", "605", "000", "001", "002", "003")):
            continue
        name = row["name"]
        close = Decimal(row["close"]).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        if "ST" in name.upper() or "退" in name or not Decimal(3) <= close < Decimal(5):
            continue
        if code in prior and sealed(row) and not sealed(prior[code]):
            candidates.append({"code": code, "name": name, "market": row["market"], "close": str(close)})
    return sorted(candidates, key=lambda row: row["code"])


def publish(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Published {path}")


def scan(day, skip_existing):
    path = REPORTS / f"{day}.json"
    if skip_existing and path.exists():
        print(f"Already published {path}")
        return
    closes = index_days(day)
    if day not in closes:
        print(f"{day} has no index daily bar")
        return
    route, ma, previous_ma, deviation = gate(list(closes.values()))
    candidates = first_boards(day, list(closes)[-2]) if route else []
    publish(path, {
        "date": day, "status": "scanned", "route": route,
        "index": {"code": "000852", "close": str(closes[day]), "ma10": str(ma),
                  "previous_ma10": str(previous_ma), "deviation_pct": str(deviation * 100)},
        "open_gap_pct": [2, 5], "candidates": candidates,
    })


def quotes(symbols):
    result = {}
    for start in range(0, len(symbols), 40):
        chunk = ",".join(symbols[start:start + 40])
        response = request(f"https://qt.gtimg.cn/q={chunk}", "gb18030")
        for symbol, payload in re.findall(r'v_([a-z0-9]+)="([^"]*)"', response):
            result[symbol] = payload.split("~")
    return result


def opening(day):
    out = REPORTS / "opening" / f"{day}.json"
    if out.exists():
        print(f"Already published {out}")
        return
    stamp = day.replace("-", "")
    index_quote = quotes([INDEX]).get(INDEX, [])
    if len(index_quote) < 31:
        raise RuntimeError("中证1000开盘行情未返回")
    if not index_quote[30].startswith(stamp):
        if day in index_days(day):
            raise RuntimeError("中证1000实时行情尚未更新到当天")
        print(f"No opening quote for {day}")
        return
    previous_days = index_days((date.fromisoformat(day) - timedelta(days=1)).isoformat())
    first = list(previous_days)[-1]
    source = REPORTS / f"{first}.json"
    if not source.exists():
        raise RuntimeError(f"缺少上一交易日盘后报告 {source}")
    report = json.loads(source.read_text(encoding="utf-8"))
    symbols = [row["market"] + row["code"] for row in report["candidates"]]
    data = quotes(symbols)
    passed, unavailable = [], []
    for symbol, row in zip(symbols, report["candidates"]):
        quote = data.get(symbol, [])
        if len(quote) < 31 or not quote[30].startswith(stamp) or not quote[4] or not quote[5]:
            unavailable.append(row["code"])
            continue
        previous, opened = Decimal(quote[4]), Decimal(quote[5])
        if opened <= 0 or previous <= 0:
            unavailable.append(row["code"])
            continue
        gap = (opened / previous - 1) * 100
        if 2 <= gap <= 5:
            passed.append((gap, {**row, "open": str(opened), "previous_close": str(previous),
                                 "gap_pct": f"{gap:.4f}"}))
    passed.sort(key=lambda item: (-item[0], item[1]["code"]))
    publish(out, {"date": day, "signal_date": first, "route": report["route"],
                  "status": "partial" if unavailable else "complete",
                  "candidates": [row for _, row in passed], "unavailable_codes": unavailable})
    if unavailable:
        print(f"开盘价暂不可用的代码: {', '.join(unavailable)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--opening", action="store_true")
    parser.add_argument("--skip-existing", action="store_true")
    args = parser.parse_args()
    today = datetime.now(SHANGHAI).date()
    day = (args.date or today).isoformat()
    if args.opening:
        if day != today.isoformat():
            parser.error("开盘筛选只接受北京时间当天")
        opening(day)
    else:
        scan(day, args.skip_existing)


if __name__ == "__main__":
    main()
