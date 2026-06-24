import unittest
from unittest.mock import patch

import server


class ExchangeAdapterTests(unittest.TestCase):
    def test_bybit_detail_maps_public_risk_tiers(self):
        def fake_fetch(url, ttl=server.CACHE_TTL):
            if "instruments-info" in url:
                return {"retCode": 0, "result": {"list": [{
                    "symbol": "BTCUSDT", "baseCoin": "BTC", "status": "Trading",
                    "leverageFilter": {"minLeverage": "1", "maxLeverage": "100"},
                    "priceFilter": {"tickSize": "0.1"},
                }]}}
            if "tickers" in url:
                return {"retCode": 0, "result": {"list": [{"markPrice": "50000", "indexPrice": "50010"}]}}
            return {"retCode": 0, "result": {"list": [{
                "id": 1, "riskLimitValue": "100000", "maxLeverage": "100",
                "initialMargin": "0.01", "maintenanceMargin": "0.005",
            }]}}

        with patch.object(server, "fetch_json", side_effect=fake_fetch):
            detail = server.get_bybit_contract_detail("BTCUSDT")

        self.assertEqual(detail["contract"]["mark_price"], "50000")
        self.assertEqual(detail["tiers"][0]["risk_limit_contracts"], 2)
        self.assertEqual(detail["tiers"][0]["maintenance_rate"], 0.5)

    def test_binance_detail_accepts_nested_brackets(self):
        def fake_fetch(url, ttl=server.CACHE_TTL):
            if "exchangeInfo" in url:
                return {"symbols": [{
                    "symbol": "BTCUSDT", "baseAsset": "BTC", "status": "TRADING",
                    "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.1"}],
                }]}
            if "premiumIndex" in url:
                return {"markPrice": "50000", "indexPrice": "50010"}
            return {"data": {"brackets": [{
                "bracket": 1, "notionalCap": 100000, "initialLeverage": 125,
                "maintMarginRatio": 0.004,
            }]}}

        with patch.object(server, "fetch_json", side_effect=fake_fetch):
            detail = server.get_binance_contract_detail("BTCUSDT")

        self.assertEqual(detail["contract"]["leverage_max"], 125)
        self.assertEqual(detail["contract"]["order_price_round"], "0.1")
        self.assertEqual(detail["tiers"][0]["risk_limit_contracts"], 2)
        self.assertEqual(detail["tiers"][0]["initial_rate"], 0.8)


if __name__ == "__main__":
    unittest.main()
