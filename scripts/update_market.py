"""Build the public Pages snapshot, including valuation and height history."""
from __future__ import annotations

import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "market.json"
LOCAL_HEIGHT_CACHE = ROOT.parent / ".market_height_history.json"
BASE = "https://lianban.net/"
ARCHIVE = f"{BASE}days/"
MIRRORS = (ARCHIVE, "https://lianbanwang.com/days/")
CSI = "https://www.csindex.com.cn/"
CSI_PERF = f"{CSI}csindex-home/perf/index-perf"
MCP_ENDPOINT = f"{BASE}mcp"
UA = "Mozilla/5.0 (compatible; ashare-review-pages/2.0; +https://github.com/)"


def fetch(url: str, referer: str = BASE, attempts: int = 4) -> str:
    req = Request(url, headers={"User-Agent": UA, "Referer": referer, "Accept": "text/html,application/json"})
    for attempt in range(attempts):
        try:
            with urlopen(req, timeout=30) as res:
                return res.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code != 429 or attempt == attempts - 1:
                raise
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url}")


def mcp_call(tool: str, **arguments) -> dict:
    body = json.dumps({
        "jsonrpc": "2.0", "id": int(time.time() * 1000) % 1_000_000,
        "method": "tools/call", "params": {"name": tool, "arguments": arguments},
    }).encode("utf-8")
    request = Request(MCP_ENDPOINT, data=body, headers={
        "User-Agent": UA, "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    })
    with urlopen(request, timeout=25) as response:
        envelope = json.loads(response.read().decode("utf-8"))
    result = envelope.get("result", {})
    content = result.get("content", [])
    if result.get("isError") or not content or not content[0].get("text"):
        raise RuntimeError(f"MCP {tool} returned no data")
    return json.loads(content[0]["text"])


def last_completed_trade_day() -> str:
    now = datetime.now(timezone.utc).astimezone(ZoneInfo("Asia/Shanghai"))
    include_today = now.weekday() >= 5 or now.hour >= 17
    today = now.date().isoformat()
    dates = mcp_call("list_trade_dates", limit=30).get("dates", [])
    eligible = [str(day) for day in dates if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(day)) and (str(day) <= today if include_today else str(day) < today)]
    if not eligible:
        raise RuntimeError("No completed trading day available")
    return max(eligible)


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", "", text))).strip()


def get_between(html: str, left: str, right: str) -> str:
    match = re.search(left + r"([\s\S]*?)" + right, html, re.I)
    return clean(match.group(1)) if match else "—"


def parse_ladder(html: str) -> list[dict]:
    ladder = []
    hits = list(re.finditer(r'<div class="b"[^>]*>(\d+)板</div>', html))
    for index, board in enumerate(hits):
        end = hits[index + 1].start() if index + 1 < len(hits) else min(len(html), board.end() + 120000)
        row = html[board.end():end]
        count = re.search(r'<div class="c">(\d+)家</div>', row)
        prior = re.search(r'明日晋级率[\s\S]{0,250}?≈([\d.]+)%', row)
        if count:
            ladder.append({
                "board": int(board.group(1)), "count": int(count.group(1)),
                "prior": prior.group(1) if prior else "—",
                "names": [clean(x) for x in re.findall(r'<a class="nm"[^>]*>([^<]+)</a>', row)],
                "subjects": [clean(x) for x in re.findall(r'<div class="cc">([^<]+)</div>', row)],
            })
    if ladder:
        return ladder
    grouped: dict[int, list[str]] = {}
    for raw in re.findall(r'<script type="application/ld\+json">([\s\S]*?)</script>', html):
        try:
            obj = json.loads(unescape(raw))
        except (json.JSONDecodeError, TypeError):
            continue
        if obj.get("@type") != "ItemList" or "连板天梯" not in str(obj.get("name", "")):
            continue
        for item in obj.get("itemListElement", []):
            hit = re.match(r"(.+?)\s+(\d+)板$", str(item.get("name", "")).strip())
            if hit:
                grouped.setdefault(int(hit.group(2)), []).append(hit.group(1))
    return [{"board": b, "count": len(names), "prior": "—", "names": names, "subjects": []} for b, names in sorted(grouped.items(), reverse=True)]


def parse_day(html: str, day: str) -> dict:
    meta = re.search(r'<meta name="description" content="([^"]+)', html)
    desc = unescape(meta.group(1)) if meta else clean(html[:7000])
    hit = re.search(r"涨停(\d+)家、连板(\d+)家、最高(\d+)板、跌停(\d+)家、封板率([\d.]+)%、炸板(\d+)家。情绪([^。]+)期。主线题材：([^。]+)", desc)
    if not hit:
        raise RuntimeError(f"Could not parse market summary for {day}")
    up, consecutive, highest, down, seal, broken, cycle, themes = hit.groups()
    temp = re.search(r"温度\s*(\d+)°", html)
    mainlines = []
    for block in re.findall(r'<div class="zxc">([\s\S]*?)</div></div>', html):
        name, count = re.search(r'<b>([^<]+)</b>', block), re.search(r'涨停(\d+)家', block)
        leader = re.search(r'龙头\s*([^<]+)', block)
        if name and count:
            mainlines.append({"name": clean(name.group(1)), "count": int(count.group(1)), "leader": clean(leader.group(1)) if leader else "—"})
    return {
        "date": day,
        "stats": {
            "up": int(up), "consecutive": int(consecutive), "highest": int(highest),
            "down": int(down), "seal": float(seal), "broken": int(broken),
            "cycle": cycle, "themes": themes.split("、"),
            "temperature": temp.group(1) if temp else "—",
            "turnover": get_between(html, r"两市成交</div><div class=\"v\"[^>]*>", r"</div>"),
            "upDown": get_between(html, r"上涨/下跌[\s\S]{0,150}?<div class=\"v[^>]*>", r"</div>"),
        },
        "mainlines": mainlines[:8], "ladder": parse_ladder(html),
    }


def market_day_from_mcp(day: str | None = None) -> tuple[dict, str]:
    review = mcp_call("get_daily_review", **({"date": day} if day else {}))
    kpi = review.get("kpi", {})
    day = str(kpi.get("date", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise RuntimeError("MCP did not return a valid latest trade date")
    themes = review.get("themes", [])
    theme_names = [str(item.get("name", "")) for item in themes if item.get("name")]
    current = {
        "date": day,
        "stats": {
            "up": int(kpi.get("limit_up", 0) or 0), "consecutive": int(kpi.get("lianban", 0) or 0),
            "highest": int(kpi.get("max_board", 0) or 0), "down": int(kpi.get("limit_down", 0) or 0),
            "seal": float(kpi.get("seal_rate_pct", 0) or 0), "broken": int(kpi.get("zhaban", 0) or 0),
            "cycle": str(kpi.get("emotion_phase", "—")), "themes": theme_names or ["—"],
            "temperature": "—", "turnover": "—", "upDown": f"{kpi.get('adv', '—')} / {kpi.get('dec', '—')}",
        },
        "mainlines": [{"name": str(item.get("name", "—")), "count": int(item.get("limit_up", 0) or 0), "leader": "见公开复盘页"} for item in themes][:8],
        "ladder": [],
    }
    return current, str(review.get("page", MCP_ENDPOINT))


def height_item(day: dict) -> dict:
    highest = day["stats"]["highest"]
    row = next((x for x in day["ladder"] if x["board"] == highest), None)
    return {"date": day["date"], "height": highest, "names": row["names"] if row else []}


def cached_height_rows() -> dict[str, dict]:
    rows = []
    for path in (OUT, LOCAL_HEIGHT_CACHE):
        try:
            obj = json.loads(path.read_text(encoding="utf-8"))
            rows.extend(obj if isinstance(obj, list) else obj.get("heightTrend", []))
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError):
            pass
    return {x["date"]: {"date": x["date"], "height": x["height"], "names": x.get("names", [])} for x in rows if x.get("date") and x.get("height")}


def build_height(current: dict, sessions: int = 22) -> list[dict]:
    cache = cached_height_rows()
    try:
        archive = fetch(ARCHIVE)
    except Exception:
        try:
            archive = fetch(MIRRORS[1], referer=MIRRORS[1])
        except Exception:
            archive = ""
    dates = sorted(set(re.findall(r'/days/(\d{4}-\d{2}-\d{2})\.html', archive)))
    if not dates:
        dates = sorted(set(cache) | {current["date"]})
    dates = [x for x in dates if x <= current["date"]][-sessions:]
    cache[current["date"]] = height_item(current)
    for index, day in enumerate(dates):
        if day in cache and cache[day].get("names"):
            continue
        mirror = MIRRORS[index % len(MIRRORS)]
        try:
            cache[day] = height_item(parse_day(fetch(f"{mirror}{day}.html", referer=mirror), day))
        except Exception:
            continue
        time.sleep(.38)
    rows = [cache[x].copy() for x in dates if x in cache]
    for index, row in enumerate(rows):
        previous = [x["height"] for x in rows[max(0, index - 5):index]]
        row["pressure"] = pressure = max(previous) if previous else None
        row["state"] = "起点" if pressure is None else "突破" if row["height"] > pressure else "触压" if row["height"] == pressure else "承压"
    return rows


def build_valuation() -> dict:
    query = urlencode({"indexCode": "000300", "startDate": "20180102", "endDate": datetime.now().strftime("%Y%m%d")})
    payload = json.loads(fetch(f"{CSI_PERF}?{query}", referer=CSI))
    by_date: dict[str, float] = {}
    for item in payload.get("data", []):
        try:
            day, pe = str(item["tradeDate"]), float(item["peg"])
            parsed = datetime.strptime(day, "%Y%m%d")
        except (KeyError, TypeError, ValueError):
            continue
        if parsed.weekday() < 5 and 0 < pe < 100:
            by_date[day] = pe
    daily = sorted(by_date.items())
    if len(daily) < 20:
        raise RuntimeError("Insufficient CSI 300 PE samples")
    values = [x[1] for x in daily]
    mean, std = statistics.fmean(values), statistics.pstdev(values)
    low, high = mean - std, mean + std
    current_day, current = daily[-1]
    monthly = {}
    for day, pe in daily:
        monthly[day[:6]] = (day, pe)
    zone = "低估区" if current < low else "高估区" if current > high else "合理区"
    return {
        "indexCode": "000300", "name": "沪深300", "basis": "滚动市盈率", "source": CSI,
        "startDate": daily[0][0], "endDate": current_day, "sampleCount": len(values),
        "current": round(current, 2), "mean": round(mean, 2), "std": round(std, 2),
        "low": round(low, 2), "high": round(high, 2),
        "percentile": round(sum(x <= current for x in values) / len(values) * 100, 1),
        "zone": zone, "formula": "全样本日度均值 ± 1 个总体标准差",
        "series": [{"date": d[:4] + "-" + d[4:6] + "-" + d[6:], "pe": round(pe, 2)} for d, pe in monthly.values()],
    }


def build_strategy(existing: dict | None) -> tuple[dict, dict]:
    """A reproducible market-regime backtest; result is an event rate, not P&L."""
    stored = dict((existing or {}).get("strategyMcpDays", {}))
    calendar = mcp_call("list_trade_dates", limit=30).get("dates", [])
    last_day = last_completed_trade_day()
    dates = sorted({str(x) for x in calendar if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(x)) and str(x) <= last_day})
    for day in dates:
        if day in stored:
            continue
        try:
            review = mcp_call("get_daily_review", date=day)
        except Exception:
            continue
        kpi = review.get("kpi", {})
        stored[day] = {
            "date": day, "cycle": str(kpi.get("emotion_phase", "")),
            "seal": float(kpi.get("seal_rate_pct", 0) or 0), "highest": int(kpi.get("max_board", 0) or 0),
        }
        time.sleep(.12)
    rows = [stored[day] for day in dates if day in stored]
    signals, checked, winners = [], 0, 0
    for prior, next_day in zip(rows, rows[1:]):
        checked += 1
        active = 4 <= prior["highest"] <= 7 and prior["seal"] >= 70
        if not active:
            continue
        advanced = next_day["highest"] >= prior["highest"]
        winners += int(advanced)
        signals.append({
            "signalDate": prior["date"], "resultDate": next_day["date"], "cycle": prior["cycle"],
            "seal": prior["seal"], "highest": prior["highest"],
            "candidates": [f"T日最高 {prior['highest']} 板"],
            "winners": [f"T+1最高 {next_day['highest']} 板 · {'延续' if advanced else '断板'}"],
            "candidateCount": 1, "winnerCount": int(advanced),
        })
    strategy = {
        "name": "高封板率中高位延续策略", "version": "v1.1 · MCP可复核",
        "metric": "高度延续胜率（T+1最高板 ≥ T日最高板）", "notReturn": True,
        "dateRange": [rows[0]["date"], rows[-1]["date"]] if rows else [],
        "requestedSessions": len(rows), "checkedPairs": checked, "signalDays": len(signals),
        "candidates": len(signals), "winners": winners,
        "winRate": round(winners / len(signals) * 100, 1) if signals else None,
        "candidateLabel": "研究信号", "winnerLabel": "高度延续", "tableCandidateLabel": "T 日连板结构", "tableWinnerLabel": "T+1 结果",
        "recentSignals": list(reversed(signals[-10:])),
        "conditions": [
            "T 日最高连板处于 4–7 板（避开无高度与 8 板以上情绪末端）",
            "T 日封板率不低于 70%", "T+1 最高板不低于 T 日最高板记为高度延续",
            "信号仅代表“允许研究接力环境”，不等于直接买入任一连板股",
        ],
        "limitations": [
            "该策略衡量市场连板环境的延续，不是单只股票、也不是可实现交易收益率；样本期较短。",
            "不包含竞价买卖、一字板、滑点、手续费、仓位、止损或盘中流动性。",
            "历史统计不构成对未来个股的预测或投资建议。",
        ],
        "source": MCP_ENDPOINT, "generatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
    }
    return strategy, {day: stored[day] for day in sorted(stored)[-40:]}


def build() -> dict:
    try:
        existing = json.loads(OUT.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        existing = None
    day = last_completed_trade_day()
    source = f"{ARCHIVE}{day}.html"
    try:
        current_html = fetch(source)
    except Exception:
        try:
            current_html = fetch(f"{MIRRORS[1]}{day}.html", referer=MIRRORS[1])
        except Exception:
            current_html = ""
    if current_html:
        current = parse_day(current_html, day)
    elif existing and existing.get("date") == day:
        current = {"date": day, "stats": existing["stats"], "mainlines": existing.get("mainlines", []), "ladder": existing.get("ladder", [])}
    else:
        current, source = market_day_from_mcp(day)
        day = current["date"]
    errors = []
    with ThreadPoolExecutor(max_workers=3) as pool:
        valuation_job, height_job, strategy_job = pool.submit(build_valuation), pool.submit(build_height, current), pool.submit(build_strategy, existing)
        try:
            valuation = valuation_job.result()
        except Exception as exc:
            valuation, errors = {"series": [], "error": str(exc)}, [f"沪深300估值：{exc}"]
        try:
            height = height_job.result()
        except Exception as exc:
            height = [height_item(current)]
            errors.append(f"连板高度历史：{exc}")
        try:
            strategy, strategy_days = strategy_job.result()
        except Exception as exc:
            strategy, strategy_days = (existing or {}).get("strategyBacktest"), (existing or {}).get("strategyMcpDays", {})
            errors.append(f"连板策略回测：{exc}")
    return {
        "date": day, "source": source,
        "updatedAt": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "stats": current["stats"], "mainlines": current["mainlines"], "ladder": current["ladder"],
        "valuation": valuation, "heightTrend": height, "strategyBacktest": strategy, "strategyMcpDays": strategy_days, "errors": errors,
    }


if __name__ == "__main__":
    snapshot = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Updated {OUT}: {snapshot['date']}; PE={snapshot['valuation'].get('current')}; height points={len(snapshot['heightTrend'])}")
