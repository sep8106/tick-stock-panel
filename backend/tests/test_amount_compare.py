"""看板成交额「同时刻」对比: 基准来源优先级、覆盖率校验、降级与边界。"""
from __future__ import annotations

import types
from datetime import date, datetime

import polars as pl
import pytest

from app.market_time import CN_TZ
from app.services import amount_compare as ac

PREV = date(2026, 11, 10)
TODAY = date(2026, 11, 11)


@pytest.fixture(autouse=True)
def _clean_state():
    ac._curves.clear()
    ac._result_cache.clear()
    yield
    ac._curves.clear()
    ac._result_cache.clear()


def _svc(tmp_path):
    return types.SimpleNamespace(
        repo=types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path)),
        _enriched_dirname="kline_daily_enriched",
        _prior_partition_dates=lambda as_of, limit: [PREV] if as_of > PREV else [],
    )


def _at(day: date, hh: int, mm: int) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=CN_TZ)


def _write_daily(tmp_path, day: date, amounts: list[float]):
    d = tmp_path / "kline_daily_enriched" / f"date={day.isoformat()}"
    d.mkdir(parents=True)
    pl.DataFrame({
        "symbol": [f"{i:06d}.SZ" for i in range(len(amounts))],
        "amount": amounts,
    }).write_parquet(d / "part.parquet")


def _write_minute(tmp_path, day: date, n_symbols: int, last_hhmm: tuple[int, int] = (15, 0)):
    """每只股票每分钟成交 1 万元, 09:31 起到 last (午休跳过)。"""
    rows = []
    minutes = [(9, m) for m in range(31, 60)] + [(10, m) for m in range(60)] + [(11, m) for m in range(0, 31)]
    minutes += [(13, m) for m in range(1, 60)] + [(14, m) for m in range(60)] + [(15, 0)]
    minutes = [t for t in minutes if t <= last_hhmm]
    for s in range(n_symbols):
        for h, m in minutes:
            rows.append((f"{s:06d}.SZ", datetime(day.year, day.month, day.day, h, m), 10_000.0))
    d = tmp_path / "kline_minute" / f"date={day.isoformat()}"
    d.mkdir(parents=True)
    pl.DataFrame(rows, schema=["symbol", "datetime", "amount"], orient="row").write_parquet(d / "part.parquet")


# ---------------- 全天对比 (盘后 / 历史日期) ----------------

def test_full_day_compare_after_close(tmp_path):
    _write_daily(tmp_path, PREV, [1e8, 3e8])  # 昨日合计 4 亿
    out = ac.compare_amount(_svc(tmp_path), TODAY, 5e8, now=_at(TODAY, 15, 30))
    assert out["source"] == "daily"
    assert out["aligned_at"] is None
    assert out["prev_total"] == pytest.approx(4e8)
    assert out["change_ratio"] == pytest.approx(0.25)
    assert out["prev_date"] == "2026-11-10"


def test_historical_as_of_uses_full_day_even_if_clock_is_intraday(tmp_path):
    """as_of 不是今天 (历史复盘 / 假日回退): 即便现在是盘中也不做时间对齐。"""
    _write_daily(tmp_path, PREV, [4e8])
    out = ac.compare_amount(_svc(tmp_path), TODAY, 2e8, now=_at(date(2026, 11, 12), 10, 0))
    assert out["source"] == "daily"
    assert out["change_ratio"] == pytest.approx(-0.5)


def test_zero_amount_rows_excluded_from_prev_full(tmp_path):
    _write_daily(tmp_path, PREV, [0.0, 0.0, 4e8])
    out = ac.compare_amount(_svc(tmp_path), TODAY, 4e8, now=_at(TODAY, 16, 0))
    assert out["prev_total"] == pytest.approx(4e8)
    assert out["change_ratio"] == pytest.approx(0.0)


def test_no_prior_trading_day_returns_empty(tmp_path):
    out = ac.compare_amount(_svc(tmp_path), PREV, 1e8, now=_at(PREV, 16, 0))
    assert out["change_ratio"] is None and out["prev_date"] is None and out["prev_full"] is None


def test_missing_prev_daily_data_does_not_raise(tmp_path):
    out = ac.compare_amount(_svc(tmp_path), TODAY, 1e8, now=_at(TODAY, 16, 0))
    assert out["prev_date"] == "2026-11-10"
    assert out["change_ratio"] is None and out["prev_full"] is None


