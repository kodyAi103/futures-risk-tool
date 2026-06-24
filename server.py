from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen
import json
import math
import os
import time


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8765"))
GATE_API = "https://api.gateio.ws/api/v4"
BITGET_API = "https://api.bitget.com/api/v2"
BYBIT_API = "https://api.bybit.com/v5"
OKX_API = "https://www.okx.com/api/v5"
MEXC_API = "https://contract.mexc.com/api/v1"
BINANCE_API = "https://fapi.binance.com"
BINANCE_WEB_API = "https://www.binance.com/bapi/futures/v1/public/future/common"
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


PREFERRED_SIMPLIFIED_LEVERAGES = [200, 150, 125, 100, 75, 50, 25, 18, 12, 8, 6, 4, 2, 1]


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
    if not tiers:
        return []

    def simplified_row(tier, new_tier):
        chosen = tier.copy()
        leverage = integer_leverage(chosen.get("leverage_max"))
        chosen["tier"] = new_tier
        chosen["leverage_max"] = leverage
        chosen["initial_rate"] = round(100 / leverage, 4)
        chosen["source_tiers"] = str(tier["tier"])
        return chosen

    if len(tiers) <= 10:
        return [simplified_row(tier, index + 1) for index, tier in enumerate(tiers)]

    tier_leverages = [integer_leverage(tier.get("leverage_max")) for tier in tiers]
    max_leverage = max(tier_leverages)
    min_leverage = min(tier_leverages)
    targets = [max_leverage]
    targets.extend(
        leverage
        for leverage in PREFERRED_SIMPLIFIED_LEVERAGES
        if min_leverage <= leverage <= max_leverage and leverage != max_leverage
    )
    if min_leverage not in targets:
        targets.append(min_leverage)

    targets = list(dict.fromkeys(targets))
    if len(targets) > 8:
        last_index = len(targets) - 1
        targets = [targets[round(index * last_index / 7)] for index in range(8)]
        targets = list(dict.fromkeys(targets))

    selected_indexes = []
    for target in targets:
        if target == targets[-1]:
            index = len(tiers) - 1
        else:
            index = next(
                (
                    candidate
                    for candidate, leverage in enumerate(tier_leverages)
                    if candidate not in selected_indexes and leverage <= target
                ),
                len(tiers) - 1,
            )
        if index not in selected_indexes:
            selected_indexes.append(index)

    selected_indexes.sort()
    return [simplified_row(tiers[index], position + 1) for position, index in enumerate(selected_indexes)]


def contract_intro(base):
    overviews = load_coin_overviews()
    return overviews.get(base) or "暂无币种概况。"


def unwrap_bitget(response):
    if response.get("code") != "00000":
        raise ValueError(response.get("msg") or "Bitget API request failed")
    return response.get("data") or []


def unwrap_bybit(response):
    if response.get("retCode") != 0:
        raise ValueError(response.get("retMsg") or "Bybit API request failed")
    return response.get("result") or {}


def unwrap_okx(response):
    if response.get("code") != "0":
        raise ValueError(response.get("msg") or "OKX API request failed")
    return response.get("data") or []


def unwrap_mexc(response):
    if not response.get("success"):
        raise ValueError(response.get("message") or "MEXC API request failed")
    return response.get("data")


def make_notional_tier(tier, risk_limit, mark_price, multiplier, leverage, initial_rate, maintenance_rate):
    size = contract_size_from_limit(risk_limit, mark_price, multiplier)
    return {
        "tier": int(as_float(tier)),
        "risk_limit_contracts": size,
        "risk_limit_usdt": approx_usdt_from_size(size, mark_price, multiplier),
        "leverage_max": format_number(leverage, 4),
        "initial_rate": format_percent(initial_rate),
        "maintenance_rate": format_percent(maintenance_rate),
        "source_risk_limit_usdt": format_number(risk_limit, 4),
    }


def price_filter_value(contract, filter_type, key):
    item = next((row for row in contract.get("filters", []) if row.get("filterType") == filter_type), {})
    return item.get(key, "-")


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


def get_bybit_contracts():
    result = []
    cursor = ""
    while True:
        params = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = unwrap_bybit(fetch_json(f"{BYBIT_API}/market/instruments-info?{urlencode(params)}"))
        for item in page.get("list", []):
            if item.get("settleCoin") != "USDT" or item.get("contractType") != "LinearPerpetual":
                continue
            leverage = item.get("leverageFilter", {})
            result.append({
                "name": item.get("symbol"),
                "status": item.get("status"),
                "mark_price": "-",
                "leverage_max": leverage.get("maxLeverage"),
                "contract_type": item.get("contractType"),
            })
        cursor = page.get("nextPageCursor") or ""
        if not cursor:
            break
    return sorted(result, key=lambda row: row["name"] or "")


