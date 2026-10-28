"""Offline fixtures for the public delayed/indicative Yahoo quote parser."""

import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from live.feeds import QuoteFeedError, YahooQuoteFeed

NOW = 1_000


def fixture(symbol="RELIANCE.NS"):
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": "INR", "exchangeTimezoneName": "Asia/Kolkata",
                 "regularMarketPrice": 9999.99,
                 "currentTradingPeriod": {"regular": {"start": 500, "end": 1500, "gmtoffset": 19800}}},
        "timestamp": [800, 860, 920, 980],
        "indicators": {"quote": [{"close": [100.11, None, 100.125, 102.0]}]},
    }]}}


class QuoteFeedTests(unittest.TestCase):
    def fetch(self, payload=None, *, now=NOW, symbol="RELIANCE.NS", raw=None):
        body = raw if raw is not None else json.dumps(fixture(symbol) if payload is None else payload).encode()
        with patch("live.feeds.urlopen", return_value=io.BytesIO(body)), patch("live.feeds.time.time", return_value=now):
            return YahooQuoteFeed(symbol).fetch()

    def test_latest_completed_close_is_paired_and_rounds_half_up(self):
        quote = self.fetch()
        self.assertEqual((quote.price_paise, quote.source_time_s), (10013, 920))
        self.assertEqual(quote.symbol, "RELIANCE.NS")
        self.assertEqual(quote.currency, "INR")
        self.assertTrue(quote.market_open)
        self.assertTrue(quote.delayed)
        self.assertIn("delayed/indicative", quote.source)

    def test_null_latest_close_uses_matching_earlier_timestamp(self):
        data = fixture()
        data["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [100, None, None, None]
        quote = self.fetch(data)
        self.assertEqual((quote.source_time_s, quote.price_paise), (800, 10000))

    def test_completion_and_session_boundaries_use_epoch_time(self):
        self.assertEqual(self.fetch(now=980).source_time_s, 920)
        self.assertEqual(self.fetch(now=979).source_time_s, 800)
        self.assertFalse(self.fetch(now=1500).market_open)
        self.assertFalse(self.fetch(now=1600).market_open)
        self.assertEqual(self.fetch(now=1040).source_time_s, 980)

    def test_duplicate_source_timestamp_is_returned_for_caller_deduplication(self):
        self.assertEqual(self.fetch(), self.fetch())

    def test_bse_and_normalized_symbol(self):
        self.assertEqual(self.fetch(symbol="500325.BO").symbol, "500325.BO")
        self.assertEqual(YahooQuoteFeed(" m&m.ns ").symbol, "M&M.NS")

    def test_only_indian_equity_symbol_suffixes(self):
        for symbol in ["AAPL", "BTC-USD", "^NSEI", "RELIANCE.NS/path", ".NS", "https://a.com/.NS", "ABC..NS", None]:
            with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                YahooQuoteFeed(symbol)

    def test_bad_timeout_rejected(self):
        for timeout in [0, -1, 31, float("inf"), float("nan"), True, "10"]:
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                YahooQuoteFeed("RELIANCE.NS", timeout_s=timeout)

    def test_missing_or_invalid_shapes_rejected(self):
        mutations = [
            lambda x: x.pop("chart"),
            lambda x: x["chart"].update(error={"code": "Not Found"}),
            lambda x: x["chart"].update(result=[]),
            lambda x: x["chart"].update(result=[{}, {}]),
            lambda x: x["chart"]["result"][0].update(timestamp=[]),
            lambda x: x["chart"]["result"][0].update(timestamp=[800]),
            lambda x: x["chart"]["result"][0]["indicators"].update(quote=[]),
            lambda x: x["chart"]["result"][0]["indicators"].update(quote=[{}, {}]),
            lambda x: x["chart"]["result"][0]["indicators"]["quote"][0].update(close="100"),
        ]
        for mutate in mutations:
            data = fixture()
            mutate(data)
            with self.subTest(data=data), self.assertRaises(QuoteFeedError):
                self.fetch(data)

    def test_symbol_currency_timezone_and_session_validation(self):
        changes = [
            {"symbol": "OTHER.NS"}, {"currency": "USD"}, {"currency": None},
            {"exchangeTimezoneName": "America/New_York"}, {"exchangeTimezoneName": []}, {"currentTradingPeriod": None},
            {"currentTradingPeriod": {"regular": {"start": 500, "end": 500, "gmtoffset": 19800}}},
            {"currentTradingPeriod": {"regular": {"start": True, "end": 1500, "gmtoffset": 19800}}},
            {"currentTradingPeriod": {"regular": {"start": 500, "end": 1500, "gmtoffset": 0}}},
        ]
        for change in changes:
            data = fixture()
            data["chart"]["result"][0]["meta"].update(change)
            with self.subTest(change=change), self.assertRaises(QuoteFeedError):
                self.fetch(data)

    def test_bad_prices_and_timestamps_fail_closed(self):
        for price in [0, -1, "100.1", True, [], {}]:
            data = fixture()
            data["chart"]["result"][0]["indicators"]["quote"][0]["close"][2] = price
            with self.subTest(price=price), self.assertRaises(QuoteFeedError):
                self.fetch(data)
        for times in [[800, 800, 920, 980], [800, 790, 920, 980], [True, 860, 920, 980], [800, 860.0, 920, 980], [-1, 860, 920, 980]]:
            data = fixture()
            data["chart"]["result"][0]["timestamp"] = times
            with self.subTest(times=times), self.assertRaises(QuoteFeedError):
                self.fetch(data)

    def test_no_completed_price_rejected(self):
        data = fixture()
        data["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [None] * 4
        with self.assertRaises(QuoteFeedError):
            self.fetch(data)
        with self.assertRaises(QuoteFeedError):
            self.fetch(now=850)
        data = fixture()
        data["chart"]["result"][0]["indicators"]["quote"][0]["close"] = [0.0001] * 4
        with self.assertRaises(QuoteFeedError):
            self.fetch(data)

    def test_malformed_and_nonfinite_json_rejected(self):
        for raw in [b"<html>Unavailable</html>", b"{", b'{"price": NaN}', b'{"price": Infinity}', b"\xff"]:
            with self.subTest(raw=raw), self.assertRaises(QuoteFeedError):
                self.fetch(raw=raw)

    def test_request_uses_public_fixed_host_and_timeout(self):
        with patch("live.feeds.urlopen", return_value=io.BytesIO(json.dumps(fixture()).encode())) as mocked, patch("live.feeds.time.time", return_value=NOW):
            YahooQuoteFeed("RELIANCE.NS", timeout_s=7).fetch()
        request = mocked.call_args.args[0]
        self.assertEqual(request.full_url, "https://query1.finance.yahoo.com/v8/finance/chart/RELIANCE.NS?interval=1m&range=1d&includePrePost=false")
        self.assertEqual(request.get_header("User-agent"), "Mozilla/5.0")
        self.assertEqual(mocked.call_args.kwargs["timeout"], 7)

    def test_http_network_and_timeout_failures_are_reported(self):
        errors = [HTTPError("https://query1.finance.yahoo.com", 429, "Limited", {}, None), URLError("DNS"), TimeoutError("slow")]
        for error in errors:
            with self.subTest(error=error), patch("live.feeds.urlopen", side_effect=error), self.assertRaises(QuoteFeedError):
                YahooQuoteFeed("RELIANCE.NS").fetch()

    def test_oversized_response_rejected(self):
        with self.assertRaises(QuoteFeedError):
            self.fetch(raw=b" " * (YahooQuoteFeed.max_response_bytes + 1))

    def test_extreme_decimal_exponents_fail_closed(self):
        body = json.dumps(fixture()).encode()
        for value in [b"1e999999999", b"1e9999999999999999999999999999"]:
            with self.subTest(value=value), self.assertRaises(QuoteFeedError):
                self.fetch(raw=body.replace(b"100.125", value))


if __name__ == "__main__":
    unittest.main()
