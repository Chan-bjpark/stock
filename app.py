import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from flask import Flask, render_template, jsonify, request

import requests
from bs4 import BeautifulSoup

app = Flask(__name__)

GROUPS = {
    "hlb": {
        "name": "HLB그룹",
        "stocks": [
            ("028300", "HLB"),
            ("002630", "HLB제약"),
            ("065510", "HLB테라퓨틱스"),
            ("003580", "HLB글로벌"),
            ("046210", "HLB파나진"),
            ("278620", "HLB바이오스텝"),
            ("024850", "HLB이노베이션"),
            ("067630", "HLB생명과학"),
            ("196300", "HLB펩"),
            ("187420", "HLB제넥스"),
        ],
    },
    "hanwha": {
        "name": "한화그룹",
        "stocks": [
            ("000880", "한화"),
            ("012450", "한화에어로스페이스"),
            ("042660", "한화오션"),
            ("009830", "한화솔루션"),
            ("272210", "한화시스템"),
            ("088350", "한화생명"),
            ("003530", "한화투자증권"),
            ("452260", "한화갤러리아"),
            ("000370", "한화손해보험"),
            ("489790", "한화비전"),
        ],
    },
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}


def fetch_stock(code, name):
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        price_tag = soup.select_one("p.no_today .blind")
        price = price_tag.get_text(strip=True) if price_tag else "-"

        blind_tags = soup.select("p.no_exday em span.blind")
        change = blind_tags[0].get_text(strip=True) if len(blind_tags) > 0 else "0"
        rate = blind_tags[1].get_text(strip=True) if len(blind_tags) > 1 else "0"

        no_exday_em = soup.select_one("p.no_exday em")
        direction = "flat"
        if no_exday_em:
            class_list = no_exday_em.get("class", [])
            if "no_up" in class_list:
                direction = "up"
            elif "no_down" in class_list:
                direction = "down"

        market_cap = "-"
        market_cap_raw = 0
        for th in soup.find_all("th"):
            if th.get_text(strip=True) == "시가총액(억)":
                td_cap = th.find_next("td")
                if td_cap:
                    raw = td_cap.get_text(strip=True).replace(",", "")
                    try:
                        cap_num = int(raw)
                        market_cap_raw = cap_num
                        if cap_num >= 10000:
                            market_cap = f"{cap_num // 10000}조{cap_num % 10000:,}억"
                        else:
                            market_cap = f"{cap_num:,}억"
                    except ValueError:
                        market_cap = td_cap.get_text(strip=True) + "억"
                break

        volume = "-"
        trade_amount = "-"
        table = soup.select_one("table.no_info")
        if table:
            for td in table.select("td"):
                text = td.get_text()
                if "거래량" in text:
                    blind = td.select_one("span.blind")
                    if blind:
                        volume = blind.get_text(strip=True)
                elif "거래대금" in text:
                    blind = td.select_one("span.blind")
                    if blind:
                        raw_val = blind.get_text(strip=True).replace(",", "")
                        try:
                            mil = int(raw_val)
                            if mil >= 10000:
                                trade_amount = f"{mil // 10000}조{mil % 10000:,}백만원"
                            elif mil >= 1000:
                                trade_amount = f"{mil:,}백만원"
                            else:
                                trade_amount = f"{mil}백만원"
                        except ValueError:
                            trade_amount = blind.get_text(strip=True) + "백만원"

        return {
            "code": code, "name": name, "price": price,
            "change": change, "direction": direction, "rate": rate,
            "market_cap": market_cap, "market_cap_raw": market_cap_raw,
            "volume": volume, "trade_amount": trade_amount,
        }
    except Exception as e:
        return {
            "code": code, "name": name, "price": "-",
            "change": "-", "direction": "flat", "rate": "-",
            "market_cap": "-", "market_cap_raw": 0,
            "volume": "-", "trade_amount": "-", "error": str(e),
        }


def fetch_shares_outstanding(code):
    url = f"https://finance.naver.com/item/main.naver?code={code}"
    resp = requests.get(url, headers=HEADERS, timeout=10)
    soup = BeautifulSoup(resp.text, "html.parser")
    for th in soup.find_all("th"):
        if th.get_text(strip=True) == "상장주식수":
            td = th.find_next("td")
            if td:
                raw = td.get_text(strip=True).replace(",", "")
                try:
                    return int(raw)
                except ValueError:
                    pass
    return None


def fetch_daily_prices(code, max_pages=20):
    all_rows = []
    for page in range(1, max_pages + 1):
        url = f"https://finance.naver.com/item/sise_day.naver?code={code}&page={page}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table.type2 tr")
        found = False
        for row in rows:
            tds = row.select("td")
            if len(tds) >= 7:
                date_text = tds[0].get_text(strip=True)
                close_text = tds[1].get_text(strip=True)
                if date_text and close_text:
                    all_rows.append({"date": date_text, "close": close_text})
                    found = True
        if not found:
            break
    return all_rows


