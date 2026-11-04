"""Public delayed/indicative practice quotes; no broker account or order route.

Yahoo's undocumented chart endpoint can change or become unavailable. Prices
are completed one-minute closes, not real-time executable quotes or depth.
"""

from dataclasses import dataclass
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
import json
import math
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote as url_quote
from urllib.request import Request, urlopen


class QuoteFeedError(RuntimeError):
    """The public feed could not provide a validated completed INR bar."""


class QuoteFeedUnavailable(QuoteFeedError):
    """A transport/server failure eligible for bounded retries."""

    def __init__(self, message: str, *, retry_after_s=None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class Quote:
    symbol: str
    price_paise: int
    source_time_s: int
    currency: str = "INR"
    market_open: bool = False
    delayed: bool = True
    source: str = "Yahoo Finance chart (delayed/indicative)"


class YahooQuoteFeed:
    """Return completed INR bars, including duplicates for caller deduplication.

    `market_open` describes the published regular-session window. It does not
    guarantee that the symbol is tradeable or that the returned bar is current.
    """

    interval_s = 60
    max_response_bytes = 2_000_000
    status = "delayed/indicative"
    _symbol_pattern = re.compile(r"[A-Z0-9][A-Z0-9&_\-]{0,49}\.(?:NS|BO)\Z")

    def __init__(self, symbol: str, *, timeout_s: float = 10.0):
        if not isinstance(symbol, str):
            raise ValueError("Symbol must be an NSE .NS or BSE .BO symbol")
        self.symbol = symbol.strip().upper()
        if self._symbol_pattern.fullmatch(self.symbol) is None:
            raise ValueError("Only NSE .NS and BSE .BO equity symbols are supported")
        if (isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float))
                or not math.isfinite(timeout_s) or not 0 < timeout_s <= 30):
            raise ValueError("Quote timeout must be greater than zero and at most 30 seconds")
        self.timeout_s = timeout_s

    def fetch(self) -> Quote:
        endpoint = ("https://query1.finance.yahoo.com/v8/finance/chart/"
                    + url_quote(self.symbol, safe="")
                    + "?interval=1m&range=1d&includePrePost=false")
        request = Request(endpoint, headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                body = response.read(self.max_response_bytes + 1)
        except HTTPError as error:
            message = f"Yahoo quote feed unavailable (HTTP {error.code}); no quote accepted"
            if error.code in (408, 429, 500, 502, 503, 504):
                retry_after = self._retry_after(error.headers.get('Retry-After') if error.headers else None)
                raise QuoteFeedUnavailable(message, retry_after_s=retry_after) from error
            raise QuoteFeedError(message) from error
        except (URLError, TimeoutError, OSError) as error:
            raise QuoteFeedUnavailable("Yahoo quote feed unavailable; no quote accepted") from error
        if len(body) > self.max_response_bytes:
            raise QuoteFeedError("Yahoo quote response exceeds the allowed size")
        try:
            payload = json.loads(body, parse_float=Decimal, parse_constant=self._invalid_constant)
        except (ValueError, UnicodeError, RecursionError, DecimalException) as error:
            raise QuoteFeedError("Yahoo quote response is not valid finite JSON") from error
        return self._parse(payload, int(time.time()))  # UTC epoch, independent of host timezone.

    @staticmethod
    def _invalid_constant(value: str):
        raise ValueError(f"Invalid JSON constant: {value}")

    @staticmethod
    def _retry_after(value):
        if value is None:
            return None
        if value.strip().isdigit():
            return int(value.strip())
        try:
            parsed = parsedate_to_datetime(value)
            return max(0.0, parsed.timestamp() - time.time()) if parsed.tzinfo is not None else None
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _timestamp(value, label: str) -> int:
        if type(value) is not int or value < 0:
            raise QuoteFeedError(f"Yahoo {label} must be a nonnegative integer timestamp")
        return value

    def _parse(self, payload, now_s: int) -> Quote:
        if not isinstance(payload, dict) or not isinstance(payload.get("chart"), dict):
            raise QuoteFeedError("Yahoo response is missing the chart object")
        chart = payload["chart"]
        if chart.get("error") is not None:
            raise QuoteFeedError("Yahoo returned a chart error; no quote accepted")
        results = chart.get("result")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise QuoteFeedError("Yahoo response must contain exactly one chart result")
        result = results[0]
        meta = result.get("meta")
        if not isinstance(meta, dict) or meta.get("symbol") != self.symbol:
            raise QuoteFeedError("Yahoo returned missing or mismatched symbol metadata")
        if meta.get("currency") != "INR":
            raise QuoteFeedError("Yahoo quote currency must be INR")
        if meta.get("exchangeTimezoneName") not in ("Asia/Kolkata", "Asia/Calcutta"):
            raise QuoteFeedError("Yahoo returned an unexpected Indian exchange timezone")
        periods = meta.get("currentTradingPeriod")
        regular = periods.get("regular") if isinstance(periods, dict) else None
        if (not isinstance(regular, dict) or type(regular.get("gmtoffset")) is not int
                or regular["gmtoffset"] != 19_800):
            raise QuoteFeedError("Yahoo regular-session metadata is missing or invalid")
        start = self._timestamp(regular.get("start"), "session start")
        end = self._timestamp(regular.get("end"), "session end")
        if start >= end:
            raise QuoteFeedError("Yahoo regular-session start must precede its end")
        market_open = start <= now_s < end

        timestamps = result.get("timestamp")
        indicators = result.get("indicators")
        quotes = indicators.get("quote") if isinstance(indicators, dict) else None
        if not isinstance(quotes, list) or len(quotes) != 1 or not isinstance(quotes[0], dict):
            raise QuoteFeedError("Yahoo response is missing one matching quote array")
        closes = quotes[0].get("close")
        if (not isinstance(timestamps, list) or not timestamps or not isinstance(closes, list)
                or len(timestamps) != len(closes)):
            raise QuoteFeedError("Yahoo timestamp and close arrays must be nonempty and equal in length")
        selected = None
        previous_time = -1
        for raw_time, raw_price in zip(timestamps, closes):
            source_time = self._timestamp(raw_time, "bar time")
            if source_time > now_s + 5:
                raise QuoteFeedError("Yahoo bar timestamp is in the future")
            if source_time <= previous_time:
                raise QuoteFeedError("Yahoo bar timestamps must strictly increase")
            previous_time = source_time
            if raw_price is None:
                continue
            if not isinstance(raw_price, Decimal) and type(raw_price) is not int:
                raise QuoteFeedError("Yahoo close prices must be finite numeric values")
            price = Decimal(raw_price)
            if not price.is_finite() or not 0 < price <= 1_000_000:
                raise QuoteFeedError("Yahoo close prices must be positive, finite and within native bounds")
            # Chart timestamps are bar starts; exclude the still-forming minute.
            if source_time + self.interval_s <= now_s:
                selected = source_time, price
        if selected is None:
            raise QuoteFeedError("Yahoo supplied no completed non-null one-minute close")
        source_time, price = selected
        if market_open and not start <= source_time < source_time + self.interval_s <= end:
            raise QuoteFeedError("Yahoo completed bar is outside the published regular session")
        try:
            paise = int((price * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        except (DecimalException, OverflowError, ValueError) as error:
            raise QuoteFeedError("Yahoo close cannot be represented as integer paise") from error
        if paise <= 0:
            raise QuoteFeedError("Yahoo close rounds to a nonpositive paise value")
        return Quote(self.symbol, paise, source_time, "INR", market_open)
