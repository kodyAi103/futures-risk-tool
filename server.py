from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import json
import math
import os
import time


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
GATE_API = "https://api.gateio.ws/api/v4"
BITGET_API = "https://api.bitget.com/api/v2"
CACHE_TTL = 60
BASE_DIR = Path(__file__).resolve().parent
cache = {}


def fetch_json(url, ttl=CACHE_TTL):
    now = time.time()
    cached = cache.get(url)
    if ttl > 0 and cached and now - cached["time"] < ttl:
        return cached["data"]

    req = Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Local futures risk tool/1.0",
        },
    )
    with urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if ttl > 0:
        cache[url] = {"time": now, "data": data}
    return data


def as_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def format_percent(value):
    return round(as_float(value) * 100, 4)


def format_number(value, digits=4):
    number = as_float(value)
    if number == 0:
        return 0
    return round(number, digits)


def load_coin_overviews():
    path = BASE_DIR / "data" / "coin_overviews.json"
    try:
        with open(path, "r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        return {}


def contract_size_from_limit(risk_limit, mark_price, multiplier):
    unit_value = mark_price * multiplier
    if unit_value <= 0:
        return 0
    return math.floor(as_float(risk_limit) / unit_value)


def approx_usdt_from_size(size, mark_price, multiplier):
    return round(size * mark_price * multiplier, 4)


def integer_leverage(value):
    leverage = as_float(value)
    if leverage <= 1:
        return 1
    return math.floor(leverage)


def enrich_gate_tier(tier, mark_price, multiplier):
    size = contract_size_from_limit(tier.get("risk_limit"), mark_price, multiplier)
    return {
        "tier": tier.get("tier"),
        "risk_limit_contracts": size,
        "risk_limit_usdt": approx_usdt_from_size(size, mark_price, multiplier),
        "leverage_max": format_number(tier.get("leverage_max"), 4),
        "initial_rate": format_percent(tier.get("initial_rate")),
        "maintenance_rate": format_percent(tier.get("maintenance_rate")),
        "source_risk_limit_usdt": format_number(tier.get("risk_limit"), 4),
    }


def simplify_tiers(tiers):
    if len(tiers) <= 10:
        return tiers

    simplified = []
    for index in range(0, len(tiers), 2):
        group = tiers[index : index + 2]
        chosen = group[-1].copy()
        chosen["tier"] = len(simplified) + 1
        chosen["leverage_max"] = integer_leverage(chosen.get("leverage_max"))
        chosen["source_tiers"] = "-".join(str(item["tier"]) for item in group)
        simplified.append(chosen)
    return simplified


def contract_intro(base):
    overviews = load_coin_overviews()
    return overviews.get(base) or "暂无币种概况。"


def unwrap_bitget(response):
    if response.get("code") != "00000":
        raise ValueError(response.get("msg") or "Bitget API request failed")
    return response.get("data") or []


def get_gate_contracts():
    contracts = fetch_json(f"{GATE_API}/futures/usdt/contracts")
    result = []
    for item in contracts:
        status = item.get("status", "")
        if status in ("trading", "pre_market") or not status:
            result.append(
                {
                    "name": item.get("name"),
                    "status": status,
                    "mark_price": item.get("mark_price"),
                    "leverage_max": item.get("leverage_max"),
                    "contract_type": item.get("contract_type"),
                }
            )
    return sorted(result, key=lambda row: row["name"] or "")


def get_bitget_contracts():
    contracts = unwrap_bitget(fetch_json(f"{BITGET_API}/mix/market/contracts?productType=usdt-futures"))
    tickers = unwrap_bitget(fetch_json(f"{BITGET_API}/mix/market/tickers?productType=usdt-futures"))
    ticker_by_symbol = {item.get("symbol"): item for item in tickers}
    result = []
    for item in contracts:
        status = item.get("symbolStatus", "")
        if status in ("normal", "maintain", "restrictedAPI") or not status:
            ticker = ticker_by_symbol.get(item.get("symbol"), {})
            result.append(
                {
                    "name": item.get("symbol"),
                    "status": status,
                    "mark_price": ticker.get("markPrice"),
                    "leverage_max": item.get("maxLever"),
                    "contract_type": item.get("symbolType"),
                }
            )
    return sorted(result, key=lambda row: row["name"] or "")


def get_contracts(exchange):
    if exchange == "gate":
        return get_gate_contracts()
    if exchange == "bitget":
        return get_bitget_contracts()
    raise ValueError("unsupported exchange")


def get_gate_contract_detail(name):
    contract = fetch_json(f"{GATE_API}/futures/usdt/contracts/{name}", ttl=0)
    tiers = fetch_json(f"{GATE_API}/futures/usdt/risk_limit_tiers?contract={name}", ttl=0)
    mark_price = as_float(contract.get("mark_price") or contract.get("index_price"))
    multiplier = as_float(contract.get("quanto_multiplier"), 1)
    enriched = [enrich_gate_tier(tier, mark_price, multiplier) for tier in tiers]

    return {
        "exchange": "gate.io",
        "intro": contract_intro(name.split("_")[0]),
        "updated_at": int(time.time()),
        "contract": {
            "name": contract.get("name"),
            "status": contract.get("status"),
            "leverage_min": contract.get("leverage_min"),
            "leverage_max": contract.get("leverage_max"),
            "cross_leverage_default": contract.get("cross_leverage_default"),
            "maintenance_rate": format_percent(contract.get("maintenance_rate")),
            "risk_limit_base": contract.get("risk_limit_base"),
            "risk_limit_max": contract.get("risk_limit_max"),
            "quanto_multiplier": contract.get("quanto_multiplier"),
            "order_price_round": contract.get("order_price_round"),
            "mark_price": contract.get("mark_price"),
            "index_price": contract.get("index_price"),
        },
        "tiers": enriched,
        "simplified_tiers": simplify_tiers(enriched),
    }


def bitget_price_step(contract):
    places = int(as_float(contract.get("pricePlace"), 0))
    if places <= 0:
        return "1"
    return "0." + ("0" * (places - 1)) + "1"


def enrich_bitget_tier(tier, mark_price, multiplier):
    risk_limit = tier.get("endUnit")
    size = contract_size_from_limit(risk_limit, mark_price, multiplier)
    leverage = as_float(tier.get("leverage"))
    initial_rate = 1 / leverage if leverage > 0 else 0
    return {
        "tier": int(as_float(tier.get("level"))),
        "risk_limit_contracts": size,
        "risk_limit_usdt": approx_usdt_from_size(size, mark_price, multiplier),
        "leverage_max": integer_leverage(tier.get("leverage")),
        "initial_rate": round(initial_rate * 100, 4),
        "maintenance_rate": format_percent(tier.get("keepMarginRate")),
        "source_risk_limit_usdt": format_number(risk_limit, 4),
    }


def get_bitget_contract_detail(name):
    contract_data = unwrap_bitget(
        fetch_json(f"{BITGET_API}/mix/market/contracts?productType=usdt-futures&symbol={name}", ttl=0)
    )
    price_data = unwrap_bitget(
        fetch_json(f"{BITGET_API}/mix/market/symbol-price?productType=usdt-futures&symbol={name}", ttl=0)
    )
    tier_data = unwrap_bitget(
        fetch_json(f"{BITGET_API}/mix/market/query-position-lever?productType=usdt-futures&symbol={name}", ttl=0)
    )
    if not contract_data:
        raise ValueError(f"contract not found: {name}")

    contract = contract_data[0]
    price = price_data[0] if price_data else {}
    mark_price = as_float(price.get("markPrice") or price.get("price"))
    multiplier = as_float(contract.get("sizeMultiplier"), 1)
    enriched = [enrich_bitget_tier(tier, mark_price, multiplier) for tier in tier_data]
    risk_limits = [as_float(tier.get("source_risk_limit_usdt")) for tier in enriched]
    first_tier = enriched[0] if enriched else {}

    return {
        "exchange": "bitget.com",
        "intro": contract_intro(contract.get("baseCoin") or name.replace("USDT", "")),
        "updated_at": int(time.time()),
        "contract": {
            "name": contract.get("symbol"),
            "status": contract.get("symbolStatus"),
            "leverage_min": contract.get("minLever"),
            "leverage_max": contract.get("maxLever"),
            "cross_leverage_default": "-",
            "maintenance_rate": first_tier.get("maintenance_rate", "-"),
            "risk_limit_base": first_tier.get("source_risk_limit_usdt", "-"),
            "risk_limit_max": max(risk_limits) if risk_limits else "-",
            "quanto_multiplier": contract.get("sizeMultiplier"),
            "order_price_round": bitget_price_step(contract),
            "mark_price": price.get("markPrice"),
            "index_price": price.get("indexPrice"),
        },
        "tiers": enriched,
        "simplified_tiers": simplify_tiers(enriched),
    }


def get_contract_detail(exchange, name):
    if exchange == "gate":
        return get_gate_contract_detail(name)
    if exchange == "bitget":
        return get_bitget_contract_detail(name)
    raise ValueError("unsupported exchange")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, data, status=200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self):
        with open(BASE_DIR / "web" / "index.html", "rb") as file:
            payload = file.read()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                self.send_html()
            elif parsed.path == "/api/exchanges":
                self.send_json([{"id": "gate", "name": "gate.io"}, {"id": "bitget", "name": "bitget.com"}])
            elif parsed.path == "/api/contracts":
                exchange = parse_qs(parsed.query).get("exchange", ["gate"])[0].strip().lower()
                self.send_json(get_contracts(exchange))
            elif parsed.path == "/api/contract":
                query = parse_qs(parsed.query)
                exchange = query.get("exchange", ["gate"])[0].strip().lower()
                name = query.get("name", [""])[0].strip().upper()
                if not name:
                    self.send_json({"error": "missing contract name"}, 400)
                    return
                self.send_json(get_contract_detail(exchange, name))
            else:
                self.send_json({"error": "not found"}, 404)
        except HTTPError as exc:
            self.send_json({"error": exc.reason, "status": exc.code}, exc.code)
        except (URLError, TimeoutError) as exc:
            self.send_json({"error": f"network error: {exc}"}, 502)
        except Exception as exc:
            self.send_json({"error": str(exc)}, 500)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Open http://{HOST}:{PORT}")
    server.serve_forever()