# ---------------- 盘中: 分钟线来源 ----------------

def test_intraday_aligned_with_minute_data(tmp_path):
    _write_daily(tmp_path, PREV, [1e9] * 10)
    _write_minute(tmp_path, PREV, n_symbols=10)
    # 10:00 之前 (含 10:00): 09:31..09:59 共 29 根 + 10:00 一根 = 30 根; 每根 10 只 × 1 万
    expected = 30 * 10 * 10_000.0
    out = ac.compare_amount(_svc(tmp_path), TODAY, expected * 1.5, now=_at(TODAY, 10, 0))
    assert out["source"] == "minute"
    assert out["aligned_at"] == "10:00"
    assert out["prev_total"] == pytest.approx(expected)
    assert out["change_ratio"] == pytest.approx(0.5)
    # 全天额仍单独返回, 但不参与盘中涨跌计算
    assert out["prev_full"] == pytest.approx(1e10)


def test_lunch_break_aligns_to_morning_close(tmp_path):
    _write_daily(tmp_path, PREV, [1e9] * 4)
    _write_minute(tmp_path, PREV, n_symbols=4)
    out = ac.compare_amount(_svc(tmp_path), TODAY, 1e7, now=_at(TODAY, 12, 15))
    assert out["aligned_at"] == "11:30"
    assert out["prev_total"] == pytest.approx(120 * 4 * 10_000.0)  # 上午 120 根


def test_partial_minute_partition_rejected_by_coverage(tmp_path):
    """分区只有 1/10 的股票 (如用户点开过单只股票分时) 不能当全市场数据。"""
    _write_daily(tmp_path, PREV, [1e9] * 10)
    _write_minute(tmp_path, PREV, n_symbols=1)
    out = ac.compare_amount(_svc(tmp_path), TODAY, 1e8, now=_at(TODAY, 10, 0))
    assert out["source"] is None
    assert out["change_ratio"] is None
    assert out["prev_full"] == pytest.approx(1e10)  # 降级只给全天额


def test_minute_partition_that_ends_early_rejected(tmp_path):
    _write_daily(tmp_path, PREV, [1e9] * 10)
    _write_minute(tmp_path, PREV, n_symbols=10, last_hhmm=(9, 45))
    out = ac.compare_amount(_svc(tmp_path), TODAY, 1e8, now=_at(TODAY, 14, 0))
    assert out["change_ratio"] is None


# ---------------- 盘中: 自记曲线回退 ----------------

def test_recorded_curve_used_when_no_minute_data(tmp_path):
    _write_daily(tmp_path, PREV, [1e9] * 10)
    ac.record_total(tmp_path, PREV, "09:59", 8e9)
    ac.record_total(tmp_path, PREV, "10:00", 1e10)
    ac.record_total(tmp_path, PREV, "10:01", 1.1e10)
    out = ac.compare_amount(_svc(tmp_path), TODAY, 5e9, now=_at(TODAY, 10, 0))
    assert out["source"] == "recorded"
    assert out["prev_total"] == pytest.approx(1e10)  # 不能偷看 10:01 的值
    assert out["change_ratio"] == pytest.approx(-0.5)


def test_recorded_curve_gap_too_large_means_not_comparable(tmp_path):
    _write_daily(tmp_path, PREV, [1e9] * 10)
    ac.record_total(tmp_path, PREV, "09:40", 1e9)  # 距 10:00 有 20 分钟缺口
    out = ac.compare_amount(_svc(tmp_path), TODAY, 5e9, now=_at(TODAY, 10, 0))
    assert out["change_ratio"] is None and out["source"] is None and out["aligned_at"] is None
    assert out["prev_full"] == pytest.approx(1e10)


def test_curve_gap_is_measured_in_trading_minutes_across_lunch(tmp_path):
    _write_daily(tmp_path, PREV, [1e9] * 10)
    ac.record_total(tmp_path, PREV, "11:30", 6e9)
    # 13:02 距上午收盘只有 2 个交易分钟
    out = ac.compare_amount(_svc(tmp_path), TODAY, 6e9, now=_at(TODAY, 13, 2))
    assert out["source"] == "recorded"
    assert out["prev_total"] == pytest.approx(6e9)