def get_okx_contracts():
    instruments = unwrap_okx(fetch_json(f"{OKX_API}/public/instruments?instType=SWAP"))
    tickers = unwrap_okx(fetch_json(f"{OKX_API}/market/tickers?instType=SWAP"))
    ticker_by_name = {item.get("instId"): item for item in tickers}
    result = []
    for item in instruments:
        if item.get("settleCcy") != "USDT" or item.get("ctType") != "linear":
            continue
        ticker = ticker_by_name.get(item.get("instId"), {})
        result.append({
            "name": item.get("instId"),
            "status": item.get("state"),
            "mark_price": ticker.get("last"),
            "leverage_max": item.get("lever"),
            "contract_type": item.get("instType"),
        })
    return sorted(result, key=lambda row: row["name"] or "")


def get_mexc_contracts():
    contracts = unwrap_mexc(fetch_json(f"{MEXC_API}/contract/detail")) or []
    return sorted([
        {
            "name": item.get("symbol"),
            "status": "trading" if item.get("state") == 0 else str(item.get("state")),
            "mark_price": "-",
            "leverage_max": item.get("maxLeverage"),
            "contract_type": "perpetual",
        }
        for item in contracts
        if item.get("quoteCoin") == "USDT" and not item.get("isHidden")
    ], key=lambda row: row["name"] or "")


def get_binance_contracts():
    response = fetch_json(f"{BINANCE_API}/fapi/v1/exchangeInfo")
    return sorted([
        {
            "name": item.get("symbol"),
            "status": item.get("status"),
            "mark_price": "-",
            "leverage_max": "-",
            "contract_type": item.get("contractType"),
        }
        for item in response.get("symbols", [])
        if item.get("quoteAsset") == "USDT" and item.get("contractType") == "PERPETUAL"
    ], key=lambda row: row["name"] or "")


def get_contracts(exchange):
    if exchange == "gate":
        return get_gate_contracts()
    if exchange == "bitget":
        return get_bitget_contracts()
    if exchange == "bybit":
        return get_bybit_contracts()
    if exchange == "okx":
        return get_okx_contracts()
    if exchange == "mexc":
        return get_mexc_contracts()
    if exchange == "binance":
        return get_binance_contracts()
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


def get_bybit_contract_detail(name):
    params = urlencode({"category": "linear", "symbol": name})
    risk_params = urlencode({"category": "linear", "symbol": name, "limit": 1000})
    instruments = unwrap_bybit(fetch_json(f"{BYBIT_API}/market/instruments-info?{params}", ttl=0)).get("list", [])
    tickers = unwrap_bybit(fetch_json(f"{BYBIT_API}/market/tickers?{params}", ttl=0)).get("list", [])
    tiers = unwrap_bybit(fetch_json(f"{BYBIT_API}/market/risk-limit?{risk_params}", ttl=0)).get("list", [])
    if not instruments:
        raise ValueError(f"contract not found: {name}")
    contract = instruments[0]
    ticker = tickers[0] if tickers else {}
    mark_price = as_float(ticker.get("markPrice") or ticker.get("indexPrice"))
    enriched = [make_notional_tier(
        item.get("id"), item.get("riskLimitValue"), mark_price, 1,
        item.get("maxLeverage"), item.get("initialMargin"), item.get("maintenanceMargin")
    ) for item in tiers]
    limits = [as_float(item.get("riskLimitValue")) for item in tiers]
    leverage = contract.get("leverageFilter", {})
    return {
        "exchange": "bybit.com", "intro": contract_intro(contract.get("baseCoin")), "updated_at": int(time.time()),
        "contract": {
            "name": contract.get("symbol"), "status": contract.get("status"),
            "leverage_min": leverage.get("minLeverage"), "leverage_max": leverage.get("maxLeverage"),
            "cross_leverage_default": "-", "maintenance_rate": enriched[0]["maintenance_rate"] if enriched else "-",
            "risk_limit_base": limits[0] if limits else "-", "risk_limit_max": max(limits) if limits else "-",
            "quanto_multiplier": 1, "order_price_round": contract.get("priceFilter", {}).get("tickSize"),
            "mark_price": ticker.get("markPrice"), "index_price": ticker.get("indexPrice"),
        },
        "tiers": enriched, "simplified_tiers": simplify_tiers(enriched),
    }