def fetch_daily_ohlc(code, max_pages=20):
    all_rows = []
    cutoff = datetime.now() - timedelta(days=370)
    for page in range(1, max_pages + 1):
        url = f"https://finance.naver.com/item/sise_day.naver?code={code}&page={page}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        rows = soup.select("table.type2 tr")
        found = False
        stop = False
        for row in rows:
            tds = row.select("td")
            if len(tds) >= 7:
                date_text = tds[0].get_text(strip=True)
                close_text = tds[1].get_text(strip=True)
                open_text = tds[3].get_text(strip=True)
                high_text = tds[4].get_text(strip=True)
                low_text = tds[5].get_text(strip=True)
                if date_text and close_text:
                    try:
                        dt = datetime.strptime(date_text, "%Y.%m.%d")
                    except ValueError:
                        continue
                    if dt < cutoff:
                        stop = True
                        break
                    all_rows.append({
                        "date": date_text,
                        "open": int(open_text.replace(",", "")),
                        "high": int(high_text.replace(",", "")),
                        "low": int(low_text.replace(",", "")),
                        "close": int(close_text.replace(",", "")),
                    })
                    found = True
        if stop or not found:
            break
    return all_rows


def aggregate_monthly_candles(daily_rows):
    monthly = defaultdict(lambda: {"open": None, "high": 0, "low": float("inf"),
                                    "close": None, "first_date": None})
    for row in daily_rows:
        ym = row["date"][:7]
        m = monthly[ym]
        if m["first_date"] is None or row["date"] < m["first_date"]:
            m["first_date"] = row["date"]
            m["open"] = row["open"]
        if m["close"] is None or row["date"] > m.get("last_date", ""):
            m["close"] = row["close"]
            m["last_date"] = row["date"]
        m["high"] = max(m["high"], row["high"])
        m["low"] = min(m["low"], row["low"])
    result = []
    for ym in sorted(monthly.keys()):
        m = monthly[ym]
        result.append({
            "month": ym, "open": m["open"], "high": m["high"],
            "low": m["low"], "close": m["close"],
        })
    return result


def format_market_cap(cap_num):
    if cap_num >= 10000:
        return f"{cap_num // 10000}조{cap_num % 10000:,}억"
    return f"{cap_num:,}억"


# --- Helper: fetch stock list for any source ---

def _fetch_stocks_data(stock_list):
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = {ex.submit(fetch_stock, code, name): (code, name)
                   for code, name in stock_list}
        results = [f.result() for f in as_completed(futures)]
    results.sort(key=lambda x: x.get("market_cap_raw", 0), reverse=True)
    return results


def _fetch_one_monthly(code, name):
    try:
        daily = fetch_daily_ohlc(code, max_pages=20)
        candles = aggregate_monthly_candles(daily)
        return code, candles[-12:] if len(candles) > 12 else candles
    except Exception:
        return code, []


def _fetch_monthly_data(stock_list):
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(_fetch_one_monthly, code, name)
                   for code, name in stock_list]
        return {code: candles for f in as_completed(futures)
                for code, candles in [f.result()]}


def _fetch_one_history(code, name, target_date):
    try:
        shares = fetch_shares_outstanding(code)
        daily = fetch_daily_prices(code, max_pages=20)
        matched = None
        for row in daily:
            if row["date"] == target_date:
                matched = row
                break
        if matched:
            close_num = int(matched["close"].replace(",", ""))
            market_cap = "-"
            if shares:
                cap = close_num * shares // 100_000_000
                market_cap = format_market_cap(cap)
            return {
                "code": code, "name": name,
                "date": matched["date"], "close": matched["close"],
                "market_cap": market_cap,
            }
        return {
            "code": code, "name": name, "date": target_date,
            "close": "-", "market_cap": "-",
            "error": "해당 날짜 데이터 없음",
        }
    except Exception as e:
        return {
            "code": code, "name": name, "date": target_date,
            "close": "-", "market_cap": "-", "error": str(e),
        }


def _fetch_history_data(stock_list, target_date):
    with ThreadPoolExecutor(max_workers=10) as ex:
        futures = [ex.submit(_fetch_one_history, code, name, target_date)
                   for code, name in stock_list]
        return [f.result() for f in as_completed(futures)]


def _parse_stocks_param(raw):
    stocks = []
    for item in raw.split(","):
        if ":" in item:
            code, name = item.split(":", 1)
            if re.match(r"^\d{6}$", code):
                stocks.append((code, name))
    return stocks


# --- Routes ---

@app.route("/")
def index():
    return render_template("index.html", groups=GROUPS)


@app.route("/<group_id>")
def group_page(group_id):
    if group_id not in GROUPS:
        return "Not Found", 404
    group = GROUPS[group_id]
    return render_template("group.html", group_id=group_id, group_name=group["name"])