def test_cutoff_follows_last_recorded_minute_when_realtime_stalled(tmp_path):
    """实时行情中途停了: 今日累计额停在 10:00, 不能拿 14:00 的昨日额去比。"""
    _write_daily(tmp_path, PREV, [1e9] * 10)
    ac.record_total(tmp_path, TODAY, "10:00", 5e9)
    ac.record_total(tmp_path, PREV, "10:00", 1e10)
    ac.record_total(tmp_path, PREV, "14:00", 9e10)
    out = ac.compare_amount(_svc(tmp_path), TODAY, 5e9, now=_at(TODAY, 14, 0))
    assert out["aligned_at"] == "10:00"
    assert out["prev_total"] == pytest.approx(1e10)


def test_pre_open_has_no_comparison(tmp_path):
    _write_daily(tmp_path, PREV, [1e9])
    out = ac.compare_amount(_svc(tmp_path), TODAY, 1e6, now=_at(TODAY, 9, 20))
    assert out["change_ratio"] is None
    assert out["prev_full"] == pytest.approx(1e9)


# ---------------- 记录曲线 ----------------

def test_record_persists_and_reloads(tmp_path):
    assert ac.record_total(tmp_path, TODAY, "10:00", 1e9)
    ac._curves.clear()  # 模拟进程重启
    assert ac.load_curve(tmp_path, TODAY) == {"10:00": 1e9}


def test_record_rejects_decreasing_total(tmp_path):
    assert ac.record_total(tmp_path, TODAY, "10:00", 2e9)
    assert not ac.record_total(tmp_path, TODAY, "10:01", 1e9)  # 累计额不会回落 → 脏/部分快照
    assert ac.load_curve(tmp_path, TODAY) == {"10:00": 2e9}


def test_record_overwrites_same_minute_with_latest(tmp_path):
    ac.record_total(tmp_path, TODAY, "10:00", 1e9)
    ac.record_total(tmp_path, TODAY, "10:00", 1.2e9)
    assert ac.load_curve(tmp_path, TODAY) == {"10:00": 1.2e9}


@pytest.mark.parametrize("hhmm", ["08:00", "15:30", "23:59"])
def test_record_outside_window_ignored(tmp_path, hhmm):
    assert not ac.record_total(tmp_path, TODAY, hhmm, 1e9)
    assert ac.load_curve(tmp_path, TODAY) == {}


@pytest.mark.parametrize("bad", [0, -1.0, float("nan"), None])
def test_record_rejects_invalid_totals(tmp_path, bad):
    assert not ac.record_total(tmp_path, TODAY, "10:00", bad)


def test_corrupt_curve_file_is_ignored(tmp_path):
    p = ac._curve_path(tmp_path, TODAY)
    p.parent.mkdir(parents=True)
    p.write_text("{not json", encoding="utf-8")
    assert ac.load_curve(tmp_path, TODAY) == {}
    assert ac.record_total(tmp_path, TODAY, "10:00", 1e9)  # 坏文件不阻断后续记录


# ---------------- quote_service 记录钩子 ----------------

def test_quote_service_hook_records_stock_total(tmp_path, monkeypatch):
    from app.services import quote_service as qs

    monkeypatch.setattr(qs, "cn_today", lambda: TODAY)
    ts = int(_at(TODAY, 10, 0).timestamp() * 1000)
    fake = types.SimpleNamespace(_repo=types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path)))
    records = [
        {"symbol": "000001.SZ", "amount": 3e8, "timestamp": ts},
        {"symbol": "000002.SZ", "amount": None, "timestamp": ts - 1000},
        {"symbol": "000003.SZ", "amount": 2e8, "timestamp": ts},
    ]
    qs.QuoteService._record_intraday_amount(fake, records)
    assert ac.load_curve(tmp_path, TODAY) == {"10:00": 5e8}


def test_quote_service_hook_skips_stale_previous_day_snapshot(tmp_path, monkeypatch):
    from app.services import quote_service as qs

    monkeypatch.setattr(qs, "cn_today", lambda: TODAY)
    ts = int(_at(PREV, 15, 0).timestamp() * 1000)  # 源端仍返回昨日快照
    fake = types.SimpleNamespace(_repo=types.SimpleNamespace(store=types.SimpleNamespace(data_dir=tmp_path)))
    qs.QuoteService._record_intraday_amount(fake, [{"symbol": "000001.SZ", "amount": 9e8, "timestamp": ts}])
    assert ac.load_curve(tmp_path, TODAY) == {}
    assert ac.load_curve(tmp_path, PREV) == {}
