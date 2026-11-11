"""全市场成交额「同时刻」对比 (看板「成交额」卡片: 今日 vs 前一交易日)。

盘中今日成交额只是截至当前的累计值, 直接和前一交易日全天比会恒显示"缩量"。
本模块按时间对齐: 取前一交易日**同一分钟**的累计成交额作基准。

基准来源 (按优先级, 取不到就降级, 不猜):
1. minute   前一交易日 kline_minute 分区 (全量分钟能力) 聚合单根 amount 到 cutoff。
            分区可能只含个别标的 (如用户点开过某只股票的分时), 所以必须过覆盖率校验。
2. recorded 本服务自己在每轮实时行情刷新时记录的盘中累计曲线
            (data/intraday_amount/YYYY-MM-DD.json), 不依赖付费分钟线; 记录开始后的
            下一个交易日起可用。
3. 都没有   只返回前一交易日全天额 (prev_full), 不给涨跌幅 —— 拿全天额算盘中涨跌是误导。

收盘后 / 历史日期 (as_of 不是"进行中的今天") 直接对比前一交易日全天, 数据来自日线分区。

口径: 金额单位均为元; change_ratio 为小数 (0.123 = +12.3%), 前端自行 ×100 展示。
对齐精度 1 分钟 (分钟线 K 的时间戳约定与快照时刻之间的差异可忽略)。
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any

import polars as pl

from app.market_time import cn_now

logger = logging.getLogger(__name__)

CURVE_DIRNAME = "intraday_amount"

_SESSION_START = dt_time(9, 30)
_MORNING_END = dt_time(11, 30)
_AFTERNOON_START = dt_time(13, 0)
_CLOSE = dt_time(15, 0)
# 记录窗口: 含集合竞价 (9:15) 与收盘后少量延迟快照 (15:05)
_RECORD_FROM = "09:15"
_RECORD_TO = "15:05"
# 曲线是稀疏采样 (轮询间隔 ≥ 数秒): 允许 cutoff 之前最近一个采样点至多落后这么多交易分钟
_MAX_GAP_TRADING_MIN = 5
# 分钟线分区至少覆盖昨日有成交股票数的这个比例才认为是全市场数据
_MIN_COVERAGE = 0.9

_lock = threading.Lock()
# (data_dir, 交易日) → {"HH:MM": 累计成交额}。仅今日曲线会被反复写, 历史曲线只读。
_curves: dict[tuple[str, str], dict[str, float]] = {}
# 小型结果缓存: 看板在每轮行情刷新 (几秒) 都会重算, 而前一交易日的聚合值按分钟才变
_result_cache: dict[tuple, Any] = {}
_RESULT_CACHE_MAX = 16


def _empty_result() -> dict[str, Any]:
    return {
        "prev_date": None,
        "prev_full": None,
        "prev_total": None,
        "aligned_at": None,
        "source": None,
        "change_ratio": None,
    }


# ------------------------------------------------------------------
# 盘中累计曲线: 记录 / 读取
# ------------------------------------------------------------------

def _curve_path(data_dir: Path, day: date) -> Path:
    return Path(data_dir) / CURVE_DIRNAME / f"{day.isoformat()}.json"


def _read_curve_file(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, (int, float)) and v == v and v > 0:
            out[k] = float(v)
    return out


def load_curve(data_dir: Path, day: date) -> dict[str, float]:
    """读取某交易日记录的盘中累计成交额曲线 (不存在返回空)。"""
    key = (str(data_dir), day.isoformat())
    with _lock:
        cached = _curves.get(key)
        if cached is not None:
            return dict(cached)
    curve = _read_curve_file(_curve_path(data_dir, day))
    # 仅缓存非空曲线: 空结果可能只是"还没记录", 不能把它固化
    if curve:
        with _lock:
            _curves.setdefault(key, dict(curve))
    return curve


def record_total(data_dir: Path, day: date, hhmm: str, total: float) -> bool:
    """记录 day 当日 hhmm (北京时间) 时刻的全市场累计成交额。

    累计额不会回落: 小于已记录最大值的采样视为部分/脏快照丢弃 (fail-closed)。
    返回是否写入。落盘失败只留日志, 不影响行情主流程。
    """
    if not (_RECORD_FROM <= hhmm <= _RECORD_TO):
        return False
    if not isinstance(total, (int, float)) or total != total or total <= 0:
        return False
    key = (str(data_dir), day.isoformat())
    path = _curve_path(data_dir, day)
    with _lock:
        curve = _curves.get(key)
        if curve is None:
            curve = _read_curve_file(path)
            _curves[key] = curve
        if curve and total < max(curve.values()):
            return False
        curve[hhmm] = float(total)
        payload = json.dumps(dict(sorted(curve.items())), ensure_ascii=False)
        # 只保留最近几个交易日的内存曲线, 避免长跑进程无限增长
        if len(_curves) > 6:
            for stale in sorted(_curves, key=lambda k: k[1])[:-6]:
                _curves.pop(stale, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        logger.warning("盘中成交额曲线写盘失败: %s", e)
    return True


def _trade_minute(hhmm: str) -> int:
    """HH:MM → 开盘以来的交易分钟序号 (午休折叠, 开盘前记 0)。"""
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    minutes = h * 60 + m
    if minutes <= 9 * 60 + 30:
        return 0
    if minutes <= 11 * 60 + 30:
        return minutes - (9 * 60 + 30)
    if minutes < 13 * 60:
        return 120
    return min(240, 120 + minutes - 13 * 60)


def _curve_cum_at(curve: dict[str, float], cutoff: str) -> float | None:
    """曲线在 cutoff 时刻的累计额: 取 ≤ cutoff 的最近采样点, 落后过多视为缺口。"""
    keys = [k for k in curve if k <= cutoff]
    if not keys:
        return None
    nearest = max(keys)
    if _trade_minute(cutoff) - _trade_minute(nearest) > _MAX_GAP_TRADING_MIN:
        return None
    return curve[nearest]


# ------------------------------------------------------------------
# 前一交易日基准
# ------------------------------------------------------------------

def _cached(key: tuple, compute):
    with _lock:
        if key in _result_cache:
            return _result_cache[key]
    value = compute()
    with _lock:
        if len(_result_cache) >= _RESULT_CACHE_MAX:
            _result_cache.clear()
        _result_cache[key] = value
    return value


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def _day_total(data_dir: Path, enriched_dirname: str, day: date) -> tuple[float, int] | None:
    """某交易日全市场股票成交额合计 (元) 与有成交股票数; 取不到返回 None。"""
    candidates = [
        Path(data_dir) / enriched_dirname / f"date={day.isoformat()}" / "part.parquet",
        Path(data_dir) / "kline_daily" / f"date={day.isoformat()}" / "part.parquet",
    ]
    for path in candidates:
        mt = _mtime(path)
        if mt < 0:
            continue

        def compute(path=path):
            try:
                lf = pl.scan_parquet(path)
                if "amount" not in lf.collect_schema().names():
                    return None
                row = (
                    lf.select(
                        pl.col("amount").cast(pl.Float64, strict=False).alias("a")
                    )
                    .filter(pl.col("a") > 0)
                    .select(pl.col("a").sum().alias("total"), pl.len().alias("n"))
                    .collect()
                )
            except Exception as e:  # noqa: BLE001
                logger.warning("前一交易日成交额读取失败 %s: %s", path, e)
                return None
            total, n = row["total"][0], row["n"][0]
            if total is None or n == 0:
                return None
            return float(total), int(n)

        res = _cached(("day_total", str(path), mt), compute)
        if res is not None:
            return res
    return None


def _minute_cum(data_dir: Path, day: date, cutoff: dt_time, expected_symbols: int) -> float | None:
    """前一交易日分钟线聚合到 cutoff 的全市场累计成交额; 覆盖不足返回 None。"""
    path = Path(data_dir) / "kline_minute" / f"date={day.isoformat()}" / "part.parquet"
    mt = _mtime(path)
    if mt < 0 or expected_symbols <= 0:
        return None

    def compute():
        try:
            lf = pl.scan_parquet(path)
            names = lf.collect_schema().names()
            if not {"symbol", "datetime", "amount"}.issubset(names):
                return None
            upto = datetime.combine(day, cutoff)
            row = (
                lf.filter(pl.col("datetime") <= upto)
                .select(
                    pl.col("amount").cast(pl.Float64, strict=False).sum().alias("total"),
                    pl.col("symbol").n_unique().alias("n"),
                    pl.col("datetime").max().alias("last"),
                )
                .collect()
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("前一交易日分钟线聚合失败 %s: %s", path, e)
            return None
        total, n, last = row["total"][0], row["n"][0], row["last"][0]
        if not total or total <= 0 or last is None:
            return None
        # 分区里个别标的 (如点开过单只股票分时) 会让合计严重偏小 → 覆盖率校验
        if n < expected_symbols * _MIN_COVERAGE:
            return None
        # 数据必须真的走到 cutoff 附近, 否则是半天数据
        last_hhmm = last.strftime("%H:%M")
        if _trade_minute(cutoff.strftime("%H:%M")) - _trade_minute(last_hhmm) > _MAX_GAP_TRADING_MIN:
            return None
        return float(total)

    return _cached(("minute", str(path), mt, cutoff.strftime("%H:%M"), expected_symbols), compute)


# ------------------------------------------------------------------
# 对齐时刻
# ------------------------------------------------------------------

_SKIP = object()


def _alignment_cutoff(data_dir: Path, as_of: date, now: datetime) -> dt_time | object | None:
    """对齐时刻: None=全天对比; _SKIP=盘前无可比数据; dt_time=盘中截至该分钟。"""
    if as_of != now.date():
        return None  # 历史日期 / 非交易日回退到上一交易日: 数据已是全天
    t = now.time()
    if t >= _CLOSE:
        return None
    if t < _SESSION_START:
        return _SKIP
    if _MORNING_END < t < _AFTERNOON_START:
        t = _MORNING_END
    cutoff = t.replace(second=0, microsecond=0)
    # 实时行情若中途停了, 今日累计额停在最后一次记录; 用记录时刻而不是墙钟对齐
    today_curve = load_curve(data_dir, as_of)
    if today_curve:
        last = max(today_curve)
        if last < cutoff.strftime("%H:%M"):
            cutoff = dt_time(int(last[:2]), int(last[3:5]))
    return cutoff


def compare_amount(svc, as_of: date, today_total: float, now: datetime | None = None) -> dict[str, Any]:
    """今日成交额 (today_total, 元) 与前一交易日同时刻对比。任何异常都降级为空结果。"""
    out = _empty_result()
    try:
        data_dir = Path(svc.repo.store.data_dir)
        prevs = svc._prior_partition_dates(as_of, 1)
        if not prevs:
            return out
        prev = prevs[0]
        out["prev_date"] = prev.isoformat()

        full = _day_total(data_dir, svc._enriched_dirname, prev)
        if full is not None:
            out["prev_full"] = full[0]

        cutoff = _alignment_cutoff(data_dir, as_of, now or cn_now())
        prev_total: float | None = None
        if cutoff is None:
            if full is not None:
                prev_total, out["source"] = full[0], "daily"
        elif cutoff is not _SKIP:
            hhmm = cutoff.strftime("%H:%M")
            if full is not None:
                prev_total = _minute_cum(data_dir, prev, cutoff, full[1])
                if prev_total is not None:
                    out["source"] = "minute"
            if prev_total is None:
                prev_total = _curve_cum_at(load_curve(data_dir, prev), hhmm)
                if prev_total is not None:
                    out["source"] = "recorded"
            if prev_total is not None:
                out["aligned_at"] = hhmm

        if prev_total and prev_total > 0 and today_total and today_total > 0:
            out["prev_total"] = prev_total
            out["change_ratio"] = today_total / prev_total - 1.0
        else:
            out["source"] = None
            out["aligned_at"] = None
    except Exception as e:  # noqa: BLE001
        logger.warning("成交额同时刻对比失败 (不影响看板其余部分): %s", e)
        return _empty_result()
    return out