def get_okx_contract_detail(name):
    instruments = unwrap_okx(fetch_json(f"{OKX_API}/public/instruments?instType=SWAP&instId={quote(name)}", ttl=0))
    if not instruments:
        raise ValueError(f"contract not found: {name}")
    contract = instruments[0]
    mark_rows = unwrap_okx(fetch_json(f"{OKX_API}/public/mark-price?instType=SWAP&instId={quote(name)}", ttl=0))
    index_rows = unwrap_okx(fetch_json(f"{OKX_API}/market/index-tickers?instId={quote(contract.get('uly', ''))}", ttl=0))
    tier_params = urlencode({"instType": "SWAP", "tdMode": "cross", "uly": contract.get("uly", "")})
    tiers = unwrap_okx(fetch_json(f"{OKX_API}/public/position-tiers?{tier_params}", ttl=0))
    mark_price = as_float(mark_rows[0].get("markPx") if mark_rows else 0)
    multiplier = as_float(contract.get("ctVal"), 1) * as_float(contract.get("ctMult"), 1)
    enriched = []
    for item in tiers:
        size = int(as_float(item.get("maxSz")))
        enriched.append({
            "tier": int(as_float(item.get("tier"))), "risk_limit_contracts": size,
            "risk_limit_usdt": approx_usdt_from_size(size, mark_price, multiplier),
            "leverage_max": format_number(item.get("maxLever"), 4),
            "initial_rate": format_percent(item.get("imr")), "maintenance_rate": format_percent(item.get("mmr")),
            "source_risk_limit_usdt": approx_usdt_from_size(size, mark_price, multiplier),
        })
    return {
        "exchange": "okx.com", "intro": contract_intro(contract.get("uly", "").split("-")[0]), "updated_at": int(time.time()),
        "contract": {
            "name": contract.get("instId"), "status": contract.get("state"), "leverage_min": 1,
            "leverage_max": contract.get("lever"), "cross_leverage_default": "-",
            "maintenance_rate": enriched[0]["maintenance_rate"] if enriched else "-",
            "risk_limit_base": enriched[0]["risk_limit_contracts"] if enriched else "-",
            "risk_limit_max": enriched[-1]["risk_limit_contracts"] if enriched else "-",
            "quanto_multiplier": multiplier, "order_price_round": contract.get("tickSz"),
            "mark_price": mark_rows[0].get("markPx") if mark_rows else "-",
            "index_price": index_rows[0].get("idxPx") if index_rows else "-",
        },
        "tiers": enriched, "simplified_tiers": simplify_tiers(enriched),
    }


def get_mexc_contract_detail(name):
    contracts = unwrap_mexc(fetch_json(f"{MEXC_API}/contract/detail", ttl=0)) or []
    contract = next((item for item in contracts if item.get("symbol") == name), None)
    if not contract:
        raise ValueError(f"contract not found: {name}")
    ticker = unwrap_mexc(fetch_json(f"{MEXC_API}/contract/ticker?symbol={quote(name)}", ttl=0)) or {}
    mark_price = as_float(ticker.get("fairPrice") or ticker.get("indexPrice"))
    multiplier = as_float(contract.get("contractSize"), 1)
    tiers = contract.get("riskLimitCustom") or []
    if not tiers:
        level_count = max(1, int(as_float(contract.get("riskLevelLimit"), 1)))
        base_volume = as_float(contract.get("riskBaseVol"))
        volume_increment = as_float(contract.get("riskIncrVol"))
        base_mmr = as_float(contract.get("maintenanceMarginRate"))
        base_imr = as_float(contract.get("initialMarginRate"))
        tiers = [
            {
                "level": level,
                "maxVol": base_volume + volume_increment * (level - 1),
                "mmr": base_mmr + as_float(contract.get("riskIncrMmr")) * (level - 1),
                "imr": base_imr + as_float(contract.get("riskIncrImr")) * (level - 1),
                "maxLeverage": 1 / (base_imr + as_float(contract.get("riskIncrImr")) * (level - 1)),
            }
            for level in range(1, level_count + 1)
            if base_imr + as_float(contract.get("riskIncrImr")) * (level - 1) > 0
        ]
    enriched = []
    for item in tiers:
        size = int(as_float(item.get("maxVol")))
        enriched.append({
            "tier": int(as_float(item.get("level"))), "risk_limit_contracts": size,
            "risk_limit_usdt": approx_usdt_from_size(size, mark_price, multiplier),
            "leverage_max": format_number(item.get("maxLeverage"), 4),
            "initial_rate": format_percent(item.get("imr")), "maintenance_rate": format_percent(item.get("mmr")),
            "source_risk_limit_usdt": approx_usdt_from_size(size, mark_price, multiplier),
        })
    return {
        "exchange": "mexc.com", "intro": contract_intro(contract.get("baseCoin")), "updated_at": int(time.time()),
        "contract": {
            "name": contract.get("symbol"), "status": "trading" if contract.get("state") == 0 else contract.get("state"),
            "leverage_min": contract.get("minLeverage"), "leverage_max": contract.get("maxLeverage"),
            "cross_leverage_default": "-", "maintenance_rate": format_percent(contract.get("maintenanceMarginRate")),
            "risk_limit_base": enriched[0]["risk_limit_contracts"] if enriched else contract.get("riskBaseVol"),
            "risk_limit_max": enriched[-1]["risk_limit_contracts"] if enriched else contract.get("riskBaseVol"),
            "quanto_multiplier": contract.get("contractSize"), "order_price_round": contract.get("priceUnit"),
            "mark_price": ticker.get("fairPrice"), "index_price": ticker.get("indexPrice"),
        },
        "tiers": enriched, "simplified_tiers": simplify_tiers(enriched),
    }


