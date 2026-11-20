"""价格指数计算。

规则：
- 基月指数 = 100。
- 月环比系数 = 相邻两月「同摊位同品种」报价比值（本月/上月）的等权平均。
- 相邻月没有可比报价对（或中间缺月）→ 该月环比为空，且自该月起指数断链留空。
- 指数 = 上月指数 × 本月环比系数。

精度与下溢：
- 环比系数与指数在计算、缓存阶段一律保留全精度，只在网页 / CSV 输出时按
  FACTOR_DISPLAY_DECIMALS / INDEX_DISPLAY_DECIMALS 舍入。
- 连乘在对数空间进行：index_log = 各月 ln(环比系数) 之和。即使某段链的指数
  小于浮点数能表示的最小正数（算术结果下溢为 0.00），对数链仍然有效，
  后续月份回升时 exp(index_log) 仍能还原正确指数，不会永久卡在 0。
- mom_factor 保存全精度算术平均（真正下溢时才可能为 0.0）；factor_log 保存
  缩放平均得到的对数值，是对数连乘的输入；index_value 仅为最佳努力的兼容值，
  index_log 才是指数的唯一真源。

只重算受影响的环比：某月被补报或更正后，该月与下一月的环比需要重算，
其余月份的环比沿用上一次结果；指数则从基月向后重新串联。
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

from . import db

BASE_INDEX = 100.0
BASE_INDEX_LOG = math.log(BASE_INDEX)

# 仅用于展示/导出，不参与任何计算
FACTOR_DISPLAY_DECIMALS = 4
INDEX_DISPLAY_DECIMALS = 2

# 缓存结构版本；旧库（无对数列 / 无版本号）启动时自动从报价重建
CACHE_VERSION = "2"


class RebuildError(Exception):
    """全量重建失败，可安全重试（旧缓存在失败时原样保留）。"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _log_mean(ratios: list[float]) -> float:
    """等权平均比值的自然对数，避免直接平均时下溢为 0。

    用 frexp 把所有比值的数量级提取出来再平均，指数部分单独累加，
    因此即使算术平均值已低于最小正浮点数也能得到有限对数值。
    """
    mantissas: list[float] = []
    exponent_sum = 0
    for value in ratios:
        mantissa, exponent = math.frexp(value)
        mantissas.append(mantissa)
        exponent_sum += exponent
    mean_mantissa = sum(mantissas) / len(mantissas)
    mean_exponent = exponent_sum / len(mantissas)
    # mean = mean_mantissa * 2**mean_exponent（mean_exponent 允许为分数）
    return math.log(mean_mantissa) + mean_exponent * math.log(2.0)


def pair_factor(
    current: dict[tuple[str, str], float],
    previous: dict[tuple[str, str], float],
) -> tuple[int, float | None, float | None]:
    """返回（可比报价对数，全精度环比系数，环比系数自然对数）。

    没有可比报价对时返回 (0, None, None)。
    """
    ratios = [
        current[key] / previous[key]
        for key in current.keys() & previous.keys()
        if previous[key] > 0
    ]
    if not ratios:
        return 0, None, None
    factor = sum(ratios) / len(ratios)
    if factor > 0.0:
        factor_log = math.log(factor)
    else:
        # 算术平均已下溢为 0：改用缩放计算，对数链依旧有效
        factor_log = _log_mean(ratios)
    return len(ratios), factor, factor_log


def _latest_upload_ids(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT month, MAX(id) AS upload_id FROM uploads GROUP BY month"
    ).fetchall()
    return {r["month"]: r["upload_id"] for r in rows}


def _factor_from_cache(prev: sqlite3.Row) -> tuple[int, float | None, float | None]:
    """沿用旧缓存中的环比；缺对数列的旧库行按 mom_factor 补算。"""
    factor = prev["mom_factor"]
    factor_log = prev["factor_log"] if "factor_log" in prev.keys() else None
    if factor_log is None and factor is not None and factor > 0.0:
        factor_log = math.log(factor)
    return int(prev["pair_count"] or 0), factor, factor_log


