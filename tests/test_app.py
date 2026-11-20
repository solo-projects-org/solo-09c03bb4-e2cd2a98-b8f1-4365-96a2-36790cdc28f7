import io
import math
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import calc, create_app, db, services  # noqa: E402


def csv_bytes(month, rows, header=("月份", "摊位", "品种", "每公斤报价")):
    lines = [",".join(header)]
    for stall, item, price in rows:
        lines.append(f"{month},{stall},{item},{price}")
    return ("\n".join(lines) + "\n").encode("utf-8")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.app = create_app({"TESTING": True, "DATABASE": self.db_path})
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def upload(self, month, rows, follow=True):
        return self.client.post(
            "/upload",
            data={"file": (io.BytesIO(csv_bytes(month, rows)), f"{month}.csv")},
            content_type="multipart/form-data",
            follow_redirects=follow,
        )

    def table(self):
        resp = self.client.get("/index.csv")
        self.assertEqual(resp.status_code, 200)
        text = resp.get_data(as_text=True).strip().splitlines()
        out = {}
        for line in text[1:]:
            month, pairs, factor, index, status = line.split(",")
            out[month] = {
                "pairs": int(pairs) if pairs else None,
                "factor": float(factor) if factor else None,
                "index": float(index) if index else None,
                "status": status,
            }
        return out

    def conn(self):
        return db.connect(self.db_path)


class TestParsing(Base):
    def test_header_alias_and_english(self):
        raw = b"month,stall,item,price\n2025-01,A,\xe5\x9c\x9f\xe8\xb1\x86,3.5\n"
        res = services.parse_csv(raw)
        self.assertEqual(res.month, "2025-01")
        self.assertEqual(res.rows[0].price, 3.5)

    def test_duplicate_rejected(self):
        raw = csv_bytes("2025-01", [("A", "土豆", 3), ("A", "土豆", 4)])
        with self.assertRaises(services.UploadError) as ctx:
            services.parse_csv(raw)
        self.assertIn("只收一条", ctx.exception.message)

    def test_non_positive_price_rejected(self):
        for bad in ("0", "-2", "abc", ""):
            with self.subTest(bad=bad):
                with self.assertRaises(services.UploadError):
                    services.parse_csv(csv_bytes("2025-01", [("A", "土豆", bad)]))

    def test_multiple_months_rejected(self):
        raw = csv_bytes("2025-01", [("A", "土豆", 3)]) + b"2025-02,B,\xe5\x9c\x9f\xe8\xb1\x86,3\n"
        with self.assertRaises(services.UploadError):
            services.parse_csv(raw)

    def test_bad_month_format_rejected(self):
        raw = csv_bytes("2025-13", [("A", "土豆", 3)])
        with self.assertRaises(services.UploadError):
            services.parse_csv(raw)

    def test_gbk_file_supported(self):
        raw = csv_bytes("2025-01", [("A", "土豆", 3)]).decode("utf-8").encode("gb18030")
        self.assertEqual(services.parse_csv(raw).month, "2025-01")


