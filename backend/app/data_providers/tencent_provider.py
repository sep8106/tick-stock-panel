"""Tencent public market-data fallback for recent A-share minute bars.

The endpoint is anonymous and intentionally used only after the configured
minute provider cannot return rows.  It is a best-effort fallback: Tencent's
public endpoint has a short rolling history and is not suitable for backtests.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from urllib.parse import quote
from urllib.request import Request, urlopen

import polars as pl

from app.data_providers.minute_normalizer import _enforce_minute_beijing_wallclock

logger = logging.getLogger(__name__)

_BASE_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/kline/mkline"
_DEFAULT_COUNT = 1024


def _market_symbol(symbol: str) -> str:
    code, _, exchange = str(symbol).upper().partition(".")
    exchange = exchange or ("SH" if code.startswith(("5", "6", "9")) else "SZ")
    if exchange not in {"SH", "SZ", "BJ"}:
        raise ValueError(f"unsupported Tencent exchange: {exchange}")
    return f"{exchange.lower()}{code}"


def _number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_response(payload: str, symbol: str, *, start_time: datetime | None = None,
                  end_time: datetime | None = None) -> pl.DataFrame:
    """Parse Tencent ``m1`` rows into the application's canonical minute schema."""
    body = json.loads(payload)
    data = body.get("data") if isinstance(body, dict) else None
    item = data.get(_market_symbol(symbol)) if isinstance(data, dict) else None
    if item is None and isinstance(data, dict):
        item = next((v for k, v in data.items() if str(k).lower() == _market_symbol(symbol)), None)
    rows = item.get("m1", []) if isinstance(item, dict) else []
    normalized: list[dict] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        raw_time = str(row[0])
        timestamp = None
        for fmt, width in (("%Y-%m-%d %H:%M", 16), ("%Y%m%d%H%M", 12)):
            try:
                timestamp = datetime.strptime(raw_time[:width], fmt)
                break
            except ValueError:
                continue
        if timestamp is None:
            continue
        if start_time is not None and timestamp < start_time.replace(tzinfo=None):
            continue
        if end_time is not None and timestamp > end_time.replace(tzinfo=None):
            continue
        normalized.append({
            "symbol": symbol,
            "datetime": timestamp,
            "open": _number(row[1]),
            "close": _number(row[2]),
            "high": _number(row[3]),
            "low": _number(row[4]),
            "volume": _number(row[5]),
            "amount": _number(row[6]) if len(row) > 6 else None,
        })
    if not normalized:
        return pl.DataFrame()
    frame = pl.DataFrame(normalized).sort("datetime")
    return _enforce_minute_beijing_wallclock(frame, source="tencent")


def get_minute(symbol: str, *, start_time: datetime | None = None,
               end_time: datetime | None = None, count: int = _DEFAULT_COUNT,
               timeout: float = 5.0) -> pl.DataFrame:
    """Fetch one symbol's recent 1-minute bars from Tencent."""
    market_symbol = _market_symbol(symbol)
    url = f"{_BASE_URL}?param={quote(market_symbol)},m1,,{int(count)}"
    request = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=timeout) as response:
        payload = response.read().decode("utf-8", errors="replace")
    return parse_response(payload, symbol, start_time=start_time, end_time=end_time)


def get_minute_batch(symbols: list[str], *, start_time: datetime | None = None,
                     end_time: datetime | None = None) -> pl.DataFrame:
    """Best-effort bounded batch wrapper; one failed symbol does not erase others."""
    frames: list[pl.DataFrame] = []
    for symbol in symbols:
        try:
            frame = get_minute(symbol, start_time=start_time, end_time=end_time)
        except Exception as exc:  # noqa: BLE001 - fallback must never break primary path
            logger.warning("Tencent minute fallback failed for %s: %s", symbol, exc)
            continue
        if not frame.is_empty():
            frames.append(frame)
    return pl.concat(frames, how="diagonal_relaxed") if frames else pl.DataFrame()