def recompute(
    conn: sqlite3.Connection,
    base_month: str,
    changed_month: str | None = None,
    latest_month: str | None = None,
) -> list[str]:
    """重算并写入 index_months，返回页面展示的月份序列。

    调用方负责事务。changed_month 为空表示全量重算。
    """
    latest = latest_month or db.max_uploaded_month(conn)
    if latest is None or latest < base_month:
        return []
    months = db.month_range(base_month, latest)
    month_set = set(months)

    existing = {
        r["month"]: dict(r)
        for r in conn.execute("SELECT * FROM index_months").fetchall()
    }
    upload_ids = _latest_upload_ids(conn)

    # 需要重算环比系数的月份
    if changed_month is None:
        factor_months = {m for m in months if m > base_month}
    else:
        candidates = {changed_month, db.next_month(changed_month)}
        factor_months = {m for m in candidates if m in month_set and m > base_month}

    # 一次性升级：旧结构缓存没有对数列（或系数曾被舍入为 0），
    # 这些月份不能沿用，必须从现行报价补算；正常 v2 缓存不受影响，
    # 日常换报依旧只重算相邻两段环比。
    for m, prev in existing.items():
        if m in month_set and m > base_month and prev.get("factor_log") is None:
            factor_months.add(m)

    factors: dict[str, tuple[int, float | None, float | None]] = {}
    price_cache: dict[str, dict[tuple[str, str], float]] = {}

    def prices(m: str) -> dict[tuple[str, str], float]:
        if m not in price_cache:
            price_cache[m] = db.month_prices(conn, m)
        return price_cache[m]

    for m in months:
        if m == base_month:
            factors[m] = (0, None, None)
            continue
        if m in factor_months or m not in existing:
            factors[m] = pair_factor(prices(m), prices(db.previous_month(m)))
        else:
            factors[m] = _factor_from_cache(existing[m])

    # 从基月向后在对数空间串联指数（不会因中间值过小而下溢）
    index_logs: dict[str, float | None] = {base_month: BASE_INDEX_LOG}
    for m in months[1:]:
        prev_log = index_logs.get(db.previous_month(m))
        factor_log = factors[m][2]
        if prev_log is None or factor_log is None:
            index_logs[m] = None
        else:
            index_logs[m] = prev_log + factor_log

    stamp = now_iso()
    rows = []
    for m in months:
        pair_count, factor, factor_log = factors[m]
        upload_id = upload_ids.get(m)
        if upload_id is None:
            # 该月没有任何已提交报价：不展示环比（缺月）
            pair_count, factor, factor_log = None, None, None
            index_log = None
            index_value = None
        else:
            index_log = index_logs[m]
            index_value = _exp_or_none(index_log)
        rows.append(
            {
                "month": m,
                "is_base_month": 1 if m == base_month else 0,
                "has_upload": 1 if upload_id is not None else 0,
                "pair_count": pair_count if m != base_month else None,
                "mom_factor": factor,
                "factor_log": factor_log,
                "index_value": index_value,
                "index_log": index_log,
                "source_upload_id": upload_id,
                "computed_at": stamp,
            }
        )
    db.upsert_index_rows(conn, rows)

    # 清理范围外的旧结果（正常情况下不存在）
    conn.execute(
        "DELETE FROM index_months WHERE month < ? OR month > ?",
        (base_month, latest),
    )
    _bump_generation(conn)
    return months


def _exp_or_none(value: float | None) -> float | None:
    """对数链还原为普通指数；下溢记 0.0，上溢记 NULL（SQLite REAL 无法存 inf）。"""
    if value is None:
        return None
    try:
        return math.exp(value)
    except OverflowError:
        return None


def _bump_generation(conn: sqlite3.Connection) -> None:
    """每提交一套新缓存就递增代际号，标记结果来自不同的完整快照。"""
    row = conn.execute("SELECT value FROM meta WHERE key = 'cache_generation'").fetchone()
    generation = int(row["value"]) + 1 if row else 1
    db.set_meta(conn, "cache_generation", str(generation))
    db.set_meta(conn, "cache_version", CACHE_VERSION)


def cache_is_stale(conn: sqlite3.Connection) -> bool:
    """旧结构缓存（无版本号 / 版本落后）需要从现行报价重建。"""
    return db.get_meta(conn, "cache_version") != CACHE_VERSION


def rebuild_cache(conn: sqlite3.Connection) -> dict:
    """沿用 SQLite 旧库，从当前生效报价全量重建环比与指数缓存。

    整个重建在单个 IMMEDIATE 事务内：先清空旧缓存、再全量重算并提交。
    任一步失败整体回滚，旧缓存原样保留，可再次调用重试。
    重建期间已提交的换报通过 BEGIN IMMEDIATE 的串行化天然包含在生效结果中。
    """
    base_month = db.get_meta(conn, "base_month")
    if base_month is None:
        raise RebuildError("库里还没有任何报价，无需重建")

    with db.transaction(conn):
        # 兼容应用启动后才被替换/挂载进来的旧库：先补齐列再重建
        db.migrate(conn)
        conn.execute("DELETE FROM index_months")
        months = recompute(conn, base_month, changed_month=None)
        row = conn.execute("SELECT COUNT(*) AS c FROM index_months").fetchone()

    return {
        "base_month": base_month,
        "months": months,
        "row_count": row["c"],
        "generation": db.get_meta(conn, "cache_generation"),
    }


def read_index(conn: sqlite3.Connection) -> list[dict]:
    """在一个读事务里读取指数快照，保证对数、环比、指数来自同一次提交。

    每次查询只读 index_months 这一套完整缓存，不读报价明细。
    """
    conn.execute("BEGIN")
    try:
        rows = [dict(r) for r in db.all_months(conn)]
        stale = cache_is_stale(conn)
    finally:
        conn.execute("COMMIT")

    by_month = {r["month"]: r for r in rows}
    for row in rows:
        # 指数以对数链为真源还原；旧库行（index_log 为空）回退到兼容值
        if row.get("index_log") is not None:
            index = _exp_or_none(row["index_log"])
        else:
            index = row["index_value"]
        row["index_value"] = index
        factor = row["mom_factor"]

        # 舍入只发生在输出阶段
        if index is not None:
            row["display_index"] = f"{index:.{INDEX_DISPLAY_DECIMALS}f}"
        else:
            row["display_index"] = ""
        if factor is not None:
            row["display_factor"] = f"{factor:.{FACTOR_DISPLAY_DECIMALS}f}"
        else:
            row["display_factor"] = ""
        row["cache_stale"] = stale

        if row["is_base_month"]:
            row["status"] = "base"
        elif not row.get("has_upload"):
            row["status"] = "missing"
        elif index is not None:
            row["status"] = "ok"
        elif row["pair_count"] == 0:
            prev = by_month.get(db.previous_month(row["month"]))
            prev_ok = prev is not None and prev["index_value"] is not None
            row["status"] = "no_pairs" if prev_ok else "broken"
        else:
            row["status"] = "broken"
    return rows