class TestIndexMath(Base):
    def test_base_month_is_100_and_factor_is_equal_weighted_mean(self):
        self.upload("2025-01", [("A", "土豆", 2.0), ("B", "土豆", 4.0)])
        self.upload("2025-02", [("A", "土豆", 2.2), ("B", "土豆", 4.4)])
        t = self.table()
        self.assertEqual(t["2025-01"]["index"], 100.0)
        self.assertEqual(t["2025-01"]["status"], "基月")
        self.assertEqual(t["2025-02"]["pairs"], 2)
        self.assertAlmostEqual(t["2025-02"]["factor"], 1.1, places=6)
        self.assertAlmostEqual(t["2025-02"]["index"], 110.0, places=4)

    def test_only_matching_stall_item_pairs_count(self):
        self.upload("2025-01", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        self.upload("2025-02", [("A", "土豆", 3.0), ("C", "白菜", 9.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["pairs"], 1)
        self.assertAlmostEqual(t["2025-02"]["factor"], 1.5, places=6)
        self.assertAlmostEqual(t["2025-02"]["index"], 150.0, places=4)

    def test_chain_compounds(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        self.upload("2025-03", [("A", "土豆", 2.0)])
        t = self.table()
        self.assertAlmostEqual(t["2025-02"]["index"], 200.0, places=4)
        self.assertAlmostEqual(t["2025-03"]["index"], 200.0, places=4)

    def test_missing_month_breaks_chain_and_backfill_restores(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        self.upload("2025-04", [("A", "土豆", 4.0)])
        t = self.table()
        self.assertEqual(t["2025-03"]["status"], "缺月")
        self.assertIsNone(t["2025-03"]["index"])
        self.assertIsNone(t["2025-04"]["index"])
        self.assertEqual(t["2025-04"]["status"], "断链")

        self.upload("2025-03", [("A", "土豆", 4.0)])
        t = self.table()
        self.assertAlmostEqual(t["2025-03"]["index"], 400.0, places=4)
        self.assertAlmostEqual(t["2025-04"]["index"], 400.0, places=4)
        self.assertEqual(t["2025-04"]["status"], "正常")

    def test_no_pairs_breaks_chain(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("Q", "黄瓜", 9.0)])
        self.upload("2025-03", [("Q", "黄瓜", 9.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["pairs"], 0)
        self.assertEqual(t["2025-02"]["status"], "无可比报价对")
        self.assertIsNone(t["2025-03"]["index"])

    def test_earlier_than_base_rejected(self):
        self.upload("2025-03", [("A", "土豆", 1.0)])
        resp = self.upload("2025-01", [("A", "土豆", 1.0)])
        self.assertIn("早于基月", resp.get_data(as_text=True))
        self.assertNotIn("2025-01", self.table())

    def test_base_month_fixed_after_first_upload(self):
        self.upload("2025-03", [("A", "土豆", 1.0)])
        self.upload("2025-05", [("A", "土豆", 2.0)])
        t = self.table()
        self.assertEqual(t["2025-03"]["status"], "基月")
        self.assertEqual(t["2025-05"]["status"], "断链")

    def test_reupload_replaces_whole_month(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        self.upload("2025-02", [("A", "土豆", 3.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["pairs"], 1)
        self.assertAlmostEqual(t["2025-02"]["factor"], 3.0, places=6)
        conn = self.conn()
        try:
            count = conn.execute(
                "SELECT COUNT(*) c FROM prices WHERE month='2025-02'"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(count, 1)


class TestIncremental(Base):
    def test_correction_only_recomputes_neighbouring_factors(self):
        for month, price in [("2025-01", 1.0), ("2025-02", 2.0), ("2025-03", 2.0)]:
            self.upload(month, [("A", "土豆", price)])
        before = self.table()
        # 标记 2025-01 的环比不参与（基月），比较 2/3 月
        self.upload("2025-02", [("A", "土豆", 3.0)])
        after = self.table()
        self.assertAlmostEqual(after["2025-02"]["factor"], 3.0, places=6)
        self.assertAlmostEqual(after["2025-02"]["index"], 300.0, places=4)
        # 3 月环比 = 2.0 / 3.0
        self.assertAlmostEqual(after["2025-03"]["factor"], 2.0 / 3.0, places=3)
        self.assertAlmostEqual(after["2025-03"]["index"], 200.0, places=4)
        # 基月未变
        self.assertEqual(before["2025-01"]["index"], after["2025-01"]["index"])

    def test_break_and_repair_via_replace(self):
        for month, price in [("2025-01", 1.0), ("2025-02", 2.0), ("2025-03", 2.5)]:
            self.upload(month, [("A", "土豆", price)])
        self.upload("2025-02", [("Z", "黄瓜", 9.0)])
        t = self.table()
        self.assertEqual(t["2025-02"]["status"], "无可比报价对")
        self.assertIsNone(t["2025-02"]["index"])
        self.assertIsNone(t["2025-03"]["index"])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        t = self.table()
        self.assertAlmostEqual(t["2025-03"]["index"], 250.0, places=4)

    def test_only_two_factors_recomputed_on_replace(self):
        for month, price in [
            ("2025-01", 1.0),
            ("2025-02", 2.0),
            ("2025-03", 2.5),
            ("2025-04", 2.5),
        ]:
            self.upload(month, [("A", "土豆", price)])

        conn = self.conn()
        try:
            with db.transaction(conn):
                # 篡改一个「不应被重算」的环比，重算后应原样保留
                conn.execute(
                    "UPDATE index_months "
                    "SET mom_factor = 1.5, factor_log = ln(1.5) "
                    "WHERE month = '2025-04'"
                )
        finally:
            conn.close()

        self.upload("2025-02", [("A", "土豆", 3.0)])
        conn = self.conn()
        try:
            rows = {
                r["month"]: r
                for r in conn.execute("SELECT * FROM index_months").fetchall()
            }
        finally:
            conn.close()
        # 2、3 月环比被重算
        self.assertAlmostEqual(rows["2025-02"]["mom_factor"], 3.0, places=6)
        self.assertAlmostEqual(rows["2025-03"]["mom_factor"], 2.5 / 3.0, places=6)
        # 4 月环比沿用，不被重算
        self.assertAlmostEqual(rows["2025-04"]["mom_factor"], 1.5, places=6)
        # 但指数全部重新串联
        self.assertAlmostEqual(rows["2025-04"]["index_value"], 300.0 * 2.5 / 3.0 * 1.5, places=3)


class TestConsistency(Base):
    def test_index_query_does_not_read_price_details(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        baseline = self.table()
        conn = self.conn()
        try:
            # 直接篡改明细，模拟「明细不可信」的情形
            conn.execute("UPDATE prices SET price = 99 WHERE month='2025-02'")
        finally:
            conn.close()
        self.assertEqual(self.table(), baseline)

    def test_failed_write_rolls_back_prices_and_index(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        before = self.table()
        conn = self.conn()
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            # 触发器令写入价格失败
            conn.execute(
                "CREATE TRIGGER boom BEFORE INSERT ON prices "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        finally:
            conn.close()
        resp = self.upload("2025-02", [("A", "土豆", 9.0)])
        self.assertIn("回滚", resp.get_data(as_text=True))
        self.assertEqual(self.table(), before)
        conn = self.conn()
        try:
            price = conn.execute(
                "SELECT price FROM prices WHERE month='2025-02'"
            ).fetchone()["price"]
            uploads = conn.execute(
                "SELECT COUNT(*) c FROM uploads WHERE month='2025-02'"
            ).fetchone()["c"]
        finally:
            conn.close()
        self.assertEqual(price, 2.0)
        self.assertEqual(uploads, 1)


class TestDemo(Base):
    def test_demo_break_and_backfill(self):
        self.client.post("/demo/load", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-04"]["status"], "缺月")
        self.assertEqual(t["2025-05"]["status"], "断链")
        self.client.post("/demo/backfill", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-05"]["status"], "正常")
        self.client.post("/demo/break", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-03"]["status"], "无可比报价对")
        self.assertIsNone(t["2025-05"]["index"])
        self.client.post("/demo/repair", follow_redirects=True)
        t = self.table()
        self.assertEqual(t["2025-05"]["status"], "正常")

    def test_demo_correction_updates_downstream(self):
        self.client.post("/demo/load", follow_redirects=True)
        self.client.post("/demo/backfill", follow_redirects=True)
        before = self.table()
        self.client.post("/demo/correct", follow_redirects=True)
        after = self.table()
        self.assertEqual(before["2025-01"], after["2025-01"])
        self.assertNotEqual(before["2025-02"]["factor"], after["2025-02"]["factor"])
        self.assertNotEqual(before["2025-05"]["index"], after["2025-05"]["index"])


class TestConcurrency(Base):
    def test_reader_never_sees_torn_snapshot(self):
        import threading

        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0), ("B", "白菜", 5.0)])
        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            try:
                while not stop.is_set():
                    i += 1
                    # 两个候选版本交替替换：环比 2.0 与 3.0
                    price = 2.0 if i % 2 else 3.0
                    self.upload("2025-02", [("A", "土豆", price)])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            for _ in range(80):
                rows = self.table()
                factor = rows["2025-02"]["factor"]
                index = rows["2025-02"]["index"]
                # 环比与指数必须来自同一次提交（全精度连乘，仅输出时舍入）
                self.assertAlmostEqual(index, 100 * factor, places=4)
                self.assertIn(round(factor, 4), (2.0, 3.0))
        finally:
            stop.set()
            thread.join(timeout=20)
        self.assertEqual(errors, [])


class TestFullPrecision(Base):
    def test_factor_and_index_not_rounded_in_storage(self):
        # 1/3 环比：旧实现会把系数舍入到 10 位、指数舍入到 6 位
        self.upload("2025-01", [("A", "土豆", 3.0)])
        self.upload("2025-02", [("A", "土豆", 1.0)])
        conn = self.conn()
        try:
            row = conn.execute(
                "SELECT * FROM index_months WHERE month='2025-02'"
            ).fetchone()
        finally:
            conn.close()
        # 缓存中是全精度，不做任何存储舍入
        self.assertEqual(row["mom_factor"], 1.0 / 3.0)
        self.assertTrue(math.isfinite(row["factor_log"]))
        self.assertTrue(math.isfinite(row["index_log"]))
        # 输出仍按原位数舍入（环比 4 位、指数 2 位）
        t = self.table()
        self.assertEqual(t["2025-02"]["factor"], round(1.0 / 3.0, 4))
        csv_text = self.client.get("/index.csv").get_data(as_text=True)
        self.assertIn("0.3333", csv_text)
        self.assertIn("33.33", csv_text)


class TestUnderflowRecovery(Base):
    def test_index_rounds_to_zero_but_chain_recovers(self):
        """网页指数显示 0.00 后，连月回升仍能算出正确指数（演示同款短链）。"""
        self.upload("2025-01", [("A", "土豆", 1.0)])
        price = 1.0
        for month in ("2025-02", "2025-03", "2025-04", "2025-05", "2025-06"):
            price *= 0.01
            self.upload(month, [("A", "土豆", price)])
        t = self.table()
        # 2025-04 指数 = 1e-6，两位小数展示为 0.00，但状态仍为正常
        self.assertEqual(t["2025-04"]["index"], 0.0)
        self.assertEqual(t["2025-04"]["status"], "正常")
        # 连续回升 5 个月，月环比 100
        for month in ("2025-07", "2025-08", "2025-09", "2025-10", "2025-11"):
            price *= 100.0
            self.upload(month, [("A", "土豆", price)])
        t = self.table()
        self.assertAlmostEqual(t["2025-11"]["index"], 100.0, places=2)
        # 回升途中（8 月指数 1e-4）仍显示 0.00，最后一个月恢复
        self.assertEqual(t["2025-08"]["index"], 0.0)

    def test_chain_below_smallest_positive_float_keeps_log_chain(self):
        """中间指数小于浮点最小正数（真下溢）时，对数链仍保留精确状态。"""
        n_drops = 163  # 指数 log ≈ -746，低于最小正浮点数的 log ≈ -744.44
        base_price = 1e308

        def month_at(i):
            return f"{2025 + i // 12}-{i % 12 + 1:02d}"

        def price_at(i):
            # 用 exp(log) 直接构造，避免中间乘法把报价本身舍入成 0
            return math.exp(math.log(base_price) + i * math.log(0.01))

        conn = self.conn()
        try:
            with db.transaction(conn):
                for i in range(n_drops + 1):
                    month = month_at(i)
                    cur = conn.execute(
                        "INSERT INTO uploads(month, filename, row_count, uploaded_at, is_base_month) "
                        "VALUES (?, ?, 1, ?, ?)",
                        (month, f"bulk-{month}.csv", calc.now_iso(), 1 if i == 0 else 0),
                    )
                    conn.execute(
                        "INSERT INTO prices(upload_id, month, stall, item, price) "
                        "VALUES (?, ?, 'A', '土豆', ?)",
                        (cur.lastrowid, month, price_at(i)),
                    )
                db.set_meta(conn, "base_month", "2025-01")
        finally:
            conn.close()

        conn = self.conn()
        try:
            result = calc.rebuild_cache(conn)
        finally:
            conn.close()
        self.assertEqual(len(result["months"]), n_drops + 1)

        conn = self.conn()
        try:
            rows = {r["month"]: r for r in conn.execute("SELECT * FROM index_months")}
            deep = rows[month_at(n_drops)]
            # 普通浮点指数已无法表示（NULL 或 0.0），但对数链有限且精确
            self.assertIn(deep["index_value"], (None, 0.0))
            self.assertTrue(math.isfinite(deep["index_log"]))
            expected_log = math.log(100.0) + n_drops * math.log(0.01)
            self.assertAlmostEqual(deep["index_log"], expected_log, places=8)
        finally:
            conn.close()

        # 网页/CSV 在真下溢区间也只显示 0.00，状态仍为正常而非断链
        t = self.table()
        self.assertEqual(t[month_at(n_drops)]["index"], 0.0)
        self.assertEqual(t[month_at(n_drops)]["status"], "正常")

    def test_long_chain_round_trip_recovers_exact_index(self):
        """155 连跌（页面全程 0.00）后同幅度连涨，指数精确回到 100。"""
        n_drops = 155
        base_price = 1e300

        def month_at(i):
            return f"{2025 + i // 12}-{i % 12 + 1:02d}"

        def drop_price(i):
            return math.exp(math.log(base_price) + i * math.log(0.01))

        conn = self.conn()
        try:
            with db.transaction(conn):
                for i in range(n_drops + 1):
                    month = month_at(i)
                    cur = conn.execute(
                        "INSERT INTO uploads(month, filename, row_count, uploaded_at, is_base_month) "
                        "VALUES (?, ?, 1, ?, ?)",
                        (month, f"bulk-{month}.csv", calc.now_iso(), 1 if i == 0 else 0),
                    )
                    conn.execute(
                        "INSERT INTO prices(upload_id, month, stall, item, price) "
                        "VALUES (?, ?, 'A', '土豆', ?)",
                        (cur.lastrowid, month, drop_price(i)),
                    )
                db.set_meta(conn, "base_month", "2025-01")
        finally:
            conn.close()

        # 最深点报价；回升月报价 = 最深点 × 100^step（与 0.01 连跌严格互逆）
        deepest_price = drop_price(n_drops)
        conn = self.conn()
        try:
            for step in range(1, n_drops + 1):
                month = month_at(n_drops + step)
                price = math.exp(math.log(deepest_price) + step * math.log(100.0))
                with db.transaction(conn):
                    cur = conn.execute(
                        "INSERT INTO uploads(month, filename, row_count, uploaded_at, is_base_month) "
                        "VALUES (?, ?, 1, ?, 0)",
                        (month, f"bulk-{month}.csv", calc.now_iso()),
                    )
                    conn.execute(
                        "INSERT INTO prices(upload_id, month, stall, item, price) "
                        "VALUES (?, ?, 'A', '土豆', ?)",
                        (cur.lastrowid, month, price),
                    )
        finally:
            conn.close()

        conn = self.conn()
        try:
            calc.rebuild_cache(conn)
        finally:
            conn.close()
        t = self.table()
        final_month = month_at(2 * n_drops)
        self.assertAlmostEqual(t[final_month]["index"], 100.0, places=2)
        # 回升前段仍显示 0.00
        self.assertEqual(t[month_at(n_drops + 5)]["index"], 0.0)


class TestRebuild(Base):
    def _make_legacy_db(self):
        """造一个旧结构库：index_months 无对数列，且系数/指数被舍入。"""
        self.upload("2025-01", [("A", "土豆", 3.0)])
        self.upload("2025-02", [("A", "土豆", 1.0)])
        conn = self.conn()
        try:
            with db.transaction(conn):
                conn.execute("ALTER TABLE index_months RENAME TO index_months_new")
                conn.execute(
                    "CREATE TABLE index_months ( "
                    "month TEXT PRIMARY KEY, is_base_month INTEGER NOT NULL DEFAULT 0, "
                    "has_upload INTEGER NOT NULL DEFAULT 0, pair_count INTEGER, "
                    "mom_factor REAL, index_value REAL, source_upload_id INTEGER, "
                    "computed_at TEXT NOT NULL)"
                )
                conn.execute(
                    "INSERT INTO index_months "
                    "SELECT month, is_base_month, has_upload, pair_count, mom_factor, "
                    "index_value, source_upload_id, computed_at FROM index_months_new"
                )
                conn.execute("DROP TABLE index_months_new")
                conn.execute("DELETE FROM meta WHERE key IN ('cache_version','cache_generation')")
                # 旧实现风格：把缓存值舍入
                conn.execute(
                    "UPDATE index_months SET mom_factor = 0.3333333333, "
                    "index_value = 33.333333 WHERE month='2025-02'"
                )
        finally:
            conn.close()

    def test_migration_and_rebuild_from_legacy_db(self):
        self._make_legacy_db()
        # 旧缓存仍可直接读取（读数不中断）
        before = self.table()
        self.assertAlmostEqual(before["2025-02"]["index"], 33.33, places=2)

        # 重新打开应用即完成列迁移；缓存被标记为旧结构
        app2 = create_app({"TESTING": True, "DATABASE": self.db_path})
        client2 = app2.test_client()
        conn = self.conn()
        try:
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(index_months)")}
            self.assertIn("index_log", columns)
            self.assertTrue(calc.cache_is_stale(conn))
        finally:
            conn.close()

        # 通过网页重建
        resp = client2.post("/rebuild", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("重建", resp.get_data(as_text=True))
        conn = self.conn()
        try:
            self.assertFalse(calc.cache_is_stale(conn))
            row = conn.execute(
                "SELECT * FROM index_months WHERE month='2025-02'"
            ).fetchone()
            self.assertEqual(row["mom_factor"], 1.0 / 3.0)
            self.assertTrue(math.isfinite(row["index_log"]))
        finally:
            conn.close()
        t = self.table()
        self.assertAlmostEqual(t["2025-02"]["factor"], round(1.0 / 3.0, 4), places=6)
        self.assertAlmostEqual(t["2025-02"]["index"], 100.0 / 3.0, places=2)

    def test_upload_after_migration_upgrades_legacy_rows(self):
        """迁移后即使不手动重建，下一次换报也会把缺对数列的旧行从报价补算。"""
        self._make_legacy_db()
        app2 = create_app({"TESTING": True, "DATABASE": self.db_path})
        client2 = app2.test_client()
        # 补一个新月份，触发一次性升级（旧行 factor_log 为空 → 强制从报价补算）
        client2.post(
            "/upload",
            data={
                "file": (
                    io.BytesIO(csv_bytes("2025-03", [("A", "土豆", 1.0)])),
                    "2025-03.csv",
                )
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        conn = self.conn()
        try:
            self.assertFalse(calc.cache_is_stale(conn))
            rows = {r["month"]: r for r in conn.execute("SELECT * FROM index_months")}
            # 2 月旧缓存（被舍入的 0.3333333333）已从现行报价补算为全精度
            self.assertEqual(rows["2025-02"]["mom_factor"], 1.0 / 3.0)
            self.assertTrue(math.isfinite(rows["2025-02"]["factor_log"]))
            self.assertTrue(math.isfinite(rows["2025-02"]["index_log"]))
        finally:
            conn.close()
        t = self.table()
        self.assertAlmostEqual(t["2025-03"]["factor"], 1.0, places=4)
        self.assertAlmostEqual(t["2025-03"]["index"], 100.0 / 3.0, places=2)

    def test_failed_rebuild_keeps_old_cache_and_can_retry(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        before = self.table()
        conn = self.conn()
        try:
            conn.execute(
                "CREATE TRIGGER rebuild_boom BEFORE INSERT ON index_months "
                "BEGIN SELECT RAISE(ABORT, 'boom'); END"
            )
        finally:
            conn.close()

        # 重建失败：抛错回滚，原缓存一行不少
        conn = self.conn()
        try:
            with self.assertRaises(sqlite3.Error):
                calc.rebuild_cache(conn)
            count = conn.execute("SELECT COUNT(*) c FROM index_months").fetchone()["c"]
            self.assertEqual(count, 2)
        finally:
            conn.close()
        self.assertEqual(self.table(), before)

        # 排除故障后重试成功
        conn = self.conn()
        try:
            conn.execute("DROP TRIGGER rebuild_boom")
        finally:
            conn.close()
        conn = self.conn()
        try:
            result = calc.rebuild_cache(conn)
        finally:
            conn.close()
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(self.table(), before)

    def test_rebuild_with_no_prices_rejected(self):
        conn = self.conn()
        try:
            with self.assertRaises(calc.RebuildError):
                calc.rebuild_cache(conn)
        finally:
            conn.close()

    def test_rebuild_reflects_committed_price_changes(self):
        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        # 直接改现行报价（已提交），重建必须反映新报价
        conn = self.conn()
        try:
            with db.transaction(conn):
                conn.execute("UPDATE prices SET price = 5.0 WHERE month='2025-02'")
        finally:
            conn.close()
        conn = self.conn()
        try:
            calc.rebuild_cache(conn)
        finally:
            conn.close()
        t = self.table()
        self.assertAlmostEqual(t["2025-02"]["factor"], 5.0, places=4)
        self.assertAlmostEqual(t["2025-02"]["index"], 500.0, places=2)


class TestRebuildConcurrency(Base):
    def test_uploads_interleaved_with_rebuild_never_tear_cache(self):
        import threading

        self.upload("2025-01", [("A", "土豆", 1.0)])
        self.upload("2025-02", [("A", "土豆", 2.0)])
        stop = threading.Event()
        errors = []

        def writer():
            i = 0
            try:
                while not stop.is_set():
                    i += 1
                    price = 2.0 if i % 2 else 3.0
                    self.upload("2025-02", [("A", "土豆", price)])
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def rebuilder():
            try:
                while not stop.is_set():
                    conn = db.connect(self.db_path)
                    try:
                        calc.rebuild_cache(conn)
                    finally:
                        conn.close()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=writer), threading.Thread(target=rebuilder)]
        for thread in threads:
            thread.start()
        try:
            for _ in range(100):
                rows = self.table()
                factor = rows["2025-02"]["factor"]
                index = rows["2025-02"]["index"]
                # 每次读到的必须是一套完整缓存：指数 = 100 × 当次环比
                self.assertAlmostEqual(index, 100 * factor, places=4)
                self.assertIn(round(factor, 4), (2.0, 3.0))
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(errors, [])

        # 最终结果必须反映最后一次已提交的换报
        conn = self.conn()
        try:
            price = conn.execute(
                "SELECT price FROM prices WHERE month='2025-02'"
            ).fetchone()["price"]
        finally:
            conn.close()
        t = self.table()
        self.assertAlmostEqual(t["2025-02"]["factor"], price, places=4)
        self.assertAlmostEqual(t["2025-02"]["index"], 100 * price, places=2)


class TestDemoUnderflow(Base):
    def test_plunge_then_recover_demo(self):
        resp = self.client.post("/demo/plunge", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        t = self.table()
        self.assertEqual(t["2025-01"]["status"], "基月")
        self.assertEqual(t["2025-04"]["index"], 0.0)
        self.assertEqual(t["2025-06"]["index"], 0.0)
        self.client.post("/demo/recover", follow_redirects=True)
        t = self.table()
        self.assertAlmostEqual(t["2025-11"]["index"], 100.0, places=2)
        # 回升途中仍显示 0.00，最后一个月恢复
        self.assertEqual(t["2025-08"]["index"], 0.0)

    def test_recover_requires_plunge_data(self):
        self.client.post("/demo/load", follow_redirects=True)
        resp = self.client.post("/demo/recover", follow_redirects=True)
        self.assertIn("连跌", resp.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