def unwrap_binance_brackets(response):
    data = response.get("data", response)
    if isinstance(data, dict):
        data = data.get("brackets", data.get("list", []))
    if isinstance(data, list) and data and "brackets" in data[0]:
        data = data[0].get("brackets", [])
    if not isinstance(data, list):
        raise ValueError("Binance risk tiers unavailable")
    return data


def get_binance_contract_detail(name):
    exchange_info = fetch_json(f"{BINANCE_API}/fapi/v1/exchangeInfo", ttl=0)
    contract = next((item for item in exchange_info.get("symbols", []) if item.get("symbol") == name), None)
    if not contract:
        raise ValueError(f"contract not found: {name}")
    price = fetch_json(f"{BINANCE_API}/fapi/v1/premiumIndex?symbol={quote(name)}", ttl=0)
    bracket_response = fetch_json(f"{BINANCE_WEB_API}/brackets?symbol={quote(name)}", ttl=0)
    tiers = unwrap_binance_brackets(bracket_response)
    mark_price = as_float(price.get("markPrice") or price.get("indexPrice"))
    enriched = [make_notional_tier(
        item.get("bracket"), item.get("notionalCap"), mark_price, 1,
        item.get("initialLeverage"), 1 / as_float(item.get("initialLeverage"), 1), item.get("maintMarginRatio")
    ) for item in tiers]
    limits = [as_float(item.get("notionalCap")) for item in tiers]
    return {
        "exchange": "binance.com", "intro": contract_intro(contract.get("baseAsset")), "updated_at": int(time.time()),
        "contract": {
            "name": contract.get("symbol"), "status": contract.get("status"), "leverage_min": 1,
            "leverage_max": tiers[0].get("initialLeverage") if tiers else "-", "cross_leverage_default": "-",
            "maintenance_rate": enriched[0]["maintenance_rate"] if enriched else "-",
            "risk_limit_base": limits[0] if limits else "-", "risk_limit_max": max(limits) if limits else "-",
            "quanto_multiplier": 1, "order_price_round": price_filter_value(contract, "PRICE_FILTER", "tickSize"),
            "mark_price": price.get("markPrice"), "index_price": price.get("indexPrice"),
        },
        "tiers": enriched, "simplified_tiers": simplify_tiers(enriched),
    }


def get_contract_detail(exchange, name):
    if exchange == "gate":
        return get_gate_contract_detail(name)
    if exchange == "bitget":
        return get_bitget_contract_detail(name)
    if exchange == "bybit":
        return get_bybit_contract_detail(name)
    if exchange == "okx":
        return get_okx_contract_detail(name)
    if exchange == "mexc":
        return get_mexc_contract_detail(name)
    if exchange == "binance":
        return get_binance_contract_detail(name)
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
                self.send_json([
                    {"id": "gate", "name": "gate.io"},
                    {"id": "bitget", "name": "bitget.com"},
                    {"id": "bybit", "name": "bybit.com"},
                    {"id": "okx", "name": "okx.com"},
                    {"id": "mexc", "name": "mexc.com"},
                    {"id": "binance", "name": "binance.com"},
                ])
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