@app.route("/dashboard")
def custom_dashboard():
    raw = request.args.get("stocks", "")
    label = request.args.get("label", "검색 결과")
    stocks = _parse_stocks_param(raw)
    if not stocks:
        return "No stocks selected", 400
    return render_template("dashboard.html", stocks=stocks, label=label)


# --- Search API ---

def _fetch_naver_group_list():
    """네이버 금융 그룹사 목록 (그룹명 -> no 매핑)"""
    url = "https://finance.naver.com/sise/sise_group.naver?type=group"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        groups = {}
        for a in soup.select("a[href*='sise_group_detail']"):
            href = a.get("href", "")
            name = a.get_text(strip=True)
            m = re.search(r"no=(\d+)", href)
            if m and name:
                groups[name] = m.group(1)
        return groups
    except Exception:
        return {}


def _fetch_naver_group_stocks(group_no):
    """네이버 금융 그룹사 상장 계열사 목록"""
    url = f"https://finance.naver.com/sise/sise_group_detail.naver?type=group&no={group_no}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        results = []
        seen = set()
        for a in soup.select("a[href*='/item/main.naver?code=']"):
            m = re.search(r"code=(\d{6})", a.get("href", ""))
            if m:
                code = m.group(1)
                name = a.get_text(strip=True)
                if name and code not in seen:
                    seen.add(code)
                    results.append({"code": code, "name": name, "market": "", "source": "group"})
        return results
    except Exception:
        return []


@app.route("/api/search")
def api_search():
    query = request.args.get("query", "").strip()
    if not query:
        return jsonify([])

    results = []
    seen = set()

    # 1) 네이버 금융 그룹사 매칭 시도
    naver_groups = _fetch_naver_group_list()
    q_clean = query.replace("그룹", "").replace("그룹사", "").strip()

    def _match_score(gname, q):
        """매칭 점수: 낮을수록 좋음"""
        if q == gname:
            return 0  # 완전 일치
        if q in gname:
            return 1  # 쿼리가 그룹명에 포함
        if gname in q:
            return 1  # 그룹명이 쿼리에 포함
        # 공통 글자수 기반 유사도 (많이 겹칠수록 좋음)
        common = sum(1 for c in q if c in gname)
        if common >= 2 and gname[:2] == q[:2]:
            return 10 - common  # 공통 글자가 많을수록 점수 낮음
        return 999

    candidates = []
    if q_clean and len(q_clean) >= 2:
        for gname, gno in naver_groups.items():
            score = _match_score(gname, q_clean)
            if score < 999:
                candidates.append((gname, gno, score))
        candidates.sort(key=lambda x: x[2])
    if candidates:
        group_stocks = _fetch_naver_group_stocks(candidates[0][1])
        for s in group_stocks:
            if s["code"] not in seen:
                seen.add(s["code"])
                results.append(s)

    # 2) 네이버 자동완성 검색 병합
    try:
        url = f"https://ac.stock.naver.com/ac?q={query}&target=stock"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        data = resp.json()
        for item in data.get("items", []):
            if item.get("nationCode") == "KOR":
                code = item["code"]
                if code not in seen:
                    seen.add(code)
                    results.append({
                        "code": code,
                        "name": item["name"],
                        "market": item.get("typeName", ""),
                    })
    except Exception:
        pass

    return jsonify(results)


# --- Group API (existing) ---

@app.route("/api/<group_id>/stocks")
def api_stocks(group_id):
    if group_id not in GROUPS:
        return jsonify({"error": "Unknown group"}), 404
    return jsonify(_fetch_stocks_data(GROUPS[group_id]["stocks"]))


@app.route("/api/<group_id>/stocks/monthly")
def api_stocks_monthly(group_id):
    if group_id not in GROUPS:
        return jsonify({"error": "Unknown group"}), 404
    return jsonify(_fetch_monthly_data(GROUPS[group_id]["stocks"]))


@app.route("/api/<group_id>/stocks/history")
def api_stocks_history(group_id):
    if group_id not in GROUPS:
        return jsonify({"error": "Unknown group"}), 404
    target_date = request.args.get("date", "").replace("-", ".")
    return jsonify(_fetch_history_data(GROUPS[group_id]["stocks"], target_date))


# --- Custom Dashboard API ---

@app.route("/api/custom/stocks", methods=["POST"])
def api_custom_stocks():
    stock_list = [(s["code"], s["name"]) for s in (request.json or [])]
    return jsonify(_fetch_stocks_data(stock_list))


@app.route("/api/custom/stocks/monthly", methods=["POST"])
def api_custom_monthly():
    stock_list = [(s["code"], s["name"]) for s in (request.json or [])]
    return jsonify(_fetch_monthly_data(stock_list))


@app.route("/api/custom/stocks/history", methods=["POST"])
def api_custom_history():
    stock_list = [(s["code"], s["name"]) for s in (request.json or [])]
    target_date = request.args.get("date", "").replace("-", ".")
    return jsonify(_fetch_history_data(stock_list, target_date))


if __name__ == "__main__":
    app.run(debug=True, port=8080)
