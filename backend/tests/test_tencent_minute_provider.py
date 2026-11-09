from datetime import datetime

import polars as pl

from app.data_providers.tencent_provider import parse_response
from app.services import kline_sync
from app.tickflow.capabilities import CapabilitySet


def test_parse_tencent_minute_response_to_canonical_rows():
    payload = '{"data":{"sz002384":{"m1":[["2026-11-09 09:30","82.10","82.20","82.30","82.00","1234","101234.5"]]}}}'

    frame = parse_response(payload, "002384.SZ")

    assert frame.columns == ["symbol", "datetime", "open", "close", "high", "low", "volume", "amount"]
    assert frame.height == 1
    assert frame["symbol"][0] == "002384.SZ"
    assert str(frame["datetime"][0]) == "2026-11-09 09:30:00"
    assert frame["close"][0] == 82.2


def test_parse_tencent_minute_response_filters_requested_window():
    payload = '{"data":{"sh600519":{"m1":[["202611090930","1","1","1","1","1"],["202611090931","2","2","2","2","2"]]}}}'

    frame = parse_response(payload, "600519.SH", start_time=datetime(2026, 11, 9, 9, 31))

    assert frame.height == 1
    assert frame["datetime"][0].hour == 9
    assert frame["datetime"][0].minute == 31


def test_single_minute_without_tickflow_capability_uses_tencent(monkeypatch):
    expected = pl.DataFrame({
        "symbol": ["002384.SZ"],
        "datetime": [datetime(2026, 11, 9, 9, 30)],
        "open": [82.1], "close": [82.2], "high": [82.3], "low": [82.0],
        "volume": [1234.0], "amount": [101234.5],
    })
    monkeypatch.setattr(kline_sync, "_try_custom_minute", lambda *args, **kwargs: (None, True))
    monkeypatch.setattr("app.data_providers.tencent_provider.get_minute", lambda *args, **kwargs: expected)

    result = kline_sync.fetch_minute_single("002384.SZ", datetime(2026, 11, 9).date(), capset=CapabilitySet())

    assert result.height == 1
    assert result["symbol"][0] == "002384.SZ"
